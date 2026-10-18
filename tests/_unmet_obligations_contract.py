"""Backend-independent compatibility examples for the existing read-only report."""
from dataclasses import dataclass
from uuid import uuid4

import pytest


@dataclass
class ObligationsDriver:
    backend: object
    store: object
    sprint: int

    def accepted(self, *, labels=("test-run", "review"), evidence=None, kind="accept", sprint=None):
        b, s = self.backend, self.store
        sprint = self.sprint if sprint is None else sprint
        track = b.get_or_create_track(s, sprint, "obligations")
        item = b.create_work_item(s, sprint, track, "report " + uuid4().hex)
        b.set_work_item_status(s, item, "active")
        contract = {} if labels is None else {"evidence_obligations": list(labels)}
        reservation = b.reserve(s, item, actor="reporter", session_id="oracle", acceptance_contract=contract)
        digest = reservation["release_digest"]
        decision = b.record_decision(s, item, kind, actor="owner", rationale="Report fixture disposition", evidence_digests=evidence)
        expected = {"item_id": item, "decision_id": decision["id"], "release_digest": digest, "unmet": list(labels or [])}
        return digest, expected

    def report(self, **kwargs):
        return self.backend.list_unmet_obligations(self.store, sprint_id=self.sprint, **kwargs)


class UnmetObligationsCases:
    def test_unknown_well_formed_release(self, obligations_driver):
        d = obligations_driver
        assert d.backend.unmet_obligations(d.store, "f" * 64) == []

    @pytest.mark.parametrize("digest", ["bad", "A" * 64, None])
    def test_malformed_release_keeps_validation_error(self, obligations_driver, digest):
        d = obligations_driver
        with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
            d.backend.unmet_obligations(d.store, digest)

    @pytest.mark.parametrize("labels", [None, ()])
    def test_undeclared_and_empty_are_not_reported(self, obligations_driver, labels):
        d = obligations_driver
        digest, _ = d.accepted(labels=labels)
        assert d.backend.unmet_obligations(d.store, digest) == []
        assert d.report() == {"count": 0, "items": []}

    def test_all_labels_retained_in_declared_order(self, obligations_driver):
        d = obligations_driver
        digest, row = d.accepted(labels=("review", "test-run", "review"))
        assert d.backend.unmet_obligations(d.store, digest) == [row]
        assert d.report() == {"count": 1, "items": [row]}

    def test_any_digest_satisfies_all_labels(self, obligations_driver):
        d = obligations_driver
        digest, _ = d.accepted(evidence=["a" * 64])
        assert d.backend.unmet_obligations(d.store, digest) == []
        assert d.report() == {"count": 0, "items": []}

    @pytest.mark.parametrize("kind", ["reject", "withdraw", "revise"])
    def test_only_accept_decisions_are_reported(self, obligations_driver, kind):
        d = obligations_driver
        digest, _ = d.accepted(kind=kind)
        assert d.backend.unmet_obligations(d.store, digest) == []
        assert d.report() == {"count": 0, "items": []}

    @pytest.mark.parametrize("limit", [None, 0, 1, -1, "2"])
    def test_total_precedes_limit_and_python_slice_is_preserved(self, obligations_driver, limit):
        d = obligations_driver
        rows = [d.accepted()[1] for _ in range(3)]
        # Explicit identifiers define the contract, independently of SQL iteration order.
        rows.sort(key=lambda row: (row["item_id"], row["decision_id"]))
        expected = rows if limit is None else rows[:int(limit)]
        assert d.report(limit=limit) == {"count": 3, "items": expected}

    def test_invalid_limit_still_raises_on_empty_report(self, obligations_driver):
        with pytest.raises(ValueError):
            obligations_driver.report(limit="not-an-integer")

    def test_sprint_filter_excludes_other_sprint(self, obligations_driver):
        d = obligations_driver
        other = d.backend.create_sprint(d.store, "other " + uuid4().hex)
        _, ours = d.accepted()
        d.accepted(sprint=other)
        assert d.report() == {"count": 1, "items": [ours]}
        assert d.backend.list_unmet_obligations(d.store, sprint_id=-1) == {"count": 0, "items": []}

    def test_report_results_do_not_alias_future_reads(self, obligations_driver):
        d = obligations_driver
        digest, row = d.accepted()
        result = d.backend.unmet_obligations(d.store, digest)
        result[0]["unmet"].append("client-only")
        result[0]["item_id"] = -1
        assert d.backend.unmet_obligations(d.store, digest) == [row]
        assert d.report() == {"count": 1, "items": [row]}

    def test_default_limit_is_read_at_call_time(self, obligations_driver, monkeypatch):
        from sprintctl import unbound
        d = obligations_driver
        rows = [d.accepted()[1] for _ in range(3)]
        rows.sort(key=lambda row: (row["item_id"], row["decision_id"]))
        monkeypatch.setattr(unbound, "DEFAULT_LIMIT", 2)
        assert d.report() == {"count": 3, "items": rows[:2]}
        assert d.report(limit=None) == {"count": 3, "items": rows[:2]}

    def test_accept_bound_to_older_release_is_still_reported(self, obligations_driver):
        d = obligations_driver
        b, s = d.backend, d.store
        track = b.get_or_create_track(s, d.sprint, "historical release")
        item = b.create_work_item(s, d.sprint, track, "older release")
        b.set_work_item_status(s, item, "active")
        first = b.reserve(s, item, actor="reporter", session_id="oracle", acceptance_contract={"evidence_obligations": ["old-review"]})["release_digest"]
        _, revision = b.get_work_item_with_edit_revision(s, item)
        b.update_work_item_description(s, item, "newer contents", expected_revision=revision, actor="editor")
        current = b.reserve(s, item, actor="reporter", session_id="oracle", acceptance_contract={"evidence_obligations": ["new-review"]})["release_digest"]
        assert current != first
        decision = b.record_decision(s, item, "accept", actor="owner", release_digest=first)
        row = {"item_id": item, "decision_id": decision["id"], "release_digest": first, "unmet": ["old-review"]}
        assert b.unmet_obligations(s, first) == [row]
        assert b.unmet_obligations(s, current) == []
        assert d.report() == {"count": 1, "items": [row]}
