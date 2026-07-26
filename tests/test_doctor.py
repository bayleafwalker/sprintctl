import json
import sqlite3
import sys
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


def test_local_schema_probe_accepts_freshly_initialized_database(tmp_path):
    from sprintctl import db

    path = tmp_path / "fresh.db"
    conn = db.get_connection(path)
    try:
        db.init_db(conn)
    finally:
        conn.close()

    result = doctor._probe_local_schema({"SPRINTCTL_DB": str(path)})

    assert result["status"] == "current"
    assert result["compatible"] is True
    assert result["actual_version"] == doctor.SQLITE_SCHEMA_VERSION


def test_local_schema_probe_is_read_only_and_reports_mismatch(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (9)")

    result = doctor._probe_local_schema({"SPRINTCTL_DB": str(path)})

    assert result == {
        "backend": "local",
        "expected_version": doctor.SQLITE_SCHEMA_VERSION,
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
            return (4,)

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


# --- served-mode probe -------------------------------------------------


def _write_served_profile(path: Path, *, credential_ref: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "vuoro-client-profile/v1",
                "id": "workstation-vuoro-shared",
                "target": {
                    "environment_id": "vuoro-shared",
                    "endpoint": "https://vuoro-shared.example/",
                },
                "credential_ref": credential_ref,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_probe_served_backend_reports_unavailable_without_a_profile():
    result = doctor._probe_served_backend({}, None)

    assert result["backend"] == "served"
    assert result["status"] == "unavailable"
    assert result["error"] == "served profile did not parse"
    assert result["credential_resolved"] is None


def test_probe_served_backend_reports_unavailable_when_vuoro_client_is_missing(
    tmp_path, monkeypatch
):
    from sprintctl.backend import ServedProfile

    profile = ServedProfile(
        name="workstation-vuoro-shared",
        endpoint="https://vuoro-shared.example/",
        credential_ref="file:/does/not/matter",
        expected_environment="vuoro-shared",
        source_path=tmp_path / "profile.json",
    )
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)

    result = doctor._probe_served_backend({}, profile)

    assert result["status"] == "unavailable"
    assert result["error"] == "vuoro-client is not installed"
    # This genuinely reflects this sandbox: vuoro-client is not installed here.
    assert doctor.importlib.util.find_spec("vuoro_client") is None


def test_probe_served_backend_reports_credential_resolution_failure(tmp_path, monkeypatch):
    from sprintctl.backend import ServedProfile
    from sprintctl.vuoro_credentials import CredentialResolutionError

    profile = ServedProfile(
        name="workstation-vuoro-shared",
        endpoint="https://vuoro-shared.example/",
        credential_ref="file:/does/not/exist",
        expected_environment="vuoro-shared",
        source_path=tmp_path / "profile.json",
    )
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())

    result = doctor._probe_served_backend({}, profile)

    assert result["credential_resolved"] is False
    assert result["status"] == "unavailable"
    assert "credential" in result["error"].lower()


def test_probe_served_backend_reports_current_when_catalog_matches(tmp_path, monkeypatch):
    from sprintctl.backend import ServedProfile

    cred_path = tmp_path / "cred"
    cred_path.write_text("token-value\n", encoding="utf-8")
    cred_path.chmod(0o600)
    profile = ServedProfile(
        name="workstation-vuoro-shared",
        endpoint="https://vuoro-shared.example/",
        credential_ref=f"file:{cred_path}",
        expected_environment="vuoro-shared",
        source_path=tmp_path / "profile.json",
    )
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        doctor._served,
        "catalog_operation_names",
        lambda served_profile: set(doctor._SERVED_EXPECTED_OPERATIONS),
    )

    result = doctor._probe_served_backend({}, profile)

    assert result["credential_resolved"] is True
    assert result["compatible"] is True
    assert result["status"] == "current"
    assert result["error"] is None
    assert result["actual_version"] == sorted(doctor._SERVED_EXPECTED_OPERATIONS)


def test_probe_served_backend_reports_mismatch_for_missing_operations(tmp_path, monkeypatch):
    from sprintctl.backend import ServedProfile

    cred_path = tmp_path / "cred"
    cred_path.write_text("token-value\n", encoding="utf-8")
    cred_path.chmod(0o600)
    profile = ServedProfile(
        name="workstation-vuoro-shared",
        endpoint="https://vuoro-shared.example/",
        credential_ref=f"file:{cred_path}",
        expected_environment="vuoro-shared",
        source_path=tmp_path / "profile.json",
    )
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        doctor._served,
        "catalog_operation_names",
        lambda served_profile: {"work.read.sprints"},
    )

    result = doctor._probe_served_backend({}, profile)

    assert result["compatible"] is False
    assert result["status"] == "mismatch"
    assert "work.claim.start" in result["error"]


def test_probe_served_backend_reports_catalog_transport_failure(tmp_path, monkeypatch):
    from sprintctl.backend import ServedProfile

    cred_path = tmp_path / "cred"
    cred_path.write_text("token-value\n", encoding="utf-8")
    cred_path.chmod(0o600)
    profile = ServedProfile(
        name="workstation-vuoro-shared",
        endpoint="https://vuoro-shared.example/",
        credential_ref=f"file:{cred_path}",
        expected_environment="vuoro-shared",
        source_path=tmp_path / "profile.json",
    )
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())

    def _boom(served_profile):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(doctor._served, "catalog_operation_names", _boom)

    result = doctor._probe_served_backend({}, profile)

    assert result["credential_resolved"] is True
    assert result["status"] == "unavailable"
    assert result["error"] == "connection refused"


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason=(
        "served mode requires Python 3.12+; this test exercises behavior only "
        "reachable past that version gate"
    ),
)
def test_doctor_json_reports_served_mode_end_to_end(tmp_path, monkeypatch, runner):
    (tmp_path / ".git").mkdir()
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "served", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    cred_path = tmp_path / "cred"
    cred_path.write_text("token-value\n", encoding="utf-8")
    cred_path.chmod(0o600)
    profile_path = _write_served_profile(
        tmp_path / "profile.json", credential_ref=f"file:{cred_path}"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile_path))
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        doctor._served,
        "catalog_operation_names",
        lambda served_profile: set(doctor._SERVED_EXPECTED_OPERATIONS),
    )

    result = runner.invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["backend"]["resolved_mode"] == "served"
    assert payload["schema"]["backend"] == "served"
    assert payload["schema"]["status"] == "current"
    assert payload["extras"]["served"]["enabled"] is True
    assert payload["status"] == "ok"


def test_doctor_served_mode_missing_extra_is_a_finding(tmp_path, monkeypatch, runner):
    (tmp_path / ".git").mkdir()
    profile_path = _write_served_profile(
        tmp_path / "profile.json", credential_ref="file:~/.config/vuoro/credentials/x"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile_path))
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)

    result = runner.invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["extras"]["served"]["enabled"] is False
    codes = {finding["code"] for finding in payload["findings"]}
    assert "served-extra-missing" in codes
    assert "sprintctl[served]" in " ".join(
        guidance
        for finding in payload["findings"]
        for guidance in finding["guidance"]
    )


def test_doctor_human_output_reports_served_extra(monkeypatch, runner):
    facts = _fixture("doctor-current.json")
    facts["backend"]["environment_mode"] = "served"
    facts["backend"]["resolved_mode"] = "served"
    facts["extras"]["served"] = {"enabled": False, "requirement": "vuoro-client"}
    facts["schema"] = {
        "backend": "served",
        "expected_version": ["work.claim.start"],
        "actual_version": None,
        "compatible": None,
        "status": "unavailable",
        "error": "vuoro-client is not installed",
    }
    report = doctor.evaluate_facts(facts)
    monkeypatch.setattr(doctor, "collect_report", lambda: report)

    result = runner.invoke(cli, ["doctor"])

    assert "extras: remote=" in result.output
    assert "served=missing" in result.output
    assert "served-extra-missing" in result.output
