You are working on a minimal, agent-centric sprint coordination system (`sprintctl`) for a monorepo.

Your task is to **document, refine, and finalize the architecture, specifications, and implementation plan** for this system so that it can be implemented by another agent without ambiguity.

---

# Context

This system is **not a general project management tool**.

It is a **lightweight coordination layer for agent-driven work**, designed to:

* support parallel agent/subagent execution
* avoid direct shared markdown editing
* maintain clear, time-bound sprint state
* enforce process consistency where needed
* allow derivation of durable knowledge and future work

The system must explicitly separate:

1. **State (facts)**

   * sprint, track, work item, claim, event, handoff, pending items
   * timestamps, ownership, status, relationships

2. **Workflow policies (rules)**

   * claim TTLs, heartbeat requirements
   * staleness thresholds
   * allowed transitions
   * validation requirements
   * daemon/sweeper behavior

3. **Hard process enforcement**

   * implemented via CLI/service/daemon
   * claim expiry, stale detection, validation checks, transitions

4. **Soft guidance**

   * skills, templates, conventions
   * handoff structure, note quality, heuristics

These must remain **strictly separated**.

---

# Objectives

Produce a **clear, implementable architecture and spec** that:

* avoids ambiguity in multi-agent/swarm scenarios
* distinguishes between "inspect", "execute", "review", and "coordinate" work
* supports multiple parallel work tracks within a sprint
* keeps docs time-bound and auto-derived where possible
* prevents stale/ambiguous work states
* allows promotion of durable knowledge and future work

---

# Required Output

Structure your response into the following sections:

---

## 1. Architecture Overview

Define the system at a high level:

* core components (state layer, policy layer, process layer, guidance layer)
* responsibilities of each
* data flow between them
* where SQLite, CLI, daemon, and repo docs fit

Be explicit about **boundaries and responsibilities**.

---

## 2. Data Model Specification

Define all core entities with fields and purpose:

* Sprint
* Track
* WorkItem
* Claim (with claim types: inspect / execute / review / coordinate)
* Event (with source_type: actor / daemon / system)
* Handoff
* PendingItem
* KnowledgeCandidate (optional but defined)

For each:

* required fields
* optional fields
* relationships
* lifecycle notes

Keep schema minimal but sufficient.

---

## 3. Workflow Policy Model

Define how policies are represented and applied:

* central policy configuration structure
* claim policies (TTL, heartbeat, exclusivity)
* transition rules
* validation requirements
* staleness rules
* daemon/sweeper behavior

Explicitly show:

* example policy config
* how policies map to state objects
* how overrides (if any) are handled via profiles (not per-field sprawl)

---

## 4. Process & Enforcement Model

Define **hard process behavior**:

* what is enforced vs what is soft
* CLI responsibilities vs daemon responsibilities
* claim lifecycle (create → heartbeat → expire)
* stale detection and handling
* review flow expectations
* carryover logic at sprint close

Clearly distinguish:

* what MUST be enforced
* what SHOULD be suggested

---

## 5. Claiming & Concurrency Model

Define:

* claim types and semantics
* exclusive vs non-exclusive claims
* TTL and heartbeat expectations
* how conflicts are handled
* how expired claims are surfaced
* how agents safely:

  * inspect work
  * take ownership
  * release or transfer work

This section must remove ambiguity in swarm execution.

---

## 6. Track & Parallel Work Model

Define:

* how tracks are represented
* how work items relate to tracks
* how track-level status is derived
* how multiple parallel tracks are surfaced in sprint views

---

## 7. Rendering & Documentation Model

Define:

* what parts of sprint docs are:

  * rendered from state
  * manually authored
* required sections of sprint docs
* freshness model (timestamps, staleness indicators)
* rendering triggers (manual vs daemon)

Ensure docs remain:

* time-bound
* current
* non-authoritative for mutation

---

## 8. Pending Work & Knowledge Promotion

Define:

* separation between active work and pending backlog
* how pending items are created from:

  * events
  * blockers
  * residual risks
* how knowledge/training material is derived
* promotion stages:

  * candidate → approved → published

Avoid mixing these into active sprint state.

---

## 9. CLI & Service Interface

Define minimal command surface:

* sprint commands
* work item commands
* claim commands
* event commands
* handoff commands
* render commands
* process/daemon commands

Keep it small and composable.

---

## 10. Implementation Plan

Produce a concrete phased plan:

### Phase 1 (must ship)

* minimal schema
* core CLI
* basic rendering
* tests for core workflow

### Phase 2

* claims + staleness + daemon
* improved filtering
* carryover logic

### Phase 3 (optional)

* knowledge promotion
* policy profiles
* API wrapper

Include:

* key files/modules
* dependencies
* risks and tradeoffs

---

## 11. Design Constraints & Anti-Goals

Explicitly list:

* what the system must NOT become
* common failure modes (e.g. “Jira creep”, “doc drift”, “policy leakage into state”)
* guardrails to prevent them

---

# Style Requirements

* Be concise but precise
* Prefer structured sections over prose
* Avoid generic architecture language
* Every concept must map to something implementable
* If something is optional, mark it clearly
* If something is enforced, say how

---

# Final Check

Before finishing, ensure:

* no policy is embedded as a document attribute
* no hard process relies on agent goodwill
* no state object contains duplicated policy logic
* claim and concurrency model is unambiguous
* system remains minimal and buildable

---

Deliver a complete, coherent spec that another agent can directly implement without reinterpretation.
