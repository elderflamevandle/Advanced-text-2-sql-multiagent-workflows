"""Prompt blocks shared by the planner, the SQL generator and the correction nodes.

``schema_prompt`` prefers the catalog's rendered context (``state["schema_context"]``, set
by the schema linker when a catalog exists) and otherwise renders the legacy schema dict
with columns, primary keys and foreign keys. The other helpers return ``""`` when there is
nothing to say, so prompts stay unchanged for runs without a catalog or query memory.
"""
from __future__ import annotations

from typing import Any, Mapping

MAX_EXAMPLES = 3
MAX_EXAMPLE_CHARS = 1200
MAX_LESSONS = 8
MAX_LESSON_CHARS = 400
MAX_JOIN_HINTS = 30

SCHEMA_RULES = (
    "Schema rules:\n"
    "- Use only the tables and columns listed in the schema context. Never invent names.\n"
    "- Prefer the listed joins when tables have to be combined.\n"
    "- When the schema context lists values for a column, use them verbatim "
    "(same spelling and letter case) in filters."
)


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _fk_text(table: str, fk: Any) -> str | None:
    """One foreign key as "T.a -> U.b". Accepts the shapes the schema extractors produce."""
    if not isinstance(fk, Mapping):
        return None
    cols = fk.get("constrained_columns") or fk.get("columns") or fk.get("column") or []
    ref_table = (fk.get("references_table") or fk.get("referred_table")
                 or fk.get("ref_table") or fk.get("table") or "")
    ref_cols = (fk.get("references_column") or fk.get("referred_columns")
                or fk.get("ref_columns") or fk.get("ref_column") or [])
    if isinstance(cols, str):
        cols = [cols]
    if isinstance(ref_cols, str):
        ref_cols = [ref_cols]
    if not cols or not ref_table:
        return None
    left = ", ".join(f"{table}.{c}" for c in cols)
    right = ", ".join(f"{ref_table}.{c}" for c in ref_cols) if ref_cols else str(ref_table)
    return f"{left} -> {right}"


def render_legacy_schema(schema: Mapping | None) -> str:
    """Tables with column names and types, plus primary and foreign keys when known."""
    if not schema:
        return "(no schema provided)"
    lines = []
    for table, info in schema.items():
        info = info if isinstance(info, Mapping) else {}
        cols = info.get("columns") or []
        col_strs = [f"{c.get('name', '?')} {c.get('type', '')}" for c in cols
                    if isinstance(c, Mapping)]
        lines.append(f"Table {table}: {', '.join(col_strs)}")
        primary = [str(k) for k in (info.get("primary_keys") or [])]
        if primary:
            lines.append(f"  Primary key: {', '.join(primary)}")
        fks = [t for t in (_fk_text(str(table), fk) for fk in (info.get("foreign_keys") or []))
               if t]
        if fks:
            lines.append(f"  Foreign keys: {'; '.join(fks)}")
    return "\n".join(lines)


def schema_prompt(state: Mapping) -> str:
    """Schema text for LLM prompts: the catalog context when present, else the legacy dict."""
    context = state.get("schema_context")
    if isinstance(context, str) and context.strip():
        return context
    return render_legacy_schema(state.get("schema") or {})


def join_hints_prompt(state: Mapping) -> str:
    """Known joins between the selected tables ("" when none)."""
    lines = []
    for hint in (state.get("join_hints") or [])[:MAX_JOIN_HINTS]:
        if not isinstance(hint, Mapping):
            continue
        on = _one_line(hint.get("on"))
        if not on:
            continue
        kind = _one_line(hint.get("kind"))
        lines.append(f"- {hint.get('from')} -> {hint.get('to')}: {on}"
                     + (f" ({kind})" if kind else ""))
    if not lines:
        return ""
    return "Known joins (prefer these):\n" + "\n".join(lines)


def _example_fields(example: Any) -> tuple[str, str, bool]:
    if isinstance(example, Mapping):
        return (_one_line(example.get("question")), str(example.get("sql") or "").strip(),
                bool(example.get("verified")))
    return (_one_line(getattr(example, "question", "")),
            str(getattr(example, "sql", "") or "").strip(),
            bool(getattr(example, "verified", False)))


def examples_prompt(state: Mapping) -> str:
    """Few-shot block of earlier questions with their SQL, verified ones first ("" if none)."""
    examples = [_example_fields(e) for e in (state.get("examples") or [])]
    examples = [e for e in examples if e[0] and e[1]]
    if not examples:
        return ""
    examples.sort(key=lambda e: not e[2])  # stable: verified first, original order kept
    blocks = []
    for question, sql, verified in examples[:MAX_EXAMPLES]:
        label = " (verified by a user)" if verified else ""
        blocks.append(f"Question{label}: {_clip(question, 300)}\n"
                      f"SQL: {_clip(sql, MAX_EXAMPLE_CHARS)}")
    return ("Earlier questions on this database and the SQL that answered them "
            "(adapt them; do not copy blindly):\n" + "\n\n".join(blocks))


def lessons_prompt(state: Mapping) -> str:
    """Notes learned from earlier corrections and feedback on this database ("" if none)."""
    lessons = [_one_line(item) for item in (state.get("lessons") or [])]
    lessons = [_clip(item, MAX_LESSON_CHARS) for item in lessons if item][:MAX_LESSONS]
    if not lessons:
        return ""
    return ("Lessons from earlier queries on this database:\n"
            + "\n".join(f"- {item}" for item in lessons))


def previous_attempt_prompt(state: Mapping) -> str:
    """After the tables were selected again: the SQL that failed and why ("" otherwise)."""
    history = state.get("sql_history") or []
    if not state.get("reselect_count") or not history:
        return ""
    last = history[-1] if isinstance(history[-1], Mapping) else {}
    error = last.get("error")
    message = error.get("message") if isinstance(error, Mapping) else error
    sql = str(last.get("sql") or "").strip()
    if not sql and not message:
        return ""
    return ("A previous attempt failed and the tables were selected again.\n"
            f"Failed SQL: {_clip(sql, MAX_EXAMPLE_CHARS)}\n"
            f"Error: {_clip(_one_line(message), 300)}")


def extra_context(state: Mapping, *, examples: bool = True, lessons: bool = True,
                  joins: bool = True, previous_attempt: bool = False) -> str:
    """The non-empty optional blocks, each preceded by a blank line ("" when all empty)."""
    blocks = [
        join_hints_prompt(state) if joins else "",
        examples_prompt(state) if examples else "",
        lessons_prompt(state) if lessons else "",
        previous_attempt_prompt(state) if previous_attempt else "",
    ]
    return "".join(f"\n\n{block}" for block in blocks if block)
