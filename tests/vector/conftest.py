"""Shared fixtures for vector retrieval tests."""
import pytest
from database.schema_utils import ColumnInfo, FKInfo, SchemaTable


@pytest.fixture
def sample_schema() -> dict:
    """Returns a dict[str, SchemaTable] with 3 Chinook-like tables."""
    return {
        "Customer": SchemaTable(
            columns=[
                ColumnInfo(name="CustomerId", type="INTEGER", nullable=False),
                ColumnInfo(name="FirstName", type="NVARCHAR", nullable=False),
                ColumnInfo(name="LastName", type="NVARCHAR", nullable=False),
                ColumnInfo(name="Email", type="NVARCHAR", nullable=False),
            ],
            primary_keys=["CustomerId"],
            foreign_keys=[],
            sample_rows=[
                {"CustomerId": 1, "FirstName": "Luís", "LastName": "Gonçalves", "Email": "luisg@embraer.com.br"},
                {"CustomerId": 2, "FirstName": "Leonie", "LastName": "Köhler", "Email": "leonekohler@surfeu.de"},
            ],
        ),
        "Invoice": SchemaTable(
            columns=[
                ColumnInfo(name="InvoiceId", type="INTEGER", nullable=False),
                ColumnInfo(name="CustomerId", type="INTEGER", nullable=False),
                ColumnInfo(name="InvoiceDate", type="DATETIME", nullable=False),
                ColumnInfo(name="Total", type="NUMERIC", nullable=False),
            ],
            primary_keys=["InvoiceId"],
            foreign_keys=[
                FKInfo(column="CustomerId", references_table="Customer", references_column="CustomerId"),
            ],
            sample_rows=[
                {"InvoiceId": 1, "CustomerId": 2, "InvoiceDate": "2021-01-01", "Total": 1.98},
                {"InvoiceId": 2, "CustomerId": 4, "InvoiceDate": "2021-01-02", "Total": 3.96},
            ],
        ),
        "InvoiceLine": SchemaTable(
            columns=[
                ColumnInfo(name="InvoiceLineId", type="INTEGER", nullable=False),
                ColumnInfo(name="InvoiceId", type="INTEGER", nullable=False),
                ColumnInfo(name="TrackId", type="INTEGER", nullable=False),
                ColumnInfo(name="UnitPrice", type="NUMERIC", nullable=False),
                ColumnInfo(name="Quantity", type="INTEGER", nullable=False),
            ],
            primary_keys=["InvoiceLineId"],
            foreign_keys=[
                FKInfo(column="InvoiceId", references_table="Invoice", references_column="InvoiceId"),
            ],
            sample_rows=[
                {"InvoiceLineId": 1, "InvoiceId": 1, "TrackId": 2, "UnitPrice": 0.99, "Quantity": 1},
                {"InvoiceLineId": 2, "InvoiceId": 1, "TrackId": 4, "UnitPrice": 0.99, "Quantity": 1},
            ],
        ),
    }


@pytest.fixture
def sample_table_name() -> str:
    """Returns the name of the Invoice table."""
    return "Invoice"


@pytest.fixture
def sample_column_info() -> ColumnInfo:
    """Returns a ColumnInfo dict for the Total column."""
    return ColumnInfo(name="Total", type="NUMERIC", nullable=False)
