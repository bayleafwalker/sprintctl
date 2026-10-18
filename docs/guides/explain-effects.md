# Explain effect acceptance facts

Use `effect explain` to inspect an exact intent revision and artifact against the
current served owner records. The command performs one existing
`work.effect.acceptance-snapshot-v1` read. It never accepts work, runs an effect,
searches for credentials, discovers receipts, or fetches artifacts.

```sh
sprintctl effect explain \
  --intent-id <intent-id> \
  --revision <revision> \
  --canonical-intent-digest <64-lowercase-hex> \
  --artifact-sha256 sha256:<64-lowercase-hex>
```

Supply the exact values from the effect basis you are assessing. A changed
revision, intent digest, or artifact digest is shown as **MISMATCH**, even when
the current intent state is accepted or an artifact-check receipt matches.
A mismatch is a successful read, with exit zero; invalid input, backend failures
and authority refusals exit nonzero. Do not use the process exit code as an
acceptance verdict.

## Include a verification reference

If you already have a protected artifact-verification reference, save its
`run_id` and evidence `item_id` in a JSON file and add `--verification-ref`:

```json
{"run_id":"<verification-run-id>","item_id":"<evidence-item-id>"}
```

```sh
sprintctl effect explain \
  --intent-id <intent-id> --revision <revision> \
  --canonical-intent-digest <64-lowercase-hex> \
  --artifact-sha256 sha256:<64-lowercase-hex> \
  --verification-ref receipt-ref.json --format json
```

The command uses the current caller's authority and binding. No reference means
`not-supplied`. Missing evidence authority remains
`unknown-no-evidence-authority`. `not-found` means no matching receipt is visible
to this caller; it does not establish global absence. Receipt comparison and
check-revision comparison stay separate.

## What the explanation establishes

Text presents the requested basis/artifact comparisons, intent state, bound and
current Release, receipt visibility, bounded Decision facts, and collection
completeness. It shows at most five Decision and obligation records; JSON
preserves the entire existing owner response, including bounded collections,
digests, truncation and unknowns. Obligation labels can themselves be truncated.

The output keeps the evidence classes separate:

- Reports are not included in this snapshot.
- A protected artifact-check receipt concerns verification of the artifact.
- Target acknowledgment and target-effect observation are not included.
- An owner-recorded accepted or applied intent is a lifecycle fact, not an
  independently observed external outcome.
- A done work item and visible Decision records do not by themselves establish
  the terminal Decision binding, accepting identity/time, or complete attempt chain.

No overall accepted-result verdict is derived. The owner response explicitly
states `authorizes_acceptance: false`. Its snapshot digest identifies selected
owner facts; it does **not** seal the entire response, including receipt data,
and is not a portable integrity proof.

## Complete result reconstruction

The full request → attempts → artifact → receipt → target observation → acceptance
chain still requires supported joins, source coverage and independent
qualification. The existing fixture archival proof does not establish general
export/import support. Follow the [implementation roadmap](../plans/resume-and-explain.md)
for that separate work. An unavailable served authority is a failed check, not
an empty result or a reason to substitute a local database.
