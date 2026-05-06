# Sprint Takeup

Sprint takeup is a sprint-level visibility signal. It records that an actor is
currently looking at or operating on a sprint, without claiming any item and
without blocking anyone else.

Use takeup when cockpit, operators, or coordinating agents need to answer "who
is on this sprint right now?" Use claims when an actor needs exclusive ownership
of a work item.

## Model

Takeup writes append-only events to the existing event stream:

- `sprint-taken-up`
- `sprint-released`

The current state is derived by pairing those events by sprint, actor, and
instance id. A release can omit `--instance-id`; in that case sprintctl matches
the most recent active takeup for the same actor.

Takeup has no TTL, heartbeat, claim token, or handoff protocol. It is not proof
of ownership.

## Commands

Mark yourself on a sprint:

```sh
sprintctl takeup take \
  --sprint-id <id> \
  --actor <name> \
  --instance-id "$SPRINTCTL_INSTANCE_ID" \
  --context remote-mode-prep
```

Release the takeup:

```sh
sprintctl takeup release \
  --sprint-id <id> \
  --actor <name> \
  --instance-id "$SPRINTCTL_INSTANCE_ID" \
  --reason done
```

Inspect current takeups:

```sh
sprintctl takeup list --json
sprintctl takeup show --sprint-id <id> --json
```

`sprintctl render` and `sprintctl sprint show --detail` include active takeups
when any exist.

Release stale takeups whose actionq runtime session is no longer active:

```sh
sprintctl takeup sweep --json
```

By default, sweep only releases active takeups that recorded a
`runtime_session_id` and whose session is absent from `actionctl sessions
--active`. To clean up old pre-integration takeups that never recorded a runtime
session, pass a conservative age threshold:

```sh
sprintctl takeup sweep --stale-after 86400 --json
```

Sweep is append-only. It records `sprint-released` events with `actor=sweep`,
`reason=session-not-active` or `reason=no-session-stale`, and
`matched_takeup_event_id` pointing at the takeup being released.

## Force

`takeup take` rejects a second active takeup for the same actor and instance.
Use `--force` only for crash recovery when a prior session did not release:

```sh
sprintctl takeup take \
  --sprint-id <id> \
  --actor <name> \
  --instance-id "$SPRINTCTL_INSTANCE_ID" \
  --force
```

Force does not delete or modify the old event. It records a new
`sprint-taken-up` event with `forced=true`.

## Multiple Active Sprints

Sprintctl now allows more than one `active_sprint` with status `active`.
Commands that default to "the active sprint" continue to work when exactly one
active sprint exists. When more than one exists, those commands fail with a
message listing the candidate sprint IDs and require an explicit `--sprint-id`
or `--id`.

Use this to inspect active sprints:

```sh
sprintctl sprint list --active
```

## Claims Versus Takeup

| Need | Use |
|---|---|
| Show that an actor is looking at a sprint | `takeup` |
| Own a work item for execution or review | `claim` |
| Prevent conflicting item transitions | `claim_id + claim_token` |
| Recover visibility after a crash | `takeup take --force` |
| Transfer item ownership to a new session | `claim handoff` |
