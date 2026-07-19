import json
import sqlite3
from pathlib import Path

import pytest

from sprintctl import doctor
from sprintctl.cli import cli


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_current_fixture_is_deterministic_and_healthy():
    facts = _fixture("doctor-current.json")

    first = doctor.evaluate_facts(facts)
    second = doctor.evaluate_facts(facts)

    assert first == second
    assert doctor.dumps(first) == doctor.dumps(second)
    assert first["status"] == "ok"
    assert first["findings"] == []


def test_stale_fixture_detects_uuid_json_extra_and_schema_mismatches():
    report = doctor.evaluate_facts(_fixture("doctor-stale.json"))

    assert report["status"] == "error"
    findings = {finding["code"]: finding for finding in report["findings"]}
    assert set(findings) == {
        "executable-source-version-mismatch",
        "package-source-version-mismatch",
        "source-capability-mismatch",
        "remote-extra-missing",
        "schema-version-mismatch",
    }
    assert "portable-aggregate-uuid-json/v1" in findings["source-capability-mismatch"]["message"]
    assert "pipx upgrade sprintctl" in findings["executable-source-version-mismatch"]["guidance"]
    assert "sprintctl[remote]" in findings["remote-extra-missing"]["guidance"][0]


def test_local_schema_probe_is_read_only_and_reports_mismatch(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (9)")

    result = doctor._probe_local_schema({"SPRINTCTL_DB": str(path)})

    assert result == {
        "backend": "local",
        "expected_version": 10,
        "actual_version": 9,
        "compatible": False,
        "status": "mismatch",
        "error": None,
    }
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("schema_version",)
        ]


def test_remote_schema_probe_enforces_read_only_connection(monkeypatch):
    psycopg = pytest.importorskip("psycopg")

    observed = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query):
            observed["query"] = query

        def fetchone(self):
            return (1,)

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            observed["closed"] = True

    def connect(url, **kwargs):
        observed.update({"url": url, "kwargs": kwargs})
        return Connection()

    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(psycopg, "connect", connect)

    result = doctor._probe_remote_schema({"SPRINTCTL_URL": "postgresql://example/db"})

    assert result["status"] == "current"
    assert observed["kwargs"]["options"] == "-c default_transaction_read_only=on"
    assert observed["query"].startswith("SELECT version")
    assert observed["closed"] is True


def test_remote_schema_error_redacts_credentials(monkeypatch):
    psycopg = pytest.importorskip("psycopg")

    url = "postgresql://user:password@example.invalid/db"
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())

    def fail_connect(value, **_kwargs):
        raise RuntimeError(f"could not connect to {value}")

    monkeypatch.setattr(psycopg, "connect", fail_connect)

    result = doctor._probe_remote_schema({"SPRINTCTL_URL": url})

    assert result["status"] == "unavailable"
    assert result["error"] == "could not connect to <redacted SPRINTCTL_URL>"
    assert "password" not in result["error"]


def test_doctor_json_reports_config_without_exposing_remote_url(tmp_path, monkeypatch, runner):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "remote", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
    monkeypatch.setenv("SPRINTCTL_URL", "postgresql://secret:password@example.invalid/db")
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)

    result = runner.invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["backend"]["url_configured"] is True
    assert "secret" not in result.output
    assert "password" not in result.output
    assert {finding["code"] for finding in payload["findings"]} >= {
        "remote-extra-missing",
        "schema-unavailable",
    }


def test_doctor_invalid_backend_does_not_create_database(tmp_path, monkeypatch, runner):
    db_path = tmp_path / "state" / "sprintctl.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "invalid")
    monkeypatch.setenv("SPRINTCTL_DB", str(db_path))

    result = runner.invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert "backend-config-invalid" in {finding["code"] for finding in payload["findings"]}
    assert not db_path.exists()


def test_doctor_human_output_names_provenance_and_guidance(monkeypatch, runner):
    report = doctor.evaluate_facts(_fixture("doctor-stale.json"))
    monkeypatch.setattr(doctor, "collect_report", lambda: report)

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "sprintctl doctor: error" in result.output
    assert "executable: 0.1.0 (/tools/sprintctl)" in result.output
    assert "package: code=0.1.0 metadata=0.1.0" in result.output
    assert "source-capability-mismatch" in result.output
    assert "fix: pipx upgrade sprintctl" in result.output
