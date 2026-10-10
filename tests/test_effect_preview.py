"""Owner read, binding, redaction and failure-path oracles for static preview."""
import copy
import json
import subprocess
from types import SimpleNamespace

import jsonschema
import pytest

from sprintctl import effect_intent as effect
from sprintctl import effect_preview as preview
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS


def intent(path="docs/readme.md", **changes):
    row = {"intent_id": "intent_" + "0" * 26, "revision": 1, "state": "proposed",
        "item_id": 1, "run_id": "run_" + "0" * 26, "repository": "repo-a",
        "base_commit": "a" * 40, "title": "secret-title", "rationale": "secret-rationale",
        "unified_diff": f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-secret-old\n+secret-new\n",
        "acceptance": None, "application": None, "rejection": None}
    row.update(changes)
    row["canonical_intent_digest"] = effect.canonical_intent_digest(row)
    return row


class OwnerReads:
    def __init__(self, row, candidates=()):
        self.row, self.candidates, self.calls = row, list(candidates), []

    def get_effect_intent(self, store, intent_id):
        self.calls.append(("get", store.repo_id, intent_id))
        return copy.deepcopy(self.row) if intent_id == self.row["intent_id"] else None

    def list_proposed_effect_intents(self, store, *, item_id, limit):
        self.calls.append(("list", store.repo_id, item_id, limit))
        return copy.deepcopy(self.candidates[:limit])

    def __getattr__(self, name):
        raise AssertionError("unexpected backend operation: " + name)


def app(row, candidates=()):
    backend = OwnerReads(row, candidates)
    def mutation(*args, **kwargs):
        raise AssertionError("preview reached a writer")
    return WorkApplication("repo-a", SimpleNamespace(repo_id="repo-a"), backend,
        mutation, mutation, mutation, mutation), backend


def ctx(authorities=(effect.AUTHORITY_GET,), repo="repo-a"):
    return SimpleNamespace(repo_id=repo, identity=SimpleNamespace(repo_id=repo,
        principal_id="actor", workspace_id="workspace", authorities=frozenset(authorities)),
        request_id="request", basis_revision=None, catalog_revision="catalog",
        idempotency_requirement="not-allowed", idempotency_key=None)


def read(work, **arguments):
    return work.invoke(effect.OPERATION_PREVIEW, arguments, context=ctx())


def test_owner_read_is_redacted_bounded_and_never_writes():
    row = intent(); before = copy.deepcopy(row); work, backend = app(row)
    result = read(work, intent_id=row["intent_id"])
    p = result["preview"]
    assert p["basis"]["canonical_intent_digest"] == row["canonical_intent_digest"]
    assert p["declared_changes"] == {"status": "parsed", "path_count": 1,
        "paths": [], "redacted": True, "truncated": False}
    assert p["resources"]["status"] == "not-declared"
    assert p["protected_policy"]["status"] == p["external_consequences"]["status"] == "unknown"
    assert p["duplicate_relation"]["status"] == "unknown"
    assert backend.calls == [("get", "repo-a", row["intent_id"])]
    assert row == before
    text = json.dumps(result)
    assert all(secret not in text for secret in ["secret-old", "secret-new", "secret-title", "secret-rationale", "docs/readme.md"])
    descriptor = next(c for c in WORK_OPERATION_CONTRACTS if c.name == effect.OPERATION_PREVIEW)
    jsonschema.validate(result, descriptor.result_schema)


def test_entitled_opt_in_path_disclosure_and_source_snapshot():
    row = intent("platform/secret.yaml", state="accepted", acceptance={"record": "present"})
    work, _ = app(row)
    p = read(work, intent_id=row["intent_id"], disclose_paths=True)["preview"]
    assert p["declared_changes"]["paths"] == [{"path": "platform/secret.yaml",
        "added_lines": 1, "removed_lines": 1, "source_only": False}]
    assert p["basis"]["state"] == "accepted"
    assert p["receipt_source"]["acceptance"] == "observed"
    assert p["external_consequences"]["status"] == "unknown"


def test_repeated_path_sections_preserve_all_declared_line_counts():
    diff = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
            "--- a/a.txt\n+++ b/a.txt\n@@ -2,0 +2 @@\n+second\n")
    assert preview._declarations(diff) == ("parsed", [{"path": "a.txt",
        "added_lines": 2, "removed_lines": 1, "source_only": False}])


def test_missing_intent_and_missing_get_authority_do_not_expose_paths():
    row = intent(); work, backend = app(row)
    with pytest.raises(ApplicationRejection) as missing:
        read(work, intent_id="missing")
    assert missing.value.code == "effect-not-found"
    backend.calls.clear()
    with pytest.raises(ApplicationRejection) as denied:
        work.invoke(effect.OPERATION_PREVIEW, {"intent_id": row["intent_id"]}, context=ctx(("work:read", "work.effect.propose")))
    assert denied.value.code == "authority-required" and backend.calls == []


@pytest.mark.parametrize("extra", [{"accept": True}, {"disclose_paths": 1}, {"disclose_paths": "true"}, {"paths": ["x"]}, {"resources": ["Deployment"]}])
def test_preview_cannot_accept_or_supply_its_own_path_facts(extra):
    row = intent(); work, _ = app(row)
    with pytest.raises(ApplicationRejection) as refused:
        read(work, intent_id=row["intent_id"], **extra)
    assert refused.value.code == "invalid-arguments"


def test_changed_owner_bytes_refuse_instead_of_reporting_plausible_paths():
    row = intent(); row["unified_diff"] += "secret-tamper"; work, _ = app(row)
    with pytest.raises(ApplicationRejection) as refused:
        read(work, intent_id=row["intent_id"], disclose_paths=True)
    assert refused.value.code == "effect-digest-mismatch"
    assert "secret" not in str(refused.value)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "a/../escape", ".git/config", "name\u202e", ":(glob)*", "-option"])
def test_unsafe_paths_fail_without_a_partial_declaration_or_raw_error(path):
    p = preview.preview_intent(intent(path), disclose_paths=True)
    assert p["declared_changes"]["status"] != "parsed"
    assert p["declared_changes"]["paths"] == []
    assert path not in json.dumps(p)


@pytest.mark.parametrize("diff", ["raw-secret-patch", "GIT binary patch\nsecret", "\x00secret", "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-secret\n"])
def test_unparseable_input_has_only_generic_unknown(diff):
    p = preview.preview_intent(intent(unified_diff=diff), disclose_paths=True)
    assert p["declared_changes"]["status"] != "parsed"
    assert p["declared_changes"]["path_count"] is None
    assert "secret" not in json.dumps(p)


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("git", 5)])
def test_missing_parser_or_timeout_is_unavailable(monkeypatch, failure):
    def fail(*a, **kw):
        raise failure
    monkeypatch.setattr(preview.subprocess, "run", fail)
    assert preview.preview_intent(intent())["declared_changes"]["status"] == "unavailable"


def test_parser_is_static_isolated_and_does_not_inherit_credentials_or_git_hooks(monkeypatch):
    actual = preview.subprocess.run
    def guard(args, **kwargs):
        assert args == ["git", "apply", "--numstat", "-z", "-"]
        assert kwargs["cwd"].startswith("/tmp/sprintctl-preview-")
        assert not any(k in kwargs["env"] for k in ["FJ_TOKEN", "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE", "GIT_EXTERNAL_DIFF"])
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert kwargs["env"]["GIT_CEILING_DIRECTORIES"] == kwargs["cwd"]
        return actual(args, **kwargs)
    monkeypatch.setattr(preview.subprocess, "run", guard)
    assert preview.preview_intent(intent())["declared_changes"]["status"] == "parsed"


def test_rename_reports_both_declared_sides_and_large_report_marks_truncation():
    diff = "diff --git a/old.md b/new.md\nsimilarity index 100%\nrename from old.md\nrename to new.md\n"
    p = preview.preview_intent(intent(unified_diff=diff), disclose_paths=True)
    assert {r["path"] for r in p["declared_changes"]["paths"]} == {"old.md", "new.md"}
    assert p["declared_changes"]["path_count"] == 2
    diffs = "".join(intent(f"docs/{i}.md")["unified_diff"] for i in range(201))
    p = preview.preview_intent(intent(unified_diff=diffs), disclose_paths=True)
    assert p["declared_changes"]["path_count"] == 201
    assert len(p["declared_changes"]["paths"]) == preview.MAX_PATHS
    assert p["declared_changes"]["truncated"]


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("line", ["rename from phantom.txt\n", "copy from phantom.txt\n"])
def test_git_ignored_preamble_or_trailer_cannot_invent_a_source(position, line):
    real = intent("real.txt")["unified_diff"]
    diff = line + real if position == "before" else real + line
    p = preview.preview_intent(intent(unified_diff=diff), disclose_paths=True)
    assert p["declared_changes"]["status"] == "unsupported"
    assert p["declared_changes"]["paths"] == []
    assert "phantom" not in json.dumps(p)


def test_rename_source_must_match_file_header_and_numstat_destination():
    diff = "diff --git a/old.md b/new.md\nsimilarity index 100%\nrename from phantom.md\nrename to new.md\n"
    p = preview.preview_intent(intent(unified_diff=diff), disclose_paths=True)
    assert p["declared_changes"]["status"] == "unsupported"
    assert p["declared_changes"]["paths"] == []


def test_inherited_tmpdir_inside_checkout_cannot_discover_local_git_configuration(monkeypatch, tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    actual = preview.subprocess.run
    def guard(args, **kwargs):
        from pathlib import Path
        assert not Path(kwargs["cwd"]).is_relative_to(tmp_path)
        probe = actual(["git", "rev-parse", "--is-inside-work-tree"], cwd=kwargs["cwd"],
                       env=kwargs["env"], capture_output=True)
        assert probe.returncode != 0  # independently prove no repository found
        return actual(args, **kwargs)
    monkeypatch.setattr(preview.subprocess, "run", guard)
    assert preview.preview_intent(intent())["declared_changes"]["status"] == "parsed"


def test_duplicate_scope_and_listing_capability_do_not_imply_uniqueness():
    row = intent(); other = intent(intent_id="intent_" + "1" * 26)
    wrong_item = intent(item_id=2, intent_id="intent_" + "2" * 26)
    wrong_release = intent(release_digest="b" * 64, intent_id="intent_" + "3" * 26)
    work, backend = app(row, [row, other, wrong_item, wrong_release])
    p = work.invoke(effect.OPERATION_PREVIEW, {"intent_id": row["intent_id"]},
        context=ctx((effect.AUTHORITY_GET, effect.AUTHORITY_LIST_PROPOSED)))["preview"]
    assert p["duplicate_relation"]["candidate_intent_ids"] == [other["intent_id"]]
    assert p["duplicate_relation"]["complete"]
    work, _ = app(row, [row] * effect.LIST_DEFAULT_LIMIT)
    p = work.invoke(effect.OPERATION_PREVIEW, {"intent_id": row["intent_id"]},
        context=ctx((effect.AUTHORITY_GET, effect.AUTHORITY_LIST_PROPOSED)))["preview"]
    assert not p["duplicate_relation"]["complete"]


def test_preview_is_one_new_read_descriptor_with_no_new_acceptance_capability():
    descriptor = next(c for c in WORK_OPERATION_CONTRACTS if c.name == effect.OPERATION_PREVIEW)
    assert descriptor.execution_semantics == "read"
    assert descriptor.required_authority == effect.AUTHORITY_GET
    assert descriptor.idempotency == "not-allowed"
    assert effect.OPERATION_ACCEPT != effect.OPERATION_PREVIEW
