"""Query memory: isolation, feedback flows, example ranking, lessons."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

import memory.service as memory_module
from catalog.retrieval import tokenize
from memory.service import (
    MAX_LESSON_CHARS,
    MAX_QUESTION_CHARS,
    Example,
    MemoryService,
    get_memory_service,
    lessons_table,
    query_log,
    set_memory_service,
)

T, C = "tenant-a", "conn-1"
SQL = "SELECT COUNT(*) FROM orders WHERE status = 'Shipped'"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class FakeEmbedder:
    name = "fake"
    SAME = {"client": "customer", "buyer": "customer", "revenu": "sale"}    # stems

    def __init__(self):
        self.calls = 0

    def embed_query(self, text):
        self.calls += 1
        vec = [0.0] * 64
        for token in tokenize(text):
            token = self.SAME.get(token, token)
            vec[int(hashlib.md5(token.encode()).hexdigest(), 16) % 64] += 1.0
        return vec

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]


@pytest.fixture
def engine():
    eng = sa.create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    yield eng
    eng.dispose()


@pytest.fixture
def memory(engine):
    return MemoryService(engine, embedder=None, clock=Clock())


def log(memory, question, sql=SQL, *, tenant=T, conn=C, status="completed", **kwargs):
    return memory.log_query(tenant, conn, question=question, sql=sql, status=status, **kwargs)


def rows(engine, table=query_log):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.select(table)).mappings()]


# ------------------------------------------------------------------ logging
def test_log_query_stores_bookkeeping_only(memory, engine):
    query_id = log(memory, "how many shipped orders", tables=("orders",), thread_id="th-1",
                   db_type="sqlite")
    failed = log(memory, "broken question", None, status="failed", error_type="syntax_error")
    assert query_id != failed and len(query_id) == 32
    stored = {r["id"]: r for r in rows(engine)}
    row = stored[query_id]
    assert row["question"] == "how many shipped orders" and row["sql"] == SQL
    assert row["status"] == "completed" and row["tables"] == '["orders"]'
    assert row["thread_id"] == "th-1" and row["tenant_id"] == T and row["connection_id"] == C
    assert row["created_at"].startswith("2026-01-01T00:00:01")
    assert row["embedding"] is None and row["verified"] == 0
    assert stored[failed]["error_type"] == "syntax_error" and stored[failed]["sql"] is None
    assert not {"rows", "result", "results"} & set(c.name for c in query_log.columns)


def test_text_is_length_capped(memory, engine):
    log(memory, "q" * 50000, "SELECT 1 -- " + "x" * 50000)
    row = rows(engine)[0]
    assert len(row["question"]) == MAX_QUESTION_CHARS and len(row["sql"]) <= 8000


def test_embedding_stored_when_an_embedder_is_available(engine):
    embedder = FakeEmbedder()
    memory = MemoryService(engine, embedder=embedder, clock=Clock())
    log(memory, "how many shipped orders")
    log(memory, "something that failed", None, status="failed")
    stored = rows(engine)
    assert stored[0]["embedding"].startswith("[") and stored[0]["embedder_name"] == "fake"
    assert stored[1]["embedding"] is None and embedder.calls == 1


# ------------------------------------------------------------------ isolation
def test_examples_are_scoped_to_tenant_and_connection(memory):
    question = "how many shipped orders last month"
    log(memory, question, "SELECT 1")
    log(memory, question, "SELECT 2", conn="conn-2")
    log(memory, question, "SELECT 3", tenant="tenant-b")
    assert [e.sql for e in memory.similar_examples(T, C, question)] == ["SELECT 1"]
    assert [e.sql for e in memory.similar_examples(T, "conn-2", question)] == ["SELECT 2"]
    assert [e.sql for e in memory.similar_examples("tenant-b", C, question)] == ["SELECT 3"]
    assert memory.similar_examples("tenant-c", C, question) == []


def test_lessons_are_scoped_to_tenant_and_connection(memory):
    memory.add_lesson(T, C, "revenue excludes cancelled orders", source="correction")
    memory.add_lesson(T, "conn-2", "dates are stored in UTC", source="feedback")
    memory.add_lesson("tenant-b", C, "use net_amount, not gross", source="feedback")
    assert memory.lessons(T, C) == ["revenue excludes cancelled orders"]
    assert memory.lessons(T, "conn-2") == ["dates are stored in UTC"]
    assert memory.lessons("tenant-b", C) == ["use net_amount, not gross"]
    assert memory.lessons("tenant-b", "conn-2") == []


def test_delete_connection_only_touches_that_connection(memory, engine):
    question = "how many shipped orders"
    for tenant, conn in ((T, C), (T, "conn-2"), ("tenant-b", C)):
        log(memory, question, tenant=tenant, conn=conn)
        memory.add_lesson(tenant, conn, "a lesson", source="feedback")
    memory.delete_connection(T, C)
    assert memory.similar_examples(T, C, question) == [] and memory.lessons(T, C) == []
    assert len(memory.similar_examples(T, "conn-2", question)) == 1
    assert memory.lessons("tenant-b", C) == ["a lesson"]
    assert len(rows(engine)) == 2 and len(rows(engine, lessons_table)) == 2
    memory.delete_connection(T, "never-existed")


# ------------------------------------------------------------------ feedback
def test_thumbs_up_verifies_the_query(memory):
    question = "how many shipped orders"
    query_id = log(memory, question)
    assert memory.similar_examples(T, C, question)[0].verified is False
    memory.record_feedback(T, query_id, rating="up", comment="exactly right")
    example = memory.similar_examples(T, C, question)[0]
    assert example == Example(question=question, sql=SQL, verified=True, score=1.0)
    assert memory.lessons(T, C) == []                  # praise is not a lesson


def test_thumbs_down_with_correction(memory, engine):
    question = "how many shipped orders"
    query_id = log(memory, question, "SELECT COUNT(*) FROM orders", db_type="sqlite")
    fixed = "SELECT COUNT(*) FROM orders WHERE status = 'Shipped'"
    memory.record_feedback(T, query_id, rating="down", comment="Filter on status = 'Shipped'",
                           corrected_sql=fixed + " ;")
    examples = memory.similar_examples(T, C, question)
    assert [(e.sql.rstrip(" ;"), e.verified) for e in examples] == [(fixed, True)]
    assert memory.lessons(T, C) == ["Filter on status = 'Shipped'"]
    original = next(r for r in rows(engine) if r["id"] == query_id)
    assert original["rating"] == "down" and original["excluded"] == 1
    # a second correction replaces the first
    memory.record_feedback(T, query_id, rating="down", corrected_sql="SELECT 42")
    assert [e.sql for e in memory.similar_examples(T, C, question)] == ["SELECT 42"]
    assert len(rows(engine)) == 2


def test_thumbs_down_without_correction_removes_the_example(memory):
    question = "how many shipped orders"
    query_id = log(memory, question)
    memory.record_feedback(T, query_id, rating="down")
    assert memory.similar_examples(T, C, question) == [] and memory.lessons(T, C) == []
    memory.record_feedback(T, query_id, rating="up")           # changed their mind
    assert memory.similar_examples(T, C, question)[0].verified is True


@pytest.mark.parametrize("bad", [
    "DROP TABLE orders",
    "DELETE FROM orders",
    "SELECT 1; DELETE FROM orders",
    "UPDATE orders SET status = 'x'",
    "this is not sql at all (",
])
def test_unsafe_correction_is_rejected_and_changes_nothing(memory, engine, bad):
    question = "how many shipped orders"
    query_id = log(memory, question)
    with pytest.raises(ValueError):
        memory.record_feedback(T, query_id, rating="down", comment="try this",
                               corrected_sql=bad)
    assert [e.sql for e in memory.similar_examples(T, C, question)] == [SQL]
    assert memory.lessons(T, C) == [] and len(rows(engine)) == 1
    assert rows(engine)[0]["rating"] is None


def test_scan_uses_the_connection_dialect(engine, mocker):
    seen = []
    scan = mocker.patch("database.safety.scan_sql",
                        side_effect=lambda sql, dialect=None: seen.append(dialect)
                        or {"safe": True, "statement_type": "SELECT", "reason": None})
    memory = MemoryService(engine, embedder=None, dialect_resolver=lambda t, c: "postgresql")
    first = log(memory, "q one", db_type="mysql")
    second = log(memory, "q two")
    memory.record_feedback(T, first, rating="down", corrected_sql="SELECT 1")
    memory.record_feedback(T, second, rating="down", corrected_sql="SELECT 2")
    memory.record_feedback(T, second, rating="down", corrected_sql="SELECT 3", dialect="duckdb")
    assert seen == ["mysql", "postgresql", "duckdb"] and scan.call_count == 3


def test_feedback_on_a_foreign_or_unknown_query(memory, engine):
    query_id = log(memory, "how many shipped orders")
    for tenant, target in (("tenant-b", query_id), (T, "no-such-id"), ("", query_id)):
        with pytest.raises(LookupError):
            memory.record_feedback(tenant, target, rating="up")
        with pytest.raises(LookupError):
            memory.record_feedback(tenant, target, rating="down", comment="x",
                                   corrected_sql="SELECT 1")
    assert rows(engine)[0]["rating"] is None and rows(engine)[0]["verified"] == 0
    assert memory.lessons("tenant-b", C) == []
    with pytest.raises(ValueError):
        memory.record_feedback(T, query_id, rating="meh")


# ------------------------------------------------------------------ examples
def test_only_completed_queries_with_sql_are_examples(memory):
    question = "how many shipped orders"
    log(memory, question, "SELECT bad", status="failed", error_type="execution_error")
    log(memory, question, None)
    log(memory, question, "   ")
    log(memory, question, "SELECT blocked", status="blocked")
    assert memory.similar_examples(T, C, question) == []
    log(memory, question, "SELECT good")
    assert [e.sql for e in memory.similar_examples(T, C, question)] == ["SELECT good"]


def test_ranking_threshold_and_cap(memory):
    log(memory, "total revenue by product category", "SELECT a")
    log(memory, "total revenue by product category in 2024", "SELECT b")
    log(memory, "revenue by category", "SELECT c")
    log(memory, "list all physicians in cardiology", "SELECT d")
    log(memory, "how many patients were admitted", "SELECT e")
    found = memory.similar_examples(T, C, "total revenue by product category", k=5)
    assert [e.sql for e in found] == ["SELECT a", "SELECT b", "SELECT c"]
    assert found[0].score == 1.0 and found[0].score > found[1].score > found[2].score > 0.3
    assert [e.sql for e in memory.similar_examples(T, C, "total revenue by product category",
                                                   k=1)] == ["SELECT a"]
    assert memory.similar_examples(T, C, "weather forecast for tomorrow") == []
    assert memory.similar_examples(T, C, "") == []
    assert memory.similar_examples(T, C, "total revenue by product category", k=0) == []


def test_verified_examples_come_first(memory):
    log(memory, "total revenue by product category", "SELECT exact_unverified")
    verified = log(memory, "revenue by product category per year", "SELECT verified")
    memory.record_feedback(T, verified, rating="up")
    found = memory.similar_examples(T, C, "total revenue by product category")
    assert [(e.sql, e.verified) for e in found] == [("SELECT verified", True),
                                                    ("SELECT exact_unverified", False)]
    assert found[0].score < found[1].score


def test_duplicate_sql_is_returned_once_and_newest_wins_ties(memory):
    question = "how many shipped orders"
    log(memory, question, "SELECT COUNT(*) FROM orders")
    log(memory, question, "select  count(*)\nFROM orders;")
    log(memory, question, "SELECT COUNT(1) FROM orders")
    found = memory.similar_examples(T, C, question, k=5)
    assert [e.sql for e in found] == ["SELECT COUNT(1) FROM orders",
                                      "select  count(*)\nFROM orders;"]


def test_cosine_similarity_when_embeddings_exist(engine):
    embedder = FakeEmbedder()
    memory = MemoryService(engine, embedder=embedder, clock=Clock())
    log(memory, "revenue per customer", "SELECT a")
    log(memory, "list all physicians in cardiology", "SELECT d")
    # no shared tokens with the stored question, but the embedder knows the synonyms
    found = memory.similar_examples(T, C, "sales per client")
    assert [e.sql for e in found] == ["SELECT a"] and found[0].score > 0.9
    lexical = MemoryService(engine, embedder=None)
    assert lexical.similar_examples(T, C, "sales per client") == []
    assert [e.sql for e in lexical.similar_examples(T, C, "revenue per customer")] == ["SELECT a"]


def test_embedder_is_not_called_when_nothing_is_embedded(engine):
    log(MemoryService(engine, embedder=None), "revenue per customer", "SELECT a")
    embedder = FakeEmbedder()
    memory = MemoryService(engine, embedder=embedder)
    assert len(memory.similar_examples(T, C, "revenue per customer")) == 1
    assert embedder.calls == 0

    class Broken(FakeEmbedder):
        def embed_query(self, text):
            raise RuntimeError("down")

    broken = MemoryService(engine, embedder=Broken())
    log(broken, "orders per customer", "SELECT b")              # must not raise
    assert len(broken.similar_examples(T, C, "orders per customer")) == 1


# ------------------------------------------------------------------ lessons
def test_lessons_dedupe_and_count_hits(memory, engine):
    memory.add_lesson(T, C, "Revenue excludes cancelled orders.", source="correction")
    memory.add_lesson(T, C, "dates are stored in UTC", source="feedback")
    memory.add_lesson(T, C, "  revenue EXCLUDES cancelled   orders ", source="feedback")
    memory.add_lesson(T, C, "Revenue excludes all cancelled orders", source="feedback")
    stored = rows(engine, lessons_table)
    assert len(stored) == 2
    first = next(r for r in stored if r["text"].startswith("Revenue"))
    assert first["hits"] == 3 and first["source"] == "correction"
    assert first["last_seen"] > first["created_at"]
    assert memory.lessons(T, C) == ["Revenue excludes cancelled orders.",
                                    "dates are stored in UTC"]


def test_lessons_order_by_hits_then_recency_and_limit(memory):
    for text in ("alpha rule one", "beta rule two", "gamma rule three", "delta rule four"):
        memory.add_lesson(T, C, text, source="feedback")
    memory.add_lesson(T, C, "beta rule two", source="feedback")
    assert memory.lessons(T, C) == ["beta rule two", "delta rule four", "gamma rule three",
                                    "alpha rule one"]
    assert memory.lessons(T, C, limit=2) == ["beta rule two", "delta rule four"]
    assert memory.lessons(T, C, limit=0) == []


def test_lesson_cap_evicts_the_least_useful(engine):
    memory = MemoryService(engine, embedder=None, clock=Clock(), max_lessons=5)
    for i in range(5):
        memory.add_lesson(T, C, f"lesson number {i} about topic{i}", source="feedback")
    memory.add_lesson(T, C, "lesson number 0 about topic0", source="feedback")     # 2 hits
    memory.add_lesson(T, C, "brand new insight", source="correction")
    kept = memory.lessons(T, C, limit=50)
    assert len(kept) == 5 and kept[0] == "lesson number 0 about topic0"
    assert "brand new insight" in kept
    assert "lesson number 1 about topic1" not in kept          # oldest single-hit lesson
    memory.add_lesson(T, "conn-2", "other connection", source="feedback")
    assert len(memory.lessons(T, C, limit=50)) == 5


def test_default_cap_is_fifty(memory):
    for i in range(60):
        memory.add_lesson(T, C, f"w{i}a w{i}b w{i}c", source="feedback")
    assert len(memory.lessons(T, C, limit=1000)) == 50


def test_lesson_text_is_capped_and_validated(memory, engine):
    memory.add_lesson(T, C, "word " * 1000, source="feedback")
    assert len(rows(engine, lessons_table)[0]["text"]) <= MAX_LESSON_CHARS
    memory.add_lesson(T, C, "   ", source="feedback")
    memory.add_lesson(T, C, None, source="feedback")
    assert len(rows(engine, lessons_table)) == 1
    with pytest.raises(ValueError):
        memory.add_lesson(T, C, "x y z", source="system")


# ------------------------------------------------------------------ singleton
def test_singleton_uses_the_metadata_engine(engine, monkeypatch):
    import database.metadata as metadata

    monkeypatch.setattr(metadata, "get_engine", lambda: engine)
    try:
        set_memory_service(None)
        service = get_memory_service()
        assert get_memory_service() is service
        service._embedder = None
        query_id = service.log_query(T, C, question="how many orders", sql="SELECT 1",
                                     status="completed")
        assert rows(engine)[0]["id"] == query_id
        custom = MemoryService(engine, embedder=None)
        set_memory_service(custom)
        assert get_memory_service() is custom
    finally:
        set_memory_service(None)
    assert memory_module._service is None
