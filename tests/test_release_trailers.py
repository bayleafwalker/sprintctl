"""Vuoro-Release trailer harvest at sync (S3 PR4), producer side.

A disposable git repository carries valid and malformed trailers; the
harvest enqueues one ``release.commit-observed`` observation per valid
(commit, digest) pair, advances a per-ref cursor and never re-enqueues.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sprintctl import contracts, outbox, release_trailers

D1 = "a" * 64
D2 = "b" * 64
D3 = "c" * 64

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "remote", "add", "origin", "https://x-access-token:ghp_secret@github.com/o/r.git?t=1")
    return root


@pytest.fixture
def conn(tmp_path):
    c = outbox.open_outbox(tmp_path / "outbox.db")
    yield c
    c.close()


def _observations(conn):
    return [
        r for r in outbox.list_records(conn) if r.event_type == release_trailers.EVENT_TYPE
    ]


def test_event_type_is_an_observation():
    assert (
        contracts.record_class_for_type("release.commit-observed")
        is contracts.RecordClass.OBSERVATION
    )


def test_harvest_enqueues_valid_trailers_and_counts_malformed(repo, conn):
    _commit(repo, "plain commit\n\nno trailers here")
    c1 = _commit(repo, f"one\n\nVuoro-Release: {D1}")
    c2 = _commit(repo, f"prefixed and several\n\nVuoro-Release: sha256:{D2.upper()}\nVuoro-Release: {D3}")
    c3 = _commit(
        repo,
        "malformed\n\nVuoro-Release: not-a-digest\nVuoro-Release: sha256:abc\n"
        f"Vuoro-Release: {D1}",
    )

    result = release_trailers.harvest_release_trailers(conn, repo, actor="agent-a")

    assert result.status == "harvested"
    assert result.ref == "refs/heads/main"
    assert result.scanned_commits == 4
    assert result.enqueued == 4
    assert result.malformed == 2
    assert {m["value"] for m in result.malformed_trailers} == {"not-a-digest", "sha256:abc"}
    assert result.cursor == c3
    pairs = [(r.payload["commit_sha"], r.payload["release_digest"]) for r in _observations(conn)]
    # Oldest commit first; the sha256: prefix is removed and hex lower-cased.
    assert pairs == [(c1, D1), (c2, D2), (c2, D3), (c3, D1)]
    record = _observations(conn)[0]
    assert record.actor == "agent-a"
    assert record.record_class == outbox.OBSERVATION
    assert record.payload == {
        "release_digest": D1,
        "commit_sha": c1,
        "ref": "refs/heads/main",
        "remote_hint": "https://github.com/o/r.git",
    }
    assert release_trailers.get_cursor(conn, "refs/heads/main") == c3


def test_cursor_advances_and_resync_is_a_noop(repo, conn):
    _commit(repo, f"first\n\nVuoro-Release: {D1}")
    first = release_trailers.harvest_release_trailers(conn, repo, actor="a")
    assert first.enqueued == 1

    calls = []

    def actor():
        calls.append(1)
        return "a"

    again = release_trailers.harvest_release_trailers(conn, repo, actor=actor)
    assert (again.enqueued, again.scanned_commits) == (0, 0)
    assert calls == []  # nothing to enqueue: the served identity is never resolved
    assert len(_observations(conn)) == 1

    head = _commit(repo, f"second\n\nVuoro-Release: {D2}")
    later = release_trailers.harvest_release_trailers(conn, repo, actor="a")
    assert (later.scanned_commits, later.enqueued, later.cursor) == (1, 1, head)
    assert len(_observations(conn)) == 2


def test_rewritten_history_rescans_window_without_duplicates(repo, conn):
    c1 = _commit(repo, f"first\n\nVuoro-Release: {D1}")
    _commit(repo, f"second\n\nVuoro-Release: {D2}")
    release_trailers.harvest_release_trailers(conn, repo, actor="a")
    _git(repo, "reset", "-q", "--hard", c1)
    _commit(repo, f"rewritten\n\nVuoro-Release: {D3}")

    result = release_trailers.harvest_release_trailers(conn, repo, actor="a")

    assert result.scanned_commits == 2  # cursor not an ancestor: bounded window
    assert result.already_enqueued == 1
    assert result.enqueued == 1
    assert len(_observations(conn)) == 3


def test_first_harvest_is_bounded_by_the_window(repo, conn):
    for index in range(3):
        _commit(repo, f"c{index}\n\nVuoro-Release: {D1[:-1]}{index}")
    result = release_trailers.harvest_release_trailers(conn, repo, actor="a", window=2)
    assert (result.scanned_commits, result.enqueued) == (2, 2)


def test_not_a_checkout_is_skipped(tmp_path, conn):
    (tmp_path / "fake" / ".git").mkdir(parents=True)
    result = release_trailers.harvest_release_trailers(conn, tmp_path / "fake", actor="a")
    assert result.status == "skipped"
    assert release_trailers.harvest_release_trailers(conn, tmp_path, actor="a").status == "skipped"


def test_nested_directory_of_a_checkout_is_not_scanned(repo, conn):
    _commit(repo, f"one\n\nVuoro-Release: {D1}")
    nested = repo / "sub"
    (nested / ".git").mkdir(parents=True)
    assert release_trailers.harvest_release_trailers(conn, nested, actor="a").status == "skipped"


def test_repository_without_commits_is_skipped(repo, conn):
    assert release_trailers.harvest_release_trailers(conn, repo, actor="a").status == "skipped"


def test_credential_shaped_malformed_value_is_not_echoed(repo, conn):
    _commit(repo, "oops\n\nVuoro-Release: ghp_" + "A" * 40)
    result = release_trailers.harvest_release_trailers(conn, repo, actor="a")
    assert result.malformed == 1
    assert result.malformed_trailers[0]["value"] == "<redacted credential-shaped value>"
    assert "ghp_" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://user:tok@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://ghp_abc@github.com:443/o/r.git?x=1#f", "https://github.com:443/o/r.git"),
        ("ssh://git@host.example:2222/o/r.git", "ssh://host.example:2222/o/r.git"),
        ("git@github.com:o/r.git", "github.com:o/r.git"),
        ("github.com:o/r.git", "github.com:o/r.git"),
        ("/srv/git/r.git", "/srv/git/r.git"),
        ("  ", None),
        (None, None),
    ],
)
def test_strip_url_credentials(url, expected):
    assert release_trailers.strip_url_credentials(url) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (D1, D1),
        (f"sha256:{D1}", D1),
        (f"SHA256:{D1.upper()}", D1),
        (f"  {D1}  ", D1),
        ("a" * 63, None),
        ("g" * 64, None),
        (f"sha512:{D1}", None),
        (f"{D1} trailing", None),
    ],
)
def test_normalize_digest(value, expected):
    assert release_trailers.normalize_digest(value) == expected


def test_stripped_remote_passes_the_credential_shape_check(repo, conn):
    _commit(repo, f"one\n\nVuoro-Release: {D1}")
    release_trailers.harvest_release_trailers(conn, repo, actor="a")
    [record] = _observations(conn)
    contracts.reject_credential_shaped_values(record.payload, "payload")
    assert "ghp_secret" not in json.dumps(record.payload)


# ---------------------------------------------------------------------------
# served ``authority sync`` harvests before flushing
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.version_info < (3, 12), reason="served mode requires Python 3.12+")
def test_served_authority_sync_uploads_harvested_trailers(runner, tmp_path, monkeypatch):
    import sprintctl.cli as cli_module
    from sprintctl.cli import cli
    from tests.test_served_authority_sync import _configure_served_repo

    _configure_served_repo(tmp_path, monkeypatch)
    (tmp_path / ".git").rmdir()
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "remote", "add", "origin", "https://u:p4ss@example.com/o/r.git")
    sha = _commit(tmp_path, f"deliver\n\nVuoro-Release: {D1}\nVuoro-Release: nope")
    monkeypatch.chdir(tmp_path)

    captured = {}

    def fake_batch_apply(profile, *, repo_id=None, records, idempotency_key):
        captured["records"] = records
        return {"results": [
            {"kind": "record", "event_id": r["event_id"], "event_type": r["event_type"],
             "ingest_offset": i + 1, "duplicate": False}
            for i, r in enumerate(records)
        ]}

    identity_calls = []

    def fake_identity(profile, *, repo_id=None):
        identity_calls.append(repo_id)
        return {"repo_id": repo_id, "actor": "served-actor"}

    monkeypatch.setattr(cli_module._served, "batch_apply", fake_batch_apply)
    monkeypatch.setattr(cli_module._served, "identity_current", fake_identity)

    result = runner.invoke(cli, ["authority", "sync", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["uploaded_observation_count"] == 1
    assert payload["release_trailers"]["enqueued"] == 1
    assert payload["release_trailers"]["malformed"] == 1
    [record] = captured["records"]
    assert record["event_type"] == "release.commit-observed"
    assert record["actor"] == "served-actor"
    assert record["payload"]["commit_sha"] == sha
    assert record["payload"]["remote_hint"] == "https://example.com/o/r.git"
    assert len(identity_calls) == 1

    again = runner.invoke(cli, ["authority", "sync", "--json"])
    assert again.exit_code == 0, again.output
    assert json.loads(again.output)["release_trailers"]["enqueued"] == 0
    assert len(identity_calls) == 1
