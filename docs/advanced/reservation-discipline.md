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
  blocked. `reserve` never refuses because someone else got there first; it
  returns `conflict`, `conflicting_reservations`, and `conflict_severity`
  (`warning` when two sessions both claim `execution`).
- Roles describe the relationship to the work — `execution`, `verification`,
  `observation` — which is what makes an overlap classifiable.
- `--interrupt-existing` is the deliberate takeover: it interrupts the item's
  active `execution` reservations with a recorded reason and audit event. Use
  it when you mean to displace someone, never merely to coexist.

## Startup Sequence

1. read context: `sprintctl usage --context --json`
2. reserve item with durable output:

```sh
sprintctl reservation reserve \
  --item-id <id> \
  --actor <name> \
  --role execution \
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

Touching is rarely necessary inside sprintctl: `last_activity_at` advances
implicitly whenever your session successfully mutates the item (status, edit,
note, ref, dep), attributed by session id rather than actor name. Reach for
`touch` when the work is happening elsewhere — a long build, external review,
git-only stretches.

There is no TTL, no heartbeat contract, and no lease to violate. Staleness is
display-only: an active reservation is marked `stale` after
`SPRINTCTL_RESERVATION_STALE_AFTER_HOURS` (default 4), and only an explicitly
invoked `sprintctl maintain sweep` interrupts reservations idle longer than
`SPRINTCTL_RESERVATION_INTERRUPT_AFTER_DAYS` (default 7). Nothing expires in
the background.

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
