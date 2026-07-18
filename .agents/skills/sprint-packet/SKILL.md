---
name: sprint-packet
description: Use when accepted scope needs to become an implementation-ready sprint packet and work items. Do not use for open-ended brainstorming, requests missing scope decisions, or resuming an item that already exists in sprintctl.
---

## Goal

Convert accepted scope into a concise sprint packet with clear deliverables, dependencies, acceptance criteria, and verification, then register it in `sprintctl`.

## Inputs

- The accepted scope or approved request.
- Governing requirements, architecture docs, and relevant plan or sprint references.
- Known constraints, dependencies, and verification expectations.

## Steps

1. Confirm the scope is decided enough to plan without inventing product direction.
2. Confirm the work is not already represented as an active or pending `sprintctl` item. If it exists, stop and resume from live sprint state instead.
3. Pull the requirements, architecture docs, and prior sprint material that define the work.
4. Draft the packet in this order: goal, scope, out-of-scope, dependencies, deliverables, acceptance checks, verification path.
5. Slice work by repo contracts and layers rather than vague task buckets.
6. Call out any required doc, requirement, fixture, or contract updates needed to complete the sprint.
7. After the packet is agreed, register it in `sprintctl`:
   - Load the project DB first via `.envrc` or exported `SPRINTCTL_DB`.
   - `sprintctl sprint create --name "<ID> — <name>" --start <YYYY-MM-DD> --end <YYYY-MM-DD> --status active --kind active_sprint`
   - `sprintctl item add --sprint-id <id> --track <track> --title "<title>"` for each deliverable.
   - Use `--kind backlog` when the packet represents parked scope rather than active delivery.
   - Run `sprintctl render` and confirm output matches the agreed packet before starting implementation.

## Output Contract

- A sprint-ready packet with clear deliverables and acceptance.
- Dependencies and blockers separated from in-scope work.
- A concrete verification path usable during implementation and handoff.
- A `sprintctl` sprint registered with items added.

## Do Not

- Do not turn unresolved product questions into fake implementation tasks.
- Do not hide architecture or contract work inside generic backlog bullets.
- Do not use for direct implementation when no planning artifact is needed.
- Do not use when the relevant sprint item already exists in live `sprintctl` state.
