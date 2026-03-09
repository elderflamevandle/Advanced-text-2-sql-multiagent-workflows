"""
Tests for BaseConnector abstract class and schema_utils types (Task 1).
"""
import pytest


def test_base_connector_imports():
    """BaseConnector and schema types import cleanly."""
    from database.connectors.base import BaseConnector
    from database.schema_utils import SchemaTable, ColumnInfo, FKInfo
    assert BaseConnector is not None
    assert SchemaTable is not None
    assert ColumnInfo is not None
    assert FKInfo is not None


def test_base_connector_is_abstract():
    """BaseConnector cannot be instantiated directly."""
    from database.connectors.base import BaseConnector
    with pytest.raises(TypeError):
        BaseConnector()


def test_base_connector_abstract_methods():
    """BaseConnector defines the required 5 abstract methods."""
    from database.connectors.base import BaseConnector
    abstract_methods = getattr(BaseConnector, '__abstractmethods__', set())
    required = {"connect", "get_schema", "execute_query", "close", "test_connection"}
    assert required.issubset(abstract_methods), f"Missing abstract methods: {required - abstract_methods}"


def test_schema_utils_typed_dicts():
    """ColumnInfo, FKInfo, SchemaTable have the expected keys."""
    from database.schema_utils import ColumnInfo, FKInfo, SchemaTable
    col: ColumnInfo = {"name": "id", "type": "INTEGER", "nullable": False}
    fk: FKInfo = {"column": "AlbumId", "references_table": "Album", "references_column": "AlbumId"}
    table: SchemaTable = {
        "columns": [col],
        "primary_keys": ["id"],
        "foreign_keys": [fk],
        "sample_rows": [{"id": 1}],
    }
    assert col["name"] == "id"
    assert fk["references_table"] == "Album"
    assert table["primary_keys"] == ["id"]


def test_no_db_driver_at_module_level():
    """base.py and schema_utils.py do not import any DB driver."""
    import importlib
    import sys
    # Remove cached modules to force fresh import
    for mod in ["database.connectors.base", "database.schema_utils"]:
        sys.modules.pop(mod, None)

    # Block db drivers temporarily
    import unittest.mock as mock
    blocked = {"duckdb", "psycopg2", "mysql", "mysql.connector", "pyodbc"}
    with mock.patch.dict("sys.modules", {k: None for k in blocked}):
        from database.connectors.base import BaseConnector
        from database.schema_utils import SchemaTable
    assert True, "Imports succeeded without any DB driver"
