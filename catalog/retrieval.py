"""Pick the tables a question needs from a catalog.

Ranking is lexical (fielded BM25 over table and column documents) and, when the catalog was
embedded with the embedder in use, fused with cosine similarity by reciprocal rank fusion.
Literal values mentioned in the question are matched against the stored enum values. The
relationship graph then adds the tables needed to join the matches and to label their ids.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

import numpy as np

from catalog.graph import RelationshipGraph, singular
from catalog.models import Catalog, SchemaContext
from catalog.render import render_context, to_legacy_schema

logger = logging.getLogger(__name__)

SMALL_CATALOG = 12         # at most this many tables: send them all
FLOOR = 3                  # matched tables kept regardless of the score gap
MAX_LOOKUPS = 4
MAX_VALUE_HINTS = 10
RRF_K = 60

_W_NAME, _W_DESC, _W_COLS, _W_COLDESC = 4.0, 2.0, 1.5, 0.5
_K1, _B_TABLE, _B_COLUMN = 1.2, 0.5, 0.3
_COLUMN_LIFT = 0.7
_SYNONYM_WEIGHT = 0.5

_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_STOPWORDS = frozenset("""
a an the of in on for to from with and or by is are was were be been what which who whom how
many much show list give get find me all each per that have has had do does did their its it
this these those than as at most least more less top number count tell display where
when whose i we us my our you your please can could would there any not no between over under
last first id into out about than then them they he she his her if so also only just
""".split())

_SYNONYM_GROUPS = (
    ("customer", "client", "buyer", "shopper"),
    ("order", "purchase"),
    ("revenue", "sales", "income", "turnover", "amount"),
    ("employee", "staff", "worker"),
    ("doctor", "physician", "provider", "clinician"),
    ("supplier", "vendor"),
    ("price", "cost"),
    ("address", "location"),
    ("medication", "drug", "medicine"),
    ("appointment", "visit", "encounter"),
    ("invoice", "bill"),
    ("salary", "wage", "payroll", "pay"),
    ("shipment", "shipping", "delivery"),
    ("product", "item"),
    ("refund", "return"),
)


def stem(token: str) -> str:
    """Light stemmer: plural, -ed, doubled consonant, trailing e.

    -ing is left alone on purpose: stripping it merges nouns such as bookings and books.
    """
    word = singular(token)
    if word.endswith("ed") and len(word) >= 5:
        word = word[:-2]
        if len(word) > 2 and word[-1] == word[-2] and word[-1] not in "aeiouls":
            word = word[:-1]
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def tokenize(text: str | None) -> list[str]:
    """Lower-case stems of ``text``: snake_case and camelCase are split, stopwords dropped."""
    out = []
    for raw in _WORDS.findall(text or ""):
        low = raw.lower()
        if low in _STOPWORDS or (len(low) < 2 and not low.isdigit()):
            continue
        out.append(stem(low))
    return out


_SYNONYMS: dict[str, tuple[str, ...]] = {}
for _group in _SYNONYM_GROUPS:
    _stems = tuple(dict.fromkeys(stem(w) for w in _group))
    for _s in _stems:
        _SYNONYMS[_s] = tuple(x for x in _stems if x != _s)


# ---------------------------------------------------------------------- index
@dataclass
class _Postings:
    """BM25 over documents made of weighted fields."""
    rows: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    idf: dict[str, float] = field(default_factory=dict)
    norm: np.ndarray | None = None       # per document length normalisation factor
    size: int = 0

    @classmethod
    def build(cls, docs: list[dict[str, float]], b: float) -> "_Postings":
        lengths = np.array([sum(d.values()) for d in docs], dtype=np.float64)
        avg = float(lengths.mean()) if len(docs) and lengths.sum() > 0 else 1.0
        collected: dict[str, tuple[list[int], list[float]]] = {}
        for i, doc in enumerate(docs):
            for term, weight in doc.items():
                ids, weights = collected.setdefault(term, ([], []))
                ids.append(i)
                weights.append(weight)
        n = len(docs)
        self = cls(size=n)
        self.norm = _K1 * (1.0 - b + b * lengths / avg) if n else np.zeros(0)
        for term, (ids, weights) in collected.items():
            self.rows[term] = (np.array(ids, dtype=np.int64), np.array(weights))
            self.idf[term] = math.log(1.0 + (n - len(ids) + 0.5) / (len(ids) + 0.5))
        return self

    def score(self, terms: dict[str, float]) -> np.ndarray:
        scores = np.zeros(self.size)
        for term, weight in terms.items():
            row = self.rows.get(term)
            if row is None:
                continue
            ids, tf = row
            scores[ids] += weight * self.idf[term] * tf * (_K1 + 1.0) / (tf + self.norm[ids])
        return scores


@dataclass
class _Index:
    fingerprint: tuple
    names: list[str]
    pos: dict[str, int]
    name_tokens: list[frozenset]
    tables: _Postings
    columns: _Postings
    column_table: np.ndarray
    values: dict[str, list[tuple[str, str, str]]]
    value_stems: dict[str, list[tuple[str, str, str]]]
    vocabulary: frozenset
    table_words: frozenset
    graph: RelationshipGraph
    degree: dict[str, int]
    vectors: tuple | None = None         # built on first use


def _fingerprint(catalog: Catalog) -> tuple:
    return (id(catalog.tables), len(catalog.tables), id(catalog.relationships),
            len(catalog.relationships), catalog.built_at, catalog.embedder_name,
            len(catalog.table_vectors), len(catalog.column_vectors))


def _add(doc: dict[str, float], tokens: list[str], weight: float) -> None:
    for token in tokens:
        doc[token] = doc.get(token, 0.0) + weight


def _build_index(catalog: Catalog) -> _Index:
    names = sorted(catalog.tables)
    table_docs: list[dict[str, float]] = []
    column_docs: list[dict[str, float]] = []
    column_table: list[int] = []
    name_tokens: list[frozenset] = []
    vocabulary: set[str] = set()
    values: dict[str, list[tuple[str, str, str]]] = {}
    value_stems: dict[str, list[tuple[str, str, str]]] = {}
    for i, name in enumerate(names):
        table = catalog.tables[name]
        doc: dict[str, float] = {}
        tokens = tokenize(name)
        if len(tokens) > 1:
            tokens_plus = tokens + ["".join(tokens)]
        else:
            tokens_plus = tokens
        name_tokens.append(frozenset(tokens))
        vocabulary.update(tokens_plus)
        _add(doc, tokens_plus, _W_NAME)
        _add(doc, tokenize(table.description), _W_DESC)
        for col in table.columns:
            col_tokens = tokenize(col.name)
            desc_tokens = tokenize(col.description)
            vocabulary.update(col_tokens)
            _add(doc, col_tokens, _W_COLS)
            _add(doc, desc_tokens, _W_COLDESC)
            col_doc: dict[str, float] = {}
            _add(col_doc, col_tokens, 3.0)
            _add(col_doc, desc_tokens, 1.0)
            column_docs.append(col_doc)
            column_table.append(i)
            if col.is_pii or not col.enum_values:
                continue                    # PII values are never matched or echoed
            for value in col.enum_values:
                text = " ".join(str(value).split())
                if not text or len(text) > 60:
                    continue
                entry = (name, col.name, str(value))
                values.setdefault(text.lower(), []).append(entry)
                if " " not in text:
                    value_stems.setdefault(stem(text.lower()), []).append(entry)
        table_docs.append(doc)
    graph = RelationshipGraph(catalog)
    return _Index(
        fingerprint=_fingerprint(catalog), names=names, pos={n: i for i, n in enumerate(names)},
        name_tokens=name_tokens, tables=_Postings.build(table_docs, _B_TABLE),
        columns=_Postings.build(column_docs, _B_COLUMN),
        column_table=np.array(column_table, dtype=np.int64), values=values,
        value_stems=value_stems, vocabulary=frozenset(vocabulary),
        table_words=frozenset().union(*name_tokens) if name_tokens else frozenset(),
        graph=graph,
        degree={n: len(graph.neighbours(n)) for n in names},
    )


def get_index(catalog: Catalog) -> _Index:
    """The search index of a catalog, cached on the catalog instance."""
    index = catalog.__dict__.get("_retrieval_index")
    if index is None or index.fingerprint != _fingerprint(catalog):
        index = _build_index(catalog)
        catalog.__dict__["_retrieval_index"] = index
    return index


# ---------------------------------------------------------------------- scoring
def _query_terms(question: str, index: _Index) -> dict[str, float]:
    tokens = tokenize(question)
    terms: dict[str, float] = {t: 1.0 for t in tokens}
    for a, b in zip(tokens, tokens[1:]):
        joined = a + b                       # "order items" also finds a table named orderitems
        if joined in index.vocabulary:
            terms.setdefault(joined, 1.0)
    for token in tokens:
        for other in _SYNONYMS.get(token, ()):
            terms.setdefault(other, _SYNONYM_WEIGHT)
    return terms


def _lexical(index: _Index, terms: dict[str, float]) -> np.ndarray:
    scores = index.tables.score(terms)
    if index.columns.size:
        col_scores = index.columns.score(terms)
        hit = np.nonzero(col_scores)[0]
        if len(hit):
            best = np.zeros(len(index.names))
            np.maximum.at(best, index.column_table[hit], col_scores[hit])
            scores += _COLUMN_LIFT * best
    direct = {t for t, w in terms.items() if w >= 1.0}
    for i in np.nonzero(scores)[0]:
        wanted = index.name_tokens[i]
        if wanted:
            scores[i] *= 1.0 + len(wanted & direct) / len(wanted)
    return scores


_QUOTED = re.compile(r"(?<![A-Za-z0-9])'([^'\n]{1,60})'(?![A-Za-z0-9])|\"([^\"\n]{1,60})\"")
_VALUE_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&./\-]*")


def _value_matches(question: str, index: _Index) -> list[tuple[str, str, str]]:
    """(table, column, exact stored value) for literals the question mentions."""
    if not index.values:
        return []
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def take(entries) -> None:
        for entry in sorted(entries):
            if entry not in seen:
                seen.add(entry)
                found.append(entry)

    blocked: list[tuple[int, int]] = []
    for match in _QUOTED.finditer(question):
        text = " ".join((match.group(1) or match.group(2) or "").split()).lower()
        blocked.append(match.span())
        take(index.values.get(text, ()))

    words = [(m.group(0).rstrip(".-/"), m.start(), m.end()) for m in _VALUE_WORD.finditer(question)]
    words = [w for w in words if not any(s <= w[1] < e for s, e in blocked)]
    used = [False] * len(words)
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            if any(used[i:i + size]):
                continue
            chunk = words[i:i + size]
            lows = [w[0].lower() for w in chunk]
            entries = index.values.get(" ".join(lows))
            if size > 1:
                if not entries or all(w in _STOPWORDS for w in lows):
                    continue
            else:
                word, low = chunk[0][0], lows[0]
                if low in _STOPWORDS or low.isdigit() or len(low) < 2:
                    continue
                capitalised = (word[0].isupper() and chunk[0][1] > 0) or (
                    word.isupper() and len(word) > 1)
                word_stem = stem(low)
                rare = len(low) >= 3 and word_stem not in index.vocabulary
                # an exact stored value in a few columns counts unless it names a table
                exact = (bool(entries) and len(low) >= 3 and len(entries) <= 3
                         and word_stem not in index.table_words)
                if not (capitalised or rare or exact):
                    continue
                if not entries and rare:
                    entries = index.value_stems.get(word_stem)
                if not entries:
                    continue
            take(entries)
            for j in range(i, i + size):
                used[j] = True
    return found


def _vectors(catalog: Catalog, index: _Index) -> tuple:
    if index.vectors is None:
        def matrix(items):
            rows = [(index.pos[t], v) for t, v in items if t in index.pos and len(v)]
            if not rows:
                return None, None
            mat = np.asarray([v for _, v in rows], dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            return mat / norms, np.array([i for i, _ in rows], dtype=np.int64)

        table_items = sorted(catalog.table_vectors.items())
        column_items = [(t, v) for (t, _c), v in sorted(catalog.column_vectors.items())]
        index.vectors = (*matrix(table_items), *matrix(column_items))
    return index.vectors


def _vector_scores(catalog: Catalog, index: _Index, question: str, embedder) -> np.ndarray | None:
    """Cosine per table (best of the table vector and its column vectors), or None."""
    if embedder is None or not catalog.table_vectors or not catalog.embedder_name:
        return None
    if getattr(embedder, "name", None) != catalog.embedder_name:
        return None
    try:
        query = np.asarray(embedder.embed_query(question), dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if not norm:
            return None
        query /= norm
        t_mat, t_ids, c_mat, c_ids = _vectors(catalog, index)
        scores = np.full(len(index.names), -1.0)
        if t_mat is not None:
            np.maximum.at(scores, t_ids, t_mat @ query)
        if c_mat is not None:
            np.maximum.at(scores, c_ids, 0.9 * (c_mat @ query))
        return scores
    except Exception as exc:  # noqa: BLE001 - retrieval must survive a broken embedder
        logger.warning("Vector scoring failed (%s): lexical ranking only", type(exc).__name__)
        return None


def _ranked(index: _Index, scores: np.ndarray, minimum: float = 0.0) -> list[tuple[str, float]]:
    pairs = [(index.names[i], float(scores[i])) for i in np.nonzero(scores > minimum)[0]]
    pairs.sort(key=lambda p: (-p[1], p[0]))
    return pairs


def _cut(ranked: list[tuple[str, float]], *, floor: int, limit: int, relative: float,
         drop: float) -> list[str]:
    """Adaptive cut: stop at the limit, below a fraction of the best score, or at a sharp drop."""
    kept: list[str] = []
    if not ranked:
        return kept
    top = previous = ranked[0][1]
    for i, (name, score) in enumerate(ranked):
        if i >= limit:
            break
        if i >= floor and (score < relative * top or score < drop * previous):
            break
        kept.append(name)
        previous = score
    return kept


def _resolve(catalog: Catalog, names) -> list[str]:
    lower = None
    out: list[str] = []
    for raw in names or ():
        name = raw if raw in catalog.tables else None
        if name is None and isinstance(raw, str):
            if lower is None:
                lower = {t.lower(): t for t in sorted(catalog.tables, reverse=True)}
            name = lower.get(raw.lower())
        if name is not None and name not in out:
            out.append(name)
    return out


# ---------------------------------------------------------------------- entry point
def select_context(catalog: Catalog, question: str, *, embedder=None, llm_filter=None,
                   max_tables: int = 12, widen: bool = False, must_include=()) -> SchemaContext:
    """Tables, join hints, value hints and prompt text for one question.

    ``widen`` roughly doubles how many tables are returned (used when a first attempt missed
    a table). ``must_include`` tables are always present. ``llm_filter(question, candidates)``
    may narrow the matched candidates; whatever it returns outside the candidates is ignored
    and any failure keeps the unfiltered list.
    """
    index = get_index(catalog)
    question = question or ""
    max_tables = max(1, int(max_tables))
    cap = max_tables * 2 if widen else max_tables
    limit = max_tables if widen else max(FLOOR, (max_tables + 1) // 2)
    limit = min(limit, cap)
    relative, drop = (0.15, 0.3) if (widen or llm_filter is not None) else (0.3, 0.5)

    lexical = _lexical(index, _query_terms(question, index))
    value_matches = _value_matches(question, index)
    if value_matches:
        top = float(lexical.max()) if len(lexical) else 0.0
        by_value: dict[str, set[str]] = {}
        for table, _column, value in value_matches:
            by_value.setdefault(value.lower(), set()).add(table)
        for owners in by_value.values():
            for table in owners:
                lexical[index.pos[table]] += 0.6 * max(top, 1.0) / len(owners)

    lex_ranked = _ranked(index, lexical)
    vector = _vector_scores(catalog, index, question, embedder)
    if vector is None:
        top = lex_ranked[0][1] if lex_ranked else 1.0
        scores = {name: score / top for name, score in lex_ranked}
        candidates = _cut(lex_ranked, floor=FLOOR, limit=limit, relative=relative, drop=drop)
    else:
        vec_ranked = _ranked(index, vector, minimum=-1.0)
        fused: dict[str, float] = {}
        for ranking in (lex_ranked, vec_ranked):
            for rank, (name, _score) in enumerate(ranking, start=1):
                fused[name] = fused.get(name, 0.0) + 1.0 / (RRF_K + rank)
        best = max(fused.values(), default=1.0)
        scores = {name: value / best for name, value in fused.items()}
        keep = set(_cut(lex_ranked, floor=FLOOR, limit=limit, relative=relative, drop=drop))
        positive = [(n, s) for n, s in vec_ranked if s > 0]
        keep.update(_cut(positive, floor=FLOOR, limit=limit, relative=0.6 if widen else 0.75,
                         drop=0.0))
        candidates = sorted(keep, key=lambda n: (-scores[n], n))[:limit]

    required = _resolve(catalog, must_include)

    if llm_filter is not None and candidates:
        payload = []
        for name in candidates:
            table = catalog.tables[name]
            payload.append({
                "name": name, "description": table.description or "",
                "columns": [c.name for c in table.columns[:40]],
                "score": round(scores.get(name, 0.0), 4),
            })
        try:
            chosen = set(llm_filter(question, payload) or ())
            filtered = [name for name in candidates if name in chosen]
            if filtered:
                candidates = filtered
        except Exception as exc:  # noqa: BLE001 - the filter is an optimisation only
            logger.warning("Table filter failed (%s): keeping all candidates",
                           type(exc).__name__)

    reasons: dict[str, str] = {name: "required" for name in required}
    for name in candidates:
        reasons.setdefault(name, "matched")
    if not reasons and len(index.names) > SMALL_CATALOG:
        # nothing matched at all: offer the most connected tables instead of nothing
        hubs = sorted(index.names, key=lambda n: (-index.degree[n], n))[:min(limit, cap)]
        reasons = {name: "fallback" for name in hubs}

    def by_score(names) -> list[str]:
        return sorted(names, key=lambda n: (-scores.get(n, 0.0), n))

    selected = by_score(reasons)
    for name in index.graph.bridge(selected):
        if len(reasons) >= cap:
            break
        reasons[name] = "bridge"
    lookups = 0
    for name in index.graph.lookups(list(reasons)):
        if len(reasons) >= cap or lookups >= MAX_LOOKUPS:
            break
        reasons[name] = "lookup"
        lookups += 1
    if len(index.names) <= min(SMALL_CATALOG, cap):
        for name in index.names:
            reasons.setdefault(name, "small_schema")

    ranked_reasons = ("required", "matched", "fallback")
    final_scores = {n: round(scores.get(n, 0.0), 4) if reasons[n] in ranked_reasons else 0.0
                    for n in reasons}
    tables = sorted(reasons, key=lambda n: (-final_scores[n], n))
    chosen_set = set(tables)
    value_hints = [{"table": t, "column": c, "value": v}
                   for t, c, v in value_matches if t in chosen_set][:MAX_VALUE_HINTS]
    join_hints = index.graph.join_hints(tables)
    return SchemaContext(
        tables=tables,
        schema=to_legacy_schema(catalog, tables),
        prompt_text=render_context(catalog, tables, join_hints, value_hints),
        join_hints=join_hints,
        value_hints=value_hints,
        scores=final_scores,
        reasons={n: reasons[n] for n in tables},
    )
