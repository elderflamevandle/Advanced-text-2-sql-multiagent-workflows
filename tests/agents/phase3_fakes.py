"""In-memory fakes of catalog.service.CatalogService and memory.service.MemoryService
(signatures from docs/phase3-contracts.md). Shared by tests/agents, tests/graph, tests/api."""
from dataclasses import dataclass, field


@dataclass
class FakeContext:
    """Same attributes as catalog.models.SchemaContext."""
    tables: list
    schema: dict
    prompt_text: str
    join_hints: list = field(default_factory=list)
    value_hints: list = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)


@dataclass
class FakeStatus:
    state: str = "missing"
    table_count: int = 0
    progress: float = 0.0
    error: str | None = None


@dataclass
class FakeExample:
    question: str
    sql: str
    verified: bool = False
    score: float = 0.5


def items_context(tables=("items",)):
    schema = {t: {"columns": [{"name": "id", "type": "INTEGER"},
                              {"name": "owner", "type": "TEXT"}],
                  "primary_keys": ["id"], "foreign_keys": []} for t in tables}
    text = "\n".join(f"CATALOG TABLE {t} (id INTEGER, owner TEXT) -- values: owner in ('a','b')"
                     for t in tables)
    return FakeContext(tables=list(tables), schema=schema, prompt_text=text,
                       join_hints=[{"from": "items", "to": "owners",
                                    "on": "items.owner = owners.name", "kind": "fk"}])


class FakeCatalog:
    def __init__(self, context=None, error=None):
        self.context = context          # FakeContext, None, or callable(**call) -> context
        self.error = error
        self.select_calls = []
        self.builds = []
        self.deleted = []
        self.descriptions = {}
        self.tables = {"items": ["id", "owner"]}
        self.state = "missing"

    def select_context(self, tenant_id, connection_id, question, *, widen=False,
                       must_include=()):
        call = {"tenant_id": tenant_id, "connection_id": connection_id, "question": question,
                "widen": widen, "must_include": tuple(must_include)}
        self.select_calls.append(call)
        if self.error is not None:
            raise self.error
        return self.context(**call) if callable(self.context) else self.context

    def start_build(self, tenant_id, connection_id, *, force=False):
        if self.error is not None:
            raise self.error
        self.builds.append((tenant_id, connection_id, force))
        self.state = "building"
        return FakeStatus(state="building")

    def status(self, tenant_id, connection_id):
        return FakeStatus(state=self.state, table_count=len(self.tables))

    def describe(self, tenant_id, connection_id):
        return {"tables": [{"name": t, "description": self.descriptions.get((t, None)),
                            "columns": [{"name": c,
                                         "description": self.descriptions.get((t, c))}
                                        for c in cols]}
                           for t, cols in self.tables.items()]}

    def set_description(self, tenant_id, connection_id, table, column, text):
        if table not in self.tables or (column is not None
                                        and column not in self.tables[table]):
            raise KeyError(table if column is None else f"{table}.{column}")
        self.descriptions[(table, column)] = text

    def delete(self, tenant_id, connection_id):
        self.deleted.append((tenant_id, connection_id))


class FakeMemory:
    def __init__(self, examples=(), lessons=(), error=None):
        self._examples = list(examples)
        self._lessons = list(lessons)
        self.error = error
        self.logged = []                # kwargs of each log_query call
        self.added_lessons = []
        self.feedback = []
        self.deleted = []
        self.example_calls = []
        self.lesson_calls = []

    def _check(self):
        if self.error is not None:
            raise self.error

    def log_query(self, tenant_id, connection_id, *, question, sql, status, error_type=None,
                  tables=(), thread_id=None, db_type=None):
        self._check()
        query_id = f"q{len(self.logged) + 1}"
        self.logged.append({"tenant_id": tenant_id, "connection_id": connection_id,
                            "question": question, "sql": sql, "status": status,
                            "error_type": error_type, "tables": tuple(tables),
                            "thread_id": thread_id, "query_id": query_id})
        return query_id

    def record_feedback(self, tenant_id, query_id, *, rating, comment=None,
                        corrected_sql=None):
        self._check()
        owner = next((q for q in self.logged if q["query_id"] == query_id), None)
        if owner is None or owner["tenant_id"] != tenant_id:
            raise LookupError(query_id)
        if corrected_sql and not corrected_sql.lstrip().lower().startswith(("select", "with")):
            raise ValueError("unsafe sql")
        self.feedback.append({"tenant_id": tenant_id, "query_id": query_id, "rating": rating,
                              "comment": comment, "corrected_sql": corrected_sql})

    def similar_examples(self, tenant_id, connection_id, question, *, k=3):
        self.example_calls.append((tenant_id, connection_id, question, k))
        self._check()
        return list(self._examples)[:k]

    def add_lesson(self, tenant_id, connection_id, text, *, source):
        self._check()
        self.added_lessons.append({"tenant_id": tenant_id, "connection_id": connection_id,
                                   "text": text, "source": source})

    def lessons(self, tenant_id, connection_id, *, limit=8):
        self.lesson_calls.append((tenant_id, connection_id, limit))
        self._check()
        return list(self._lessons)[:limit]

    def delete_connection(self, tenant_id, connection_id):
        self.deleted.append((tenant_id, connection_id))
