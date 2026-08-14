import subprocess

import pytest
from click.testing import CliRunner

from sprintctl import db
from sprintctl.cli import cli


@pytest.fixture(autouse=True)
def _suppress_auditctl(monkeypatch):
    """Prevent real auditctl subprocess calls from contaminating test output.

    Tests that need to inspect or assert on audit calls should override
    subprocess.run themselves (as test_audit_events.py does).
    """
    import sprintctl.cli as _cli_module

    original = _cli_module.subprocess.run

    def _selective(args, **kwargs):
        if args and args[0] == "auditctl":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return original(args, **kwargs)

    monkeypatch.setattr(_cli_module.subprocess, "run", _selective)


@pytest.fixture(autouse=True)
def _isolate_cli_cwd(tmp_path, monkeypatch):
    """Keep local-mode CLI tests out of this checkout's remote marker."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("SPRINTCTL_DB", str(path))
    monkeypatch.setenv("SPRINTCTL_BACKEND", "local")
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


def seed_legacy_claim(
    conn,
    work_item_id: int,
    agent: str = "legacy-agent",
    *,
    claim_type: str = "execute",
    exclusive: int = 1,
    expires_at: str = "2999-01-01T00:00:00Z",
    claim_token: str | None = None,
    status: str = "active",
) -> int:
    """Insert a legacy ``claim`` row directly and return its id.

    The credential-bearing claim runtime is retired; the live ``claim``
    relation survives only until the schema cutover removes it.  Tests that
    still need archive, export, or migration evidence seed rows through this
    helper instead of a public API that no longer exists.
    """
    cur = conn.execute(
        """
        INSERT INTO claim (work_item_id, agent, claim_type, exclusive,
                           expires_at, claim_token, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (work_item_id, agent, claim_type, exclusive, expires_at, claim_token, status),
    )
    conn.commit()
    return int(cur.lastrowid)
