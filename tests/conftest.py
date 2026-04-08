import pytest
from click.testing import CliRunner

from sprintctl import db
from sprintctl.cli import cli


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("SPRINTCTL_DB", str(path))
    return path


@pytest.fixture
def conn(db_path):
    c = db.get_connection(db_path)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def runner(db_path):
    """CliRunner with SPRINTCTL_DB already set via db_path fixture."""
    return CliRunner()


@pytest.fixture
def active_sprint(conn):
    sid = db.create_sprint(conn, "S1", "Ship Phase 1", "2026-03-01", "2026-03-31", "active")
    return db.get_sprint(conn, sid)
