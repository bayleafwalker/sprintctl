---
doc_id: sprintctl.projection-recovery
status: draft
supersedes: null
---

# Cached projection fault and recovery boundary

This document closes four read-side failure outcomes required by
`adr-outbox-sync-model`. The local SQLite cache is disposable evidence; the
remote ingestion ledger remains the source of truth. These histories authorize
no mutation or repair against a shared remote backend.

## Outcomes

| Fault | Durable observation | Required outcome | Safe recovery |
|---|---|---|---|
| Crash during projection apply | Prior records and prior watermark | The apply transaction rolls back completely; the watermark never describes a partially applied batch | Re-read from the unchanged watermark and retry the same remote range |
| Projection schema rebuild | Empty initialized cache and complete retained remote log | Replay starts at offset 1 and produces the same ordered envelopes and final watermark | Replace the disposable cache only after the fresh replay reaches the expected remote offset |
| Remote advance while offline | Local watermark remains fixed while higher remote offsets exist | Offline reads report the old watermark and its age; they do not imply current authority | On reconnect, request strictly after the visible watermark and apply every offset in order |
| Remote log retained past the local cursor | First available remote offset is greater than `watermark + 1` | Raise `ProjectionGapError`; insert no suffix records and do not advance the watermark | Stop normal replay. Install a separately validated snapshot with an explicit base offset, then resume after that base |

Snapshot creation, validation, and installation are not implemented by this
verification-only tract. A retained suffix is never treated as a snapshot, and
the cache must not skip to its first offset. Until a trusted snapshot path is
implemented, the safe recovery result is an explicit blocked rebuild rather
than a fabricated current projection.

## Executable mapping

| Invariant | Implementation anchor | Executable history |
|---|---|---|
| Records and watermark commit atomically, then retry converges | `sprintctl/projection.py:apply_ingested_records` and `sprintctl/sync.py:synchronize_outbox` | `tests/test_projection.py:test_failed_record_application_rolls_back_records_and_watermark` and `tests/test_sync.py:test_sync_recovers_after_lost_response_or_projection_apply_failure` |
| Offline catch-up consumes the contiguous suffix | `sprintctl/projection.py:apply_ingested_records` | `tests/test_projection.py:test_offline_catch_up_and_full_log_rebuild_converge` |
| Complete-log rebuild converges | `sprintctl/pg.py:list_ingested_records` and projection apply | SQLite history above plus `tests/test_pg_integration.py:test_disposable_remote_history_rebuilds_projection` |
| Retention cannot jump a cursor | `sprintctl/projection.py:ProjectionGapError` | `tests/test_projection.py:test_retained_suffix_cannot_advance_an_empty_projection` |

The PostgreSQL expansion is guarded by disposable credentials and repository
scope. The committed packet
`verification/contexts/projection-fault-recovery.json` is reusable intent, not
an execution result or a claim of snapshot support.
