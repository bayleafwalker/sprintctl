# Reservation Discipline

Reservations are the coordination signal for `sprintctl` items. This guide
defines the minimum operating discipline for reliable multi-session work.

## What a Reservation Is

A reservation is a visible, credential-free signal that an actor in a session
is working on an item. It is not proof of ownership and it does not gate
mutations.

- `reservation_id` is a handle, not a secret.
- Metadata fields (`actor`, `session_id`, `instance_id`, branch, worktree,
  hostname, pid) are advisory only. They provide traceability, not
  authorization.
- Multiple active reservations on the same item are surfaced as conflicts, not
  blocked.

## Startup Sequence

1. read context: `sprintctl usage --context --json`
2. reserve item with durable output:

```sh
sprintctl reservation reserve \
  --item-id <id> \
  --actor <name> \
  --role execute \
  --session-id "$SPRINTCTL_RUNTIME_SESSION_ID" \
  --json
```

3. persist `reservation_id` for the full session

## Activity Rule

Touch the reservation when useful, especially before a long pause or at the
end of a focused block:

```sh
sprintctl reservation touch \
  --id <reservation-id> \
  --session-id "$SPRINTCTL_RUNTIME_SESSION_ID"
```

There is no TTL, no heartbeat contract, and no lease to violate. Staleness is
display-only.

## Status Transition Rule

Item status updates use expected-revision compare-and-swap, not reservation
proof:

```sh
REV=$(sprintctl item show --id <item-id> --json | jq -r '.item.status_revision')
sprintctl item status \
  --id <item-id> \
  --status active|done|blocked \
  --actor <name> \
  --expected-revision "$REV"
```

Treat status transition and reservation as separate operation boundaries:
first mutate status, then release or reassign the reservation.

## Recovery Rule

If session state is lost, there is no token to recover:

```sh
sprintctl reservation list --all --json
```

Reassign an existing active reservation to the current session, or release it
and create a new one if the old session is gone.

```sh
sprintctl reservation reassign \
  --id <reservation-id> \
  --actor <name> \
  --session-id "$SPRINTCTL_RUNTIME_SESSION_ID" \
  --json
```

## Shutdown Rule

Before exit, every active reservation must be:

- reassigned to the next runtime (`reservation reassign`), or
- released (`reservation release`)

Then emit a handoff bundle for session resumption:

```sh
sprintctl handoff --format json --output handoff.json
```

## Related

- [Coordinator Mode](coordinator-mode.md)
- [Work Loop](../guides/work-loop.md)
- [Resume Work](../guides/resume-work.md)
