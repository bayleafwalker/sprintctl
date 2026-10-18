# Diagnose continuation gaps

`run diagnose` turns one bounded owner continuation read into a checklist. It
does not rank work by measured recovery burden or probe artifacts on your behalf.

```sh
sprintctl run diagnose --run-id <your-run-id> --item-id <item-id>
sprintctl run diagnose --run-id <your-run-id> --item-id <item-id> --checkpoint checkpoint.json --format json
```

Checkpoint, limit and independent cursor handling match
[run brief](resume-work.md#brief-one-served-item). The owner response is retained
unchanged in JSON under `owner_response`. The surrounding
`sprintctl-resume-diagnostic/v1` object is a derived presentation report, not a
new state owner or acceptance contract.

The stable reason codes distinguish absent comparison input, no current returned
source checkpoint, changed or unknown owner comparisons, incomplete pages,
unchecked bytes/reference correspondence/availability, caller-reported access
problems, and an unassessed effect outcome. No current source checkpoint does
not mean no historical checkpoint exists.

## Optional caller-reported checks

Supply `--observations checks.json` only when you already have observations. The
file must use this complete shape:

```json
{
  "schema": "sprintctl-resume-observations/v1",
  "repo_id": "your-repository",
  "item_id": 7,
  "snapshot_digest": "sha256:<owner-snapshot-digest>",
  "checks": [{
    "target": {"kind": "reference", "reference_id": 4},
    "observed_at": "2026-10-18T12:00:00Z",
    "observer": "reader-name",
    "method": "manual inspection",
    "status": "inaccessible",
    "detail": "Optional short explanation"
  }]
}
```

Use the exact repository, item and snapshot digest from the current owner page.
A target is either `{"kind":"source-bytes"}` or a reference ID visible on
that page. Status is `available`, `inaccessible` or `unchecked`. Duplicate
targets, wrong bindings, unknown fields and off-page references are rejected.
Files are limited to 64 KiB and 64 checks; observer/method are bounded to 200
characters, optional detail to 500. Times must be UTC. Observations more than
one hour old or later than report generation are retained but excluded with
`stale` or `future` reasons. Exactly one hour old remains eligible. Report time
comes from the caller's UTC clock; it is not an owner-issued timestamp or proof
of observation authenticity. Text shows up to five caller reports; JSON retains
all supplied checks and eligibility reasons.

These checks remain **caller reports**, including when their binding and time
match. They never replace owner unknowns, verify digest correspondence, prove
global absence, establish a target effect, or grant authority. Methods are
descriptions, never executable commands. The CLI makes no content requests and
does not persist the checks. A changed owner snapshot requires a fresh matching
report rather than silently rebinding an older one.

## Checklist order and qualification

The declared order is reported access problems, changed owner records,
missing/incomplete context, unchecked/unknown facts, then unassessed outcome.
Ties sort by reason code and subject. This is neither an effort estimate nor
cross-item priority. `recovery_effort` remains null/unmeasured.

Three paired disposable cases in `tests/pg/test_resume_diagnostic_trial.py`
freeze their questions before comparison: recover a missing continuation point,
identify changed records against a supplied baseline, and distinguish an access
report from owner assurance. Each case has three explicit owner reads (baseline,
before, after) and performs zero artifact checks. Repairs can remove a specific
gap or make a changed fact answerable without reducing the total checklist.
All remaining content/currentness/outcome unknowns must survive the repair.

This qualifies deterministic lookup/check requirements on fixtures. Actual
human/agent recovery effort, time saved and workflow improvement are unmeasured.
The field trial must use frozen questions and task population, independent fresh
readers for before/after conditions, timestamped attempts, intervention logs,
failed/incomplete cases and visible denominators. Missing measurements remain
unknown; three fixtures do not establish a statistical improvement.
