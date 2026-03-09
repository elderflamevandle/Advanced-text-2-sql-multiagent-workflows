import pytest
import os


@pytest.fixture(scope="session")
def chinook_db_path():
    """Path to Chinook SQLite sample database."""
    path = os.path.join(os.path.dirname(__file__), "..", "data", "chinook.db")
    assert os.path.exists(path), f"Chinook DB not found at {path}"
    return os.path.abspath(path)


@pytest.fixture
def mock_pg_conn(mocker):
    """Mock psycopg2 connection for unit tests (no live PostgreSQL needed)."""
    conn = mocker.MagicMock()
    cursor = mocker.MagicMock()
    conn.cursor.return_value = cursor
    return conn


@pytest.fixture
def mock_mysql_conn(mocker):
    """Mock mysql.connector connection for unit tests (no live MySQL needed)."""
    conn = mocker.MagicMock()
    cursor = mocker.MagicMock()
    conn.cursor.return_value.__enter__ = lambda s: cursor
    conn.cursor.return_value.__exit__ = mocker.MagicMock(return_value=False)
    return conn
