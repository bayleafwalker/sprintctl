# Independent cold-reader fixture results, 18 October 2026

Native work: `sprintctl#2715`. [Protocol](independent-reader-trial-protocol.md)
was frozen in native event5214 before any reader dispatch. The
[retained population and grades](independent-reader-trial-20261018/results.json)
include all nine assigned attempts and108 questions:74 correct known answers,
34 correct unknowns, zero incorrect, unanswered, malformed or timed-out results.
No favorable rerun replaced an attempt. Inputs, answers, individual grades,
oracle, prompt, freeze and timing records are retained alongside the results.

| Paired fixture | Before | After | Observed difference |
|---|---|---|---|
| Checkpoint |12/12;7 known,5 unknown|12/12;8 known,4 unknown|Reader recovered the supplied declared SHA; source bytes stayed unknown.|
| Comparison baseline |12/12;7 known,5 unknown|12/12;9 known,3 unknown|Reader identified changed Release and Decision baselines.|
| Caller access report |12/12;7 known,5 unknown|12/12;7 known,5 unknown|Reader identified inaccessible caller report; actual availability stayed unknown.|

Each of the matching-receipt, changed-artifact and omitted-receipt result
readers answered12/12. A matching historical receipt remained distinct from
whether the requested artifact matched. Terminal acceptance and target effect
remained unknown; no supplied result granted authority.

This is one host and the observed Codex fresh-session collaboration harness.
Readers received no predecessor conversation, but source locations were supplied.
Isolation was instructed, not enforced by OS access controls. Actual tool-call
compliance, exact model revision, cost and active effort are unknown.

Recorded elapsed times were approximately195seconds for the first three and
38seconds for the other six. They include queue, orchestration and observation
delay; the first batch was persisted after a coordinator context boundary.
These times are not comparable recovery-effort measurements or evidence of time
saved. Before/after correctness was unchanged. The additional known facts show
what the repaired context disclosed, not causal or representative improvement.

A preparation-only generator lookup failure was fixed before freeze/readers;
its temporary preparation directory was retained separately. The final native
fixture generated all nine inputs in disposable PostgreSQL and cleaned its rows.
Ordinary CI excludes the opt-in generator at directory collection unless its
output environment is explicitly set, preserving the existing zero-skip PG gate.
Real collection regressions cover both modes; the frozen generator is unchanged.
The independent pre-dispatch review also checked oracle self-consistency;
that check is not counted as reader performance.

Broader `sprintctl#2661` cross-host/model qualification, `sprintctl#2663`
measured recovery effort and their `agentops#2654` prerequisite remain open.
This result establishes neither general transfer qualification nor a provider
comparison.
