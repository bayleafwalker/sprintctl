# Changelog

## Unreleased

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
  one session.

### Changed

- `work.effect.export-proposal-v3` now admits schema 24 or later instead of
  exactly schema 24.
