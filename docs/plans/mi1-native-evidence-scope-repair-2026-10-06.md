# Native evidence transport repository scope repair

Served agentops item 2618; operator-authorized MI-1 commissioning on 2026-10-06.
A real native run registered on service 0.1.85, but durable producer sync was
refused at `work.run.resolve-v1` with `repo-id-required`. No append was attempted.
The pending request and original run binding remain retained in the ordinary
producer outbox; a refusal is not a confirmation or permission to replace it.

The CLI already resolves the repository from its served backend configuration.
The native transport helper omitted that scope when invoking both owner
operations. Require an explicit `repo_id` keyword in the helper and pass the
configured repository from `authority evidence-sync`, as other served helpers
already do. Never infer scope from a run ID, profile name or provider payload.
No owner semantics, authority, retry key, chain fields or ledger change.

Two regression cases failed before repair because the helper did not accept
repository scope. After repair the SDK receives the explicit repository for
run resolution and evidence append. Existing CLI retry tests assert forwarding
at the call site, and unrelated operations remain refused before client use.

Runtime acceptance retries the unchanged pending provider request through the
reviewed source fallback after landing, then compares its receipt and tail,
recaptures the exact bytes and verifies no additional append. Do not claim live
reply-loss or concurrent-producer testing from this repair; the separate owner
verification packet provides those bounded fault histories.

Validation: 92 focused tests passed. Full unit run: 1,723 passed, 697
optional/PostgreSQL skips and 14 performance deselections; the sole environment
refusal was the unchanged YAML contract unable to write uv cache in the
sandbox. Its escalated rerun passed. Exact-head CI must pass before landing.
The locked isolated worktree environment is authoritative; the shared source
venv had stale installed distribution metadata and was not used for the final
full run. The doctor unit fixture now stubs its identity probe as well as its
catalog probe, with a displayed-actor assertion, avoiding unintended network IO.
