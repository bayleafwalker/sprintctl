---
doc_id: resume-explain-qualification-handoff
status: draft
supersedes: null
---

# Remaining resume and explain qualification

The approved source slices are implemented. The bounded offline packet source
is `13ca363fda35a7c63c99012760be1c634bb6b671` (`sprintctl#2714`).
The [actual nine-reader trial](../evidence/independent-reader-trial-results.md)
(`sprintctl#2715`) is retained with its frozen event5214 and108 questions.
Neither completes the broader parent outcomes below. Live native records win.

| Open native scope | Accountable boundary | Prerequisite and required inputs | Observable completion |
|---|---|---|---|
| `sprintctl#2661`: cross-host/model/harness continuation | Qualification coordinator; deployment owner supplies available protected hosts | Named `agentops#2654` qualification gate; approved runtimes/hosts and interrupted-task population; preserved failures and independent reader outputs | Independent readers detect deliberate artifact/Decision changes across declared environments; actual model/harness/host and extra-read denominators retained. |
| `sprintctl#2663`: recovery-effort comparison | Qualification coordinator and measured readers | `sprintctl#2661`; comparable start/end and active-effort instrumentation, intervention provenance and declared population | Actual paired effort with unknowns/failures, or explicit insufficient evidence. Current38/195second orchestration times are ineligible effort. |
| `sprintctl#2662`: historical result closure and supported transfer | Sprintctl owner records; protected effect/evidence owners supply their authoritative joins | Named `agentops#2654` gate; complete request, attempts, artifact, receipt, observation and terminal Decision bindings; archival owner `agentops#2642` and `agentops#2630` | Independent reader detects missing/changed relationships; supported export/import preserves exact relationships and evidence classes. Offline packet self-hash alone is insufficient. |
| L1 representative accepted-outcome comparison | Vuoro learning layer, broader `vuoro#2237` | Authenticated owner source joins and qualified measurement windows/populations | Known/unknown accepted-outcome denominators and qualified effort/cost; insufficient samples remain inconclusive. |
| Deployed acceptance and external onboarding | Deployment session `01a14da7-de27-7071-a890-e6c5554bf0ee`; existing `sprintctl#2127` | Deployment-owner release/pin decision and rollout gates | Exact deployed version and native read behavior verified by that owner; source merge is not deployed qualification. |

## Reproduction and handoff

Use the repository revision containing the retained trial results. Ordinary
verification does not dispatch readers or generate a replacement population:

```sh
uv sync --locked --extra dev --extra served --extra remote --extra inspection
uv run --locked pytest tests/test_reader_trial_grading.py tests/test_result_packet.py tests/test_docs_integrity.py -q -x
timeout --foreground -k30s 900s scripts/pg-disposable-tests.sh -k test_result_read_packet
```

To reproduce deterministic grading of one retained attempt, use the independent
freeze expectation recorded in the native event readback. For this trial it is
`sha256:c474a753474fc7e57341a07330ebc53466f2628810402a58b624792e34964093`:

```sh
uv run python scripts/grade_resume_reader_trial.py \
  docs/evidence/independent-reader-trial-20261018/oracle.json \
  attempt-4bed32145b02 \
  docs/evidence/independent-reader-trial-20261018/attempt-4bed32145b02/answer.json \
  --freeze docs/evidence/independent-reader-trial-20261018/freeze.json \
  --expected-freeze-sha256 sha256:c474a753474fc7e57341a07330ebc53466f2628810402a58b624792e34964093
```

The recorded event is an independently retained expectation for this experiment,
not a public authenticity trust root. Compare the actual native readback when
available. Changed source, prompt, oracle or input bytes must refuse frozen grading.

A future host/model trial needs its own approved population, unique output
folder, instrumented protocol, new frozen event and new attempt IDs. The opt-in
`RESUME_COLD_TRIAL_OUTPUT` generator must only use disposable PostgreSQL and a
fresh folder. Never replace the retained attempt population or infer live
artifact accessibility from those disposable fixtures.

No release, package version, global install, adapter pin or deployment is changed
by this source handoff. The official0.20.0 pin stays under the deployment owner.
Existing archival fixture guards remain in force. Unknown source authenticity,
cross-read atomicity, target outcome and historical acceptance links remain visible.
