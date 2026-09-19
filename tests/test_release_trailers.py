"""Vuoro-Release trailer harvest at sync (S3 PR4), producer side.

A disposable git repository carries valid and malformed trailers; the
harvest enqueues one ``release.commit-observed`` observation per valid
(commit, digest) pair, advances a per-ref cursor and never re-enqueues.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sprintctl import contracts, outbox, release_trailers, served
from sprintctl.application import SUPPORTED_BATCH_TYPES

_NEW_SERVER_TYPES = frozenset(SUPPORTED_BATCH_TYPES)
_OLD_SERVER_TYPES = frozenset(SUPPORTED_BATCH_TYPES - {"release.commit-observed"})
_requires_312 = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="served mode requires Python 3.12+"
)

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
        # scp-like: query/fragment never survive (review: ?token leaked).
        ("git@github.com:o/r.git?token=abc#frag", "github.com:o/r.git"),
        ("github.com:o/r.git#x", "github.com:o/r.git"),
        # A password containing ``?``/``#`` must not leak a prefix: userinfo
        # is removed before the cut, on both branches.
        ("https://user:pa?ss@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://user:pa#ss@github.com/o/r.git", "https://github.com/o/r.git"),
        ("user:pa?ss@github.com:o/r.git", "github.com:o/r.git"),
        ("user:pa#ss@github.com:o/r.git", "github.com:o/r.git"),
        # Still credential-shaped after stripping: no hint at all.
        ("https://github.com/o/ghp_" + "A" * 40 + ".git", None),
        ("github.com:o/ghp_" + "A" * 40, None),
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
    monkeypatch.setattr(
        cli_module._served, "batch_record_types", lambda profile: _NEW_SERVER_TYPES
    )

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


# ---------------------------------------------------------------------------
# review fixes: failures skip, signatures, capability gate, shallow/detached
# ---------------------------------------------------------------------------


def test_git_missing_skips_instead_of_raising(repo, conn, monkeypatch):
    _commit(repo, f"one\n\nVuoro-Release: {D1}")
    monkeypatch.setenv("PATH", str(repo / "no-such-bin"))
    result = release_trailers.harvest_release_trailers(conn, repo, actor="a")
    assert result.status == "skipped"
    assert result.detail.startswith("harvest failed: FileNotFoundError")
    assert result.noteworthy_skip
    assert _observations(conn) == []


def test_failing_identity_lookup_skips_and_keeps_the_cursor(repo, conn):
    _commit(repo, f"one\n\nVuoro-Release: {D1}")

    def actor():
        raise RuntimeError("identity lookup failed: token ghp_" + "A" * 40)

    result = release_trailers.harvest_release_trailers(conn, repo, actor=actor)
    assert result.status == "skipped"
    assert result.detail.startswith("harvest failed: RuntimeError")
    assert "ghp_" not in result.detail  # never echo a credential-shaped detail
    assert _observations(conn) == []
    assert release_trailers.get_cursor(conn, "refs/heads/main") is None
    # The next pass with a working identity harvests the same commit.
    assert release_trailers.harvest_release_trailers(conn, repo, actor="a").enqueued == 1


def test_unparseable_origin_url_never_blocks_the_harvest(repo, conn):
    _git(repo, "remote", "set-url", "origin", "::@@??##")
    _commit(repo, f"one\n\nVuoro-Release: {D1}")
    result = release_trailers.harvest_release_trailers(conn, repo, actor="a")
    assert result.status == "harvested" and result.enqueued == 1


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_log_show_signature_config_does_not_break_parsing(repo, conn, tmp_path):
    key = tmp_path / "signing-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    _commit(repo, "unsigned base")
    _git(repo, "config", "gpg.format", "ssh")
    _git(repo, "config", "user.signingkey", str(key))
    _git(repo, "commit", "-q", "-S", "--allow-empty", "-m", f"signed\n\nVuoro-Release: {D1}")
    signed = _git(repo, "rev-parse", "HEAD")
    # A user's config: without --no-show-signature git log prefixes each
    # record with verification output ("No signature", gpg errors ...).
    _git(repo, "config", "log.showSignature", "true")

    result = release_trailers.harvest_release_trailers(conn, repo, actor="a")

    assert (result.status, result.malformed, result.enqueued) == ("harvested", 0, 1)
    assert [r.payload["commit_sha"] for r in _observations(conn)] == [signed]


def test_server_without_support_skips_and_keeps_the_cursor(repo, conn):
    _commit(repo, f"one\n\nVuoro-Release: {D1}")
    actor_calls = []

    def actor():
        actor_calls.append(1)
        return "a"

    old = release_trailers.harvest_release_trailers(
        conn, repo, actor=actor, server_accepts=lambda: False
    )
    assert old.status == "skipped"
    assert "does not accept release.commit-observed" in old.detail
    assert old.noteworthy_skip
    assert _observations(conn) == []  # never minted into the contiguous stream
    assert actor_calls == []
    assert release_trailers.get_cursor(conn, "refs/heads/main") is None

    rolled = release_trailers.harvest_release_trailers(
        conn, repo, actor=actor, server_accepts=lambda: True
    )
    assert (rolled.status, rolled.enqueued) == ("harvested", 1)


def test_capability_gate_is_not_consulted_with_nothing_to_enqueue(repo, conn):
    _commit(repo, "no trailers")

    def gate():
        raise AssertionError("catalog must not be fetched")

    result = release_trailers.harvest_release_trailers(
        conn, repo, actor="a", server_accepts=gate
    )
    assert result.status == "harvested"


def test_catalog_advertises_batch_record_types():
    from sprintctl.vuoro_adapter import catalog_operation_specs

    catalog = {"operations": list(catalog_operation_specs(resource_schema_available=True))}
    advertised = served.batch_record_types_from_catalog(catalog)
    assert advertised == _NEW_SERVER_TYPES
    assert release_trailers.EVENT_TYPE in advertised


def test_catalog_without_advertisement_reads_as_unknown():
    old_batch = {
        "name": "work.batch.apply",
        "input_schema": {"$defs": {"record": {"properties": {"event_type": {"type": "string"}}}}},
    }
    assert served.batch_record_types_from_catalog({"operations": [old_batch]}) is None
    assert served.batch_record_types_from_catalog({"operations": []}) is None
    assert served.batch_record_types_from_catalog(None) is None


def test_detached_head_is_harvested_under_head(repo, conn):
    c1 = _commit(repo, f"one\n\nVuoro-Release: {D1}")
    _commit(repo, f"two\n\nVuoro-Release: {D2}")
    _git(repo, "checkout", "-q", "--detach", c1)

    result = release_trailers.harvest_release_trailers(conn, repo, actor="a")

    assert (result.status, result.ref, result.enqueued, result.cursor) == ("harvested", "HEAD", 1, c1)
    assert _observations(conn)[0].payload["ref"] == "HEAD"


def test_shallow_clone_is_harvested_within_its_history(repo, conn, tmp_path):
    for index in range(4):
        _commit(repo, f"c{index}\n\nVuoro-Release: {D1[:-1]}{index}")
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "2", f"file://{repo}", str(clone)],
        check=True, capture_output=True, env={**os.environ, **_GIT_ENV},
    )
    result = release_trailers.harvest_release_trailers(conn, clone, actor="a")
    assert (result.status, result.scanned_commits, result.enqueued) == ("harvested", 2, 2)


def test_shallow_clone_whose_cursor_commit_is_absent_rescans_the_window(repo, conn, tmp_path):
    _commit(repo, f"c0\n\nVuoro-Release: {D1}")
    old_head = _git(repo, "rev-parse", "HEAD")
    for index in range(3):
        _commit(repo, f"c{index + 1}\n\nVuoro-Release: {D2[:-1]}{index}")
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(clone)],
        check=True, capture_output=True, env={**os.environ, **_GIT_ENV},
    )
    # A cursor naming a commit this shallow clone does not have.
    assert release_trailers.get_cursor(conn, "refs/heads/main") is None
    release_trailers._set_cursor(conn, "refs/heads/main", old_head)

    result = release_trailers.harvest_release_trailers(conn, clone, actor="a")

    assert (result.status, result.scanned_commits, result.enqueued) == ("harvested", 1, 1)


@_requires_312
def test_served_sync_against_an_old_server_skips_the_harvest_and_syncs_the_rest(
    runner, tmp_path, monkeypatch
):
    import sprintctl.cli as cli_module
    from sprintctl.cli import cli
    from tests.test_served_authority_sync import _append_observation, _configure_served_repo

    _configure_served_repo(tmp_path, monkeypatch)
    (tmp_path / ".git").rmdir()
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, f"deliver\n\nVuoro-Release: {D1}")
    monkeypatch.chdir(tmp_path)
    note = _append_observation(tmp_path, actor="served-actor")

    sent: list[dict] = []

    def fake_batch_apply(profile, *, repo_id=None, records, idempotency_key):
        for record in records:
            if record["event_type"] not in accepted["types"]:
                raise RuntimeError("record-type-not-allowed")
        sent.extend(records)
        return {"results": [
            {"kind": "record", "event_id": r["event_id"], "event_type": r["event_type"],
             "ingest_offset": len(sent), "duplicate": False}
            for r in records
        ]}

    accepted = {"types": _OLD_SERVER_TYPES}  # what the server's check admits
    advertised = {"types": None}  # an old server's catalog advertises nothing
    monkeypatch.setattr(cli_module._served, "batch_apply", fake_batch_apply)
    monkeypatch.setattr(
        cli_module._served, "batch_record_types", lambda profile: advertised["types"]
    )
    monkeypatch.setattr(
        cli_module._served, "identity_current",
        lambda profile, *, repo_id=None: {"repo_id": repo_id, "actor": "served-actor"},
    )

    first = runner.invoke(cli, ["authority", "sync", "--json"])
    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["uploaded_observation_count"] == 1
    assert payload["release_trailers"]["status"] == "skipped"
    assert [r["event_id"] for r in sent] == [note.event_id]

    advertised["types"] = _OLD_SERVER_TYPES  # advertises, but not the new type
    second = runner.invoke(cli, ["authority", "sync", "--json"])
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["release_trailers"]["status"] == "skipped"

    accepted["types"] = advertised["types"] = _NEW_SERVER_TYPES  # the server rolls
    third = runner.invoke(cli, ["authority", "sync", "--json"])
    assert third.exit_code == 0, third.output
    assert json.loads(third.output)["release_trailers"]["enqueued"] == 1
    assert [r["event_type"] for r in sent][-1] == "release.commit-observed"


@_requires_312
def test_served_sync_survives_a_failing_harvest(runner, tmp_path, monkeypatch):
    import sprintctl.cli as cli_module
    from sprintctl.cli import cli
    from tests.test_served_authority_sync import _append_observation, _configure_served_repo

    _configure_served_repo(tmp_path, monkeypatch)
    (tmp_path / ".git").rmdir()
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, f"deliver\n\nVuoro-Release: {D1}")
    monkeypatch.chdir(tmp_path)
    _append_observation(tmp_path, actor="served-actor")

    def unreachable_catalog(profile):
        raise ConnectionError("catalog unreachable")

    monkeypatch.setattr(cli_module._served, "batch_record_types", unreachable_catalog)
    monkeypatch.setattr(
        cli_module._served, "batch_apply",
        lambda profile, *, repo_id=None, records, idempotency_key: {"results": [
            {"kind": "record", "event_id": r["event_id"], "event_type": r["event_type"],
             "ingest_offset": 1, "duplicate": False}
            for r in records
        ]},
    )

    result = runner.invoke(cli, ["authority", "sync"])
    assert result.exit_code == 0, result.output
    assert "1 observations uploaded" in result.output
    assert "Release trailers: skipped (harvest failed: ConnectionError" in result.output
