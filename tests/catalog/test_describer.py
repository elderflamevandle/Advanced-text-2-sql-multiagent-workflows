import asyncio
import json

import pytest

from catalog import describer
from catalog.describer import build_prompt, describe_tables, parse_response
from llm.errors import LLMUnavailableError
from tests.catalog.conftest_build import FakeLLM, good_answer, sample_catalog, table_of


def test_good_json_describes_tables_and_columns():
    catalog = sample_catalog()
    catalog.tables["customers"].description = None
    llm = FakeLLM()
    assert describe_tables(catalog, ["customers", "orders"], llm=llm) == 2
    assert len(llm.prompts) == 2                       # one call per table
    orders = catalog.tables["orders"]
    assert orders.description == "Rows of orders." and orders.description_source == "llm"
    assert orders.column("customer_id").description == "The customer_id of orders."
    assert orders.column("customer_id").description_source == "llm"


def test_fenced_json_and_chatter():
    catalog = sample_catalog()
    llm = FakeLLM(lambda p: "Sure, here it is:\n```json\n" + good_answer(p) + "\n```\nBye")
    assert describe_tables(catalog, ["orders"], llm=llm) == 1
    assert catalog.tables["orders"].description == "Rows of orders."


def test_parse_response_variants():
    assert parse_response('{"table_description": "T", "columns": {"a": "A"}}') == {
        "table_description": "T", "columns": {"a": "A"}}
    assert parse_response('noise {"table_description": "T"} trailing')["columns"] == {}
    listed = parse_response(json.dumps(
        {"description": "T", "columns": [{"name": "a", "description": "A"}]}))
    assert listed == {"table_description": "T", "columns": {"a": "A"}}
    assert parse_response([{"type": "text", "text": '{"table_description": "T"}'}])
    for bad in ("", None, "no json here", "[1, 2]", "{broken", '{"other": 1}', 42):
        assert parse_response(bad) is None


def test_garbage_answers_change_nothing_and_stop_early():
    catalog = sample_catalog()
    for i in range(6):
        catalog.tables[f"t{i}"] = type(catalog.tables["orders"])(name=f"t{i}")
    llm = FakeLLM("I cannot help with that.")
    names = ["orders"] + [f"t{i}" for i in range(6)]
    assert describe_tables(catalog, names, llm=llm) == 0
    assert catalog.tables["orders"].description is None
    assert len(llm.prompts) == describer.MAX_CONSECUTIVE_BAD_ANSWERS


def test_one_bad_answer_does_not_stop_the_rest():
    catalog = sample_catalog()
    catalog.tables["customers"].description = None
    llm = FakeLLM(lambda p: "garbage" if table_of(p) == "customers" else good_answer(p))
    assert describe_tables(catalog, ["customers", "orders"], llm=llm) == 1
    assert catalog.tables["customers"].description is None
    assert catalog.tables["orders"].description


def test_outage_stops_cleanly_and_keeps_what_was_done():
    catalog = sample_catalog()
    catalog.tables["customers"].description = None
    llm = FakeLLM(lambda p: good_answer(p) if table_of(p) == "customers"
                  else LLMUnavailableError("all providers failed"))
    assert describe_tables(catalog, ["customers", "orders"], llm=llm) == 1
    assert catalog.tables["customers"].description == "Rows of customers."
    assert catalog.tables["orders"].description is None

    llm = FakeLLM(RuntimeError("boom"))
    assert describe_tables(sample_catalog(), ["orders"], llm=llm) == 0


def test_default_llm_failure_is_contained(monkeypatch):
    import llm.fallback as fallback

    def broken(node, state=None):
        assert node == "catalog_describer"
        raise RuntimeError("no config")

    monkeypatch.setattr(fallback, "get_llm", broken)
    assert describe_tables(sample_catalog(), ["orders"]) == 0


def test_user_descriptions_are_untouched():
    catalog = sample_catalog()
    customers = catalog.tables["customers"]
    customers.description, customers.description_source = "Our buyers.", "user"
    orders = catalog.tables["orders"]
    orders.column("customer_id").description = "Who bought."
    orders.column("customer_id").description_source = "user"
    llm = FakeLLM()
    assert describe_tables(catalog, ["customers", "orders", "unknown"], llm=llm) == 1
    assert [table_of(p) for p in llm.prompts] == ["orders"]     # user table not even sent
    assert customers.description == "Our buyers."
    assert orders.column("customer_id").description == "Who bought."
    assert orders.column("customer_id").description_source == "user"
    assert orders.column("id").description_source == "llm"
    assert "documented as: Who bought." in llm.prompts[0]


def test_prompt_content_and_no_pii_values():
    catalog = sample_catalog()
    customers = catalog.tables["customers"]
    customers.description = None
    email = customers.column("email")
    email.sample_values = ["leak@example.org"]          # must be ignored even if present
    email.enum_values = ["leak@example.org"]
    llm = FakeLLM()
    describe_tables(catalog, ["customers"], llm=llm)
    prompt = llm.prompts[0]
    assert "Table: customers" in prompt and "Primary key: id" in prompt
    assert "- segment (TEXT)" in prompt and '"retail", "wholesale"' in prompt
    assert "orders.customer_id = customers.id" in prompt
    assert "- email (TEXT)" in prompt and "values withheld" in prompt
    assert "leak@example.org" not in prompt and "leak@example.org" not in llm.systems[0]
    assert "JSON" in llm.systems[0]


def test_columns_per_prompt_are_capped(monkeypatch):
    monkeypatch.setattr(describer, "MAX_COLUMNS_PER_PROMPT", 2)
    catalog = sample_catalog()
    prompt = build_prompt(catalog, catalog.tables["customers"])
    assert "- id" in prompt and "- segment" in prompt and "- email" not in prompt
    assert "1 more columns" in prompt


def test_long_descriptions_are_clipped():
    catalog = sample_catalog()
    llm = FakeLLM(json.dumps({"table_description": "x " * 1000, "columns": {}}))
    describe_tables(catalog, ["orders"], llm=llm)
    assert len(catalog.tables["orders"].description) <= describer.MAX_DESCRIPTION_CHARS


def test_works_inside_a_running_event_loop():
    catalog = sample_catalog()
    seen = []

    async def main():
        return describe_tables(catalog, ["orders"], llm=FakeLLM(), on_table=seen.append)

    assert asyncio.run(main()) == 1
    assert seen == ["orders"]


def test_run_sync_propagates_errors_from_the_worker_thread():
    async def boom():
        raise ValueError("x")

    async def main():
        with pytest.raises(ValueError):
            describer.run_sync(boom())

    asyncio.run(main())
