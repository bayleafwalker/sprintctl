# Changelog

## Unreleased

### Added

- `work.run.resolve-v2`: resolve-v1's binding, under the same binding rule,
  plus `issued_at` (the owner's `run.created_at`), `superseded_by_run_id`
  (the first run registered with this run as `predecessor_run_id`, else
  null) and `state` (`active` or `superseded`; a run has no recorded close,
  so there is no `closed`). resolve-v1 is unchanged: its catalog descriptor
  is byte-frozen, so the new fields are a new version.
- Structured approvals: `work.approval.record-v1` records an immutable
  approval of a `transition` for a `run_id` until `expires_at` (at most 7
  days), as the authenticated approver, under the new `work:approve`
  authority; `work.approval.get-v1` (`work:read`) reads one by
  `approval_id` with the owner's `evaluated_at` and `expired`. PostgreSQL
  only; standalone SQLite refuses them with `unsupported-backend`.
- PostgreSQL schema 26 (`_apply_schema_version_26`,
  `sprintctl/approval_schema.py`) adds the append-only `work_approval`
  table. The compatibility handshake publishes `approval_storage` with an
  exact catalog fingerprint. The runtime admits exactly schema 26, so the
  next release is a coordinated cutover.

### Changed

- `work.effect.export-proposal-v3` admits schema 24, 25 or 26.
- Integration-test cleanup (`pg_testing`) and the archival-result closure
  know `work_approval`; an archival closure refuses a run with approvals.

## 0.19.0

### Added

- Agent directory and durable inbox, store side of vuoro contract
  `agent-directory-and-inbox` v1 (with the vuoro#219 `launch-ambiguous`
  amendment). Ten served operations: `work.agent.register-v1`,
  `observe-v1`, `retire-v1`, `list-v1` and `describe-v1`, and
  `work.message.send-v1`, `read-v1`, `status-v1`, `receipt-v1` and
  `cancel-v1`. They run on PostgreSQL only; standalone SQLite refuses them
  with `unsupported-backend`. See `docs/reference/agent-inbox.md`.
- PostgreSQL schema 25 (`_apply_schema_version_25`,
  `sprintctl/agent_inbox_schema.py`) adds instances, declaration revisions,
  observations, the per-repository `inbox_seq` counter row, messages and
  append-only receipts. The compatibility handshake now publishes
  `agent_inbox_storage` with an exact catalog fingerprint. The runtime
  admits exactly schema 25.
- CLI: `sprintctl agent register|observe|retire|list|describe`,
  `sprintctl message send|read|status|receipt|cancel`,
  `sprintctl run context --run-id` (`work.run.predecessor-context-v1`) and
  `sprintctl run note --run-id --note --idempotency-key`
  (`work.session-note.write-v1`). They read and print JSON, need served mode,
  and add the `agent-inbox/v1` capability.
- Test support: `sprintctl.pg_testing.freeze_inbox_clock`,
  `shift_inbox_clock` and `release_inbox_clock` control the owner's clock for
  one session. They take effect only after one of the first two switches on a
  process-local flag, and only on a database commented as disposable. A
  session setting alone is ignored.

### Changed

- `work.effect.export-proposal-v3` now admits schema 24 or 25 instead of
  exactly schema 24.
