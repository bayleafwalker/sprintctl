# Plans Index

Planning now lives here, not in `docs/sprint-snapshots/`.

Plan docs carry `doc_id` / `status` / `supersedes` frontmatter, and work items
link back to them via doc refs — see
[docs/reference/doc-refs.md](../reference/doc-refs.md).

Primary plan documents:

- [ADR: Outbox and Synchronization Model](adr-outbox-sync-model.md)
- [Roadmap Reset](roadmap-reset.md)
- [Operator UX Roadmap](operator-ux-roadmap.md)
- [UX Plan Pack](ux/00-index.md)
- [Doc–backlog linking, Phase 0](doc-backlog-linking-phase0.md)

Other plan and design documents (lifecycle is each file's `status`, in
frontmatter or a body `Status:` line; execution status lives in sprintctl):

- [sprintctl pg backend and remote mode plan](pg-backend-remote-mode-plan.md)
- [Sprintctl alignment with the Vuoro served authority](vuoro-served-authority-alignment.md)
- [Served-Mode Gaps — Implementation Brief](served-mode-gaps-plan.md)
- [Vuoro UX Robustness](vuoro-ux-robustness-plan.md)
- [v3 Clean-Sweep Plan: Claims as Advisory Reservations](v3-reservation-model-plan.md)
- [sprintctl multi-agent takeup plan](sprintctl-multi-agent-takeup-plan.md)
- [Coordinator decisions: terminal-claim recovery and actor convention](2020-2026-authority-recovery-design.md)
- [#1164 gate-evidence ledger](1164-gate-evidence-ledger.md)
- [#1219 Recovery Rehearsal brief](1219-recovery-export-plan.md)
- [S6 ledger checkpoint](2450-s6-ledger-checkpoint.md)
- [Volatile-context native-hook pilot](volatile-context-native-hook-pilot.md)
- [Run continuation: serving read_predecessor_context](2026-09-30-run-continuation.md)

Historical phase snapshots remain in `docs/sprint-snapshots/` only as archived
records. They are not the authoritative backlog source.
