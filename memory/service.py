"""Query memory: past questions as few-shot examples, user feedback, and lessons.

Stored in the metadata database (tables ``t2s_query_log`` and ``t2s_lessons``). Every
statement is scoped by tenant in SQL. Only the question, the SQL and bookkeeping are kept:
result rows are never stored.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import sqlalchemy as sa

from catalog.retrieval import tokenize

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 2000
MAX_SQL_CHARS = 8000
MAX_COMMENT_CHARS = 1000
MAX_LESSON_CHARS = 400
MAX_LESSONS_PER_CONNECTION = 50
MAX_TABLES = 50
SCAN_LIMIT = 500                 # most recent candidate rows compared per lookup
MIN_COSINE = 0.55
MIN_OVERLAP = 0.34
LESSON_DUPLICATE = 0.8
_LESSON_SOURCES = ("correction", "feedback")
_UNSET = object()

_metadata = sa.MetaData()
query_log = sa.Table(
    "t2s_query_log", _metadata,
    sa.Column("id", sa.String(40), primary_key=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("connection_id", sa.String(128), nullable=False),
    sa.Column("thread_id", sa.String(128)),
    sa.Column("question", sa.Text, nullable=False),
    sa.Column("sql", sa.Text),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("error_type", sa.String(64)),
    sa.Column("tables", sa.Text),
    sa.Column("db_type", sa.String(32)),
    sa.Column("embedding", sa.Text),
    sa.Column("embedder_name", sa.String(128)),
    sa.Column("verified", sa.Integer, nullable=False, default=0),
    sa.Column("excluded", sa.Integer, nullable=False, default=0),
    sa.Column("rating", sa.String(8)),
    sa.Column("comment", sa.Text),
    sa.Column("source", sa.String(16), nullable=False, default="query"),
    sa.Column("corrects", sa.String(40)),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Index("ix_t2s_query_log_scope", "tenant_id", "connection_id", "created_at"),
)
lessons_table = sa.Table(
    "t2s_lessons", _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("connection_id", sa.String(128), nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("text_norm", sa.Text, nullable=False),
    sa.Column("source", sa.String(16), nullable=False),
    sa.Column("hits", sa.Integer, nullable=False, default=1),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("last_seen", sa.String(40), nullable=False),
    sa.Index("ix_t2s_lessons_scope", "tenant_id", "connection_id"),
)


@dataclass
class Example:
    question: str
    sql: str
    verified: bool
    score: float


def _clip(text, limit: int) -> str:
    text = "" if text is None else str(text).strip()
    return text if len(text) <= limit else text[:limit]


def normalize_sql(sql: str) -> str:
    """Whitespace- and case-insensitive form used to spot the same statement twice."""
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";").strip()).lower()


def _normalize_text(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class MemoryService:
    """Examples, feedback and lessons for one metadata database.

    ``embedder`` defaults to ``catalog.embedding.get_embedder()`` (which may be None: token
    overlap is used then). ``dialect_resolver(tenant_id, connection_id) -> db_type | None`` is
    consulted when corrected SQL has to be safety-scanned and the query row has no db_type.
    """

    def __init__(self, engine=None, *, embedder=_UNSET, dialect_resolver=None, clock=None,
                 max_lessons: int = MAX_LESSONS_PER_CONNECTION) -> None:
        self._engine = engine
        self._embedder = embedder
        self._dialect_resolver = dialect_resolver
        self._clock = clock
        self._max_lessons = max(1, int(max_lessons))
        self._ready = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ plumbing
    def _db(self):
        if self._engine is None:
            from database.metadata import get_engine

            self._engine = get_engine()
        if not self._ready:
            with self._lock:
                if not self._ready:
                    _metadata.create_all(self._engine)
                    self._ready = True
        return self._engine

    def _now(self) -> str:
        moment = self._clock() if self._clock else datetime.now(timezone.utc)
        return moment.isoformat(timespec="microseconds")

    def _get_embedder(self):
        if self._embedder is _UNSET:
            try:
                from catalog.embedding import get_embedder

                self._embedder = get_embedder()
            except Exception:  # noqa: BLE001 - optional
                self._embedder = None
        return self._embedder

    def _embed(self, question: str) -> tuple[list[float] | None, str | None]:
        embedder = self._get_embedder()
        if embedder is None or not question:
            return None, None
        try:
            vector = [round(float(x), 6) for x in embedder.embed_query(question)]
            return (vector or None), getattr(embedder, "name", None)
        except Exception as exc:  # noqa: BLE001 - memory must not break a query
            logger.warning("Question embedding failed (%s)", type(exc).__name__)
            return None, None

    # ------------------------------------------------------------------ query log
    def log_query(self, tenant_id, connection_id, *, question, sql, status, error_type=None,
                  tables=(), thread_id=None, db_type=None) -> str:
        """Record one answered (or failed) question and return its query id."""
        question = _clip(question, MAX_QUESTION_CHARS)
        sql_text = _clip(sql, MAX_SQL_CHARS) or None
        vector, embedder_name = (None, None)
        if str(status) == "completed" and sql_text:
            vector, embedder_name = self._embed(question)
        query_id = uuid.uuid4().hex
        names = [_clip(t, 128) for t in list(tables or ())[:MAX_TABLES]]
        with self._db().begin() as conn:
            conn.execute(sa.insert(query_log).values(
                id=query_id, tenant_id=str(tenant_id), connection_id=str(connection_id),
                thread_id=_clip(thread_id, 128) or None, question=question, sql=sql_text,
                status=_clip(status, 32) or "unknown", error_type=_clip(error_type, 64) or None,
                tables=json.dumps(names), db_type=_clip(db_type, 32) or None,
                embedding=json.dumps(vector) if vector else None, embedder_name=embedder_name,
                verified=0, excluded=0, source="query", created_at=self._now()))
        return query_id

    def record_feedback(self, tenant_id, query_id, *, rating: str, comment=None,
                        corrected_sql=None, dialect=None) -> None:
        """Thumbs up/down on a logged query.

        "up" verifies the query. "down" removes it from the examples; with ``corrected_sql``
        the correction becomes a verified example for the same question, and a comment
        becomes a lesson. Raises LookupError for a query that is not this tenant's and
        ValueError for a bad rating or corrected SQL that fails the safety scan.
        """
        if rating not in ("up", "down"):
            raise ValueError("rating must be 'up' or 'down'")
        scope = sa.and_(query_log.c.id == str(query_id), query_log.c.tenant_id == str(tenant_id))
        with self._db().connect() as conn:
            row = conn.execute(sa.select(query_log).where(scope)).mappings().first()
        if row is None or row["source"] != "query":
            raise LookupError("query not found")
        comment = _clip(comment, MAX_COMMENT_CHARS) or None
        corrected = (corrected_sql or "").strip() or None
        if corrected and rating == "down":
            if len(corrected) > MAX_SQL_CHARS:
                raise ValueError("corrected SQL is too long")
            self._check_sql(corrected, dialect or row["db_type"]
                            or self._resolve_dialect(tenant_id, row["connection_id"]))
        else:
            corrected = None

        with self._db().begin() as conn:
            if rating == "up":
                usable = row["status"] == "completed" and bool(row["sql"])
                conn.execute(sa.update(query_log).where(scope).values(
                    rating="up", comment=comment, verified=1 if usable else 0, excluded=0))
            else:
                conn.execute(sa.update(query_log).where(scope).values(
                    rating="down", comment=comment, verified=0, excluded=1))
                previous = sa.and_(query_log.c.tenant_id == str(tenant_id),
                                   query_log.c.corrects == str(query_id))
                conn.execute(sa.delete(query_log).where(previous))
                if corrected:
                    conn.execute(sa.insert(query_log).values(
                        id=uuid.uuid4().hex, tenant_id=row["tenant_id"],
                        connection_id=row["connection_id"], thread_id=row["thread_id"],
                        question=row["question"], sql=corrected, status="completed",
                        tables=row["tables"], db_type=row["db_type"],
                        embedding=row["embedding"], embedder_name=row["embedder_name"],
                        verified=1, excluded=0, rating=None, source="correction",
                        corrects=str(query_id), created_at=self._now()))
        if rating == "down" and comment:
            self.add_lesson(tenant_id, row["connection_id"], comment, source="feedback")

    def _resolve_dialect(self, tenant_id, connection_id):
        if self._dialect_resolver is None:
            return None
        try:
            return self._dialect_resolver(tenant_id, connection_id)
        except Exception:  # noqa: BLE001 - fall back to the generic dialect
            return None

    @staticmethod
    def _check_sql(sql: str, dialect) -> None:
        from database.safety import scan_sql

        verdict = scan_sql(sql, dialect=dialect)
        if not verdict.get("safe"):
            raise ValueError(f"corrected SQL rejected: {verdict.get('reason') or 'not read-only'}")

    # ------------------------------------------------------------------ examples
    def similar_examples(self, tenant_id, connection_id, question, *, k=3) -> list[Example]:
        """Up to ``k`` past questions close to this one, verified ones first."""
        k = max(0, min(int(k), 20))
        question = _clip(question, MAX_QUESTION_CHARS)
        if not k or not question:
            return []
        stmt = (
            sa.select(query_log.c.question, query_log.c.sql, query_log.c.verified,
                      query_log.c.embedding, query_log.c.embedder_name, query_log.c.created_at)
            .where(query_log.c.tenant_id == str(tenant_id),
                   query_log.c.connection_id == str(connection_id),
                   query_log.c.status == "completed", query_log.c.excluded == 0,
                   query_log.c.sql.is_not(None), query_log.c.sql != "")
            .order_by(query_log.c.verified.desc(), query_log.c.created_at.desc(),
                      query_log.c.id)
            .limit(SCAN_LIMIT)
        )
        with self._db().connect() as conn:
            rows = conn.execute(stmt).all()
        if not rows:
            return []

        tokens = set(tokenize(question))
        query_vec = None
        embedder_name = None
        if any(r.embedding for r in rows):          # never embed when nothing can be compared
            vector, embedder_name = self._embed(question)
            if vector:
                query_vec = np.asarray(vector, dtype=np.float64)
                norm = float(np.linalg.norm(query_vec))
                query_vec = query_vec / norm if norm else None

        scored = []
        for row in rows:
            score = None
            if query_vec is not None and row.embedding and row.embedder_name == embedder_name:
                try:
                    other = np.asarray(json.loads(row.embedding), dtype=np.float64)
                    norm = float(np.linalg.norm(other))
                    if norm and other.shape == query_vec.shape:
                        score = float(other @ query_vec / norm)
                        if score < MIN_COSINE:
                            continue
                except (ValueError, TypeError):
                    score = None
            if score is None:
                score = _jaccard(tokens, set(tokenize(row.question)))
                if score < MIN_OVERLAP:
                    continue
            scored.append((0 if row.verified else 1, -score, row.created_at, row))
        scored.sort(key=lambda item: (item[0], item[1], _reverse(item[2]), item[3].question))

        out: list[Example] = []
        seen: set[str] = set()
        for _rank, negative, _created, row in scored:
            key = normalize_sql(row.sql)
            if key in seen:
                continue
            seen.add(key)
            out.append(Example(question=row.question, sql=row.sql, verified=bool(row.verified),
                               score=round(-negative, 4)))
            if len(out) >= k:
                break
        return out

    # ------------------------------------------------------------------ lessons
    def add_lesson(self, tenant_id, connection_id, text, *, source: str) -> None:
        """Remember a short lesson. A near-identical lesson is counted, not duplicated."""
        if source not in _LESSON_SOURCES:
            raise ValueError("source must be 'correction' or 'feedback'")
        text = _clip(" ".join(str(text or "").split()), MAX_LESSON_CHARS)
        norm = _normalize_text(text)
        if not norm:
            return
        scope = sa.and_(lessons_table.c.tenant_id == str(tenant_id),
                        lessons_table.c.connection_id == str(connection_id))
        words = set(norm.split())
        now = self._now()
        with self._lock, self._db().begin() as conn:
            existing = conn.execute(
                sa.select(lessons_table.c.id, lessons_table.c.text_norm, lessons_table.c.hits,
                          lessons_table.c.last_seen).where(scope).order_by(lessons_table.c.id)
            ).all()
            for row in existing:
                same = row.text_norm == norm
                if same or _jaccard(words, set(row.text_norm.split())) >= LESSON_DUPLICATE:
                    conn.execute(sa.update(lessons_table).where(lessons_table.c.id == row.id)
                                 .values(hits=row.hits + 1, last_seen=now))
                    return
            conn.execute(sa.insert(lessons_table).values(
                tenant_id=str(tenant_id), connection_id=str(connection_id), text=text,
                text_norm=norm, source=source, hits=1, created_at=now, last_seen=now))
            overflow = len(existing) + 1 - self._max_lessons
            if overflow > 0:      # evict the least useful: fewest hits, then least recent
                victims = sorted(existing, key=lambda r: (r.hits, r.last_seen, r.id))[:overflow]
                conn.execute(sa.delete(lessons_table).where(
                    lessons_table.c.id.in_([v.id for v in victims])))

    def lessons(self, tenant_id, connection_id, *, limit=8) -> list[str]:
        """The most useful lessons: most hits first, then most recently seen."""
        limit = max(0, min(int(limit), self._max_lessons))
        if not limit:
            return []
        stmt = (
            sa.select(lessons_table.c.text)
            .where(lessons_table.c.tenant_id == str(tenant_id),
                   lessons_table.c.connection_id == str(connection_id))
            .order_by(lessons_table.c.hits.desc(), lessons_table.c.last_seen.desc(),
                      lessons_table.c.id.desc())
            .limit(limit)
        )
        with self._db().connect() as conn:
            return [row.text for row in conn.execute(stmt)]

    # ------------------------------------------------------------------ lifecycle
    def delete_connection(self, tenant_id, connection_id) -> None:
        """Forget everything stored for one connection of this tenant."""
        with self._db().begin() as conn:
            for table in (query_log, lessons_table):
                conn.execute(sa.delete(table).where(
                    table.c.tenant_id == str(tenant_id),
                    table.c.connection_id == str(connection_id)))


def _reverse(stamp: str) -> tuple:
    """Sort key that puts later ISO timestamps first."""
    return tuple(-ord(ch) for ch in stamp or "")


_service: MemoryService | None = None
_service_lock = threading.Lock()


def get_memory_service() -> MemoryService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = MemoryService()
    return _service


def set_memory_service(service: MemoryService | None) -> None:
    global _service
    with _service_lock:
        _service = service
