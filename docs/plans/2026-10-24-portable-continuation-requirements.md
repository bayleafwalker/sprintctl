---
doc_id: portable-continuation-requirements-2026-10-24
status: candidate
supersedes: null
---

# Portable continuation requirements

Candidate requirements for a larger continuation feature batch. This document
does not approve implementation scope or change an owner contract. The local
roadmap names `sprintctl#2661` for independent-reader field qualification and
`sprintctl#2662` for result packets; it does not establish that either item
authorizes owner-side page aggregation or a new portable continuation format.
Confirm the owning issue and scope before treating the conditional aggregation
requirements below as build-ready.

## Existing behavior to preserve

`run brief` makes one call to the existing
`work.run.continuation-delta-v1` operation. Its JSON output is the complete
owner response, including the owner snapshot, reference page, reference cursor,
independent predecessor note and evidence cursors, and completeness flags.
Callers may explicitly send a returned cursor to request another bounded page;
the CLI does not fetch pages automatically. Text is a presentation of the one
response and declares its reference excerpt. `delta.current` is the checkpoint
for a later comparison. The Git `source_checkpoint` is a separate, owner
declared continuation position.

Preserve these properties:

- The existing operation and response remain authoritative. Do not silently
  redefine a one-page response as a complete history.
- Preserve unknown, changed, and unchanged distinctions and all raw owner
  fields. A caller presentation or packet must not upgrade owner assertions.
- A cursor is explicit continuation input, not a credential or transferable
  authority. The operation remains bound to the caller's run and item.
- Owner-record references do not establish that source bytes exist, match a
  declared digest, are current, or can be accessed from a different host.
- Missing baselines, incomplete pages, stale checkpoints, and refusals remain
  visible. They cannot be interpreted as “no changes.”
- Handoff Git metadata remains caller-supplied observation; absent observations
  mean unknown cleanliness. No local database fallback is permitted in served
  mode.

The result packet remains an offline wrapper around complete supplied owner
responses. Its self-digest, packaging time, and opaque caller source reference
do not establish authenticity, currentness, page coverage, or source access.

## Conditional bounded aggregation

If and only if the owner approves a feature scope that includes aggregation,
the caller-facing continuation workflow may automate explicit repeated reads
over the existing operation. First determine whether aggregation belongs in
the CLI/client, a separately approved owner operation, or a different item;
this candidate does not choose that boundary.

Any approved aggregator must:

1. Take an explicit page budget and retain the existing per-call owner limit.
   Stop at the budget; never chase cursors without a finite bound.
2. Send each exact returned cursor with the original run/item and checkpoint
   basis. It must not synthesize or repair cursors, alter page contents, or
   substitute local state.
3. Keep pages as distinct raw owner responses with their request cursor,
   response snapshot identity, page ordinal, and observed time. It may offer a
   derived view, but must retain the page records unchanged.
4. Stop on a null cursor, the declared budget, a duplicate/repeated cursor, an
   invalid cursor, or any owner error. Report why it stopped and whether more
   content may remain.
5. Never label a concatenation “complete” merely because the final call
   returned no cursor for one list. Completion must be assessed independently
   for references, predecessor notes, predecessor evidence, and decision
   history using each owner's explicit cursor/completeness facts.
6. Never retry a refused or expired page with a new snapshot basis and merge
   the result into earlier pages as if it were one snapshot. A restart must be
   explicit and represented as a new collection.

If the approved owner contract cannot bind all pages to a stable snapshot, the
aggregate is a collection of page observations, not an atomic snapshot. The
output must say so. It may not claim an exact complete history across concurrent
changes.

## Per-page provenance and coverage

Every portable collection must retain, per page and per independently paged
list:

- operation/schema version and repository, run, and item identity;
- request cursor (or explicit first-page marker), returned cursor, and page
  ordinal;
- owner snapshot/checkpoint digests exactly as returned, plus any mismatch or
  absence; do not recompute these into a stronger identity;
- caller-observed collection time with timezone and clock provenance stated;
- raw response bytes or a lossless representation, with a digest whose domain
  is named. A collection digest detects change to collected bytes but does not
  authenticate the owner;
- completeness state: complete, partial due to budget, refused/stale, malformed,
  or unknown, separately for each list;
- stop reason and all warnings/errors, including repeated cursor or mixed
  snapshot identities.

Report the number of pages and records actually retained. Empty arrays are not
evidence of an empty source if completeness is false or unknown. Keep the
current owner `reference_git_correspondence`, `source_bytes`, and
`external_missing_content` classifications as returned; they remain unknown
unless an independently authorized source check establishes otherwise.

## Stale snapshot and refusal handling

The current cursor carries an expected snapshot digest and checkpoint digest.
Treat owner refusal for stale/mismatched basis, expired cursor, changed run or
item binding, missing authority, or unavailable operation as a hard boundary.
Preserve the exact error code and associated owner evidence. Do not drop the
cursor, silently restart from page one, use a later snapshot to fill a gap, or
report partial pages as current and complete.

The reader must distinguish at least:

- complete under one owner snapshot, when the owner contract explicitly
  supports that assertion;
- partial because a finite caller page budget ended;
- refused because the cursor/basis was stale or invalid;
- pages obtained under inconsistent or unverifiable snapshot identities;
- unable to determine completeness.

On refusal, retain earlier pages as historical observations, mark the overall
collection incomplete, and instruct the caller to initiate a fresh collection
if appropriate. A fresh collection has its own identity and is never merged
silently with the refused attempt.

## Clean-host source access boundary

Continuation records and source references can be moved to a clean host as
data. They do not carry the predecessor's credentials, grants, run ownership,
local files, database, mounts, Git checkout, or source-provider access. The
reader must not dereference a URL, execute a reference, fetch an artifact, or
infer access from a URL being present.

Any later source inspection requires a separately declared provider and
authorization path, an explicit user or owner action under that path, and
recorded host/provider/identity and access outcome. Record whether bytes were
actually obtained, their digest and digest domain, and whether that digest was
compared to an owner-declared value. Failure, unavailable credentials, blocked
network, absent content, and unchecked source remain distinct outcomes. A
matching digest can establish byte equality for the compared bytes only; it
does not establish publication authority, currentness, target effect, or
authorization to act.

## Acceptance evidence

Before implementation can be accepted, the owning issue must confirm the
feature boundary and explicitly authorize any owner contract change or
automatic aggregation. Then provide reviewable evidence for:

- regression of current one-page JSON preservation, text disclosure, explicit
  cursors, and no automatic reads when aggregation is outside approved scope;
- when aggregation is approved: independent cursors, finite budgets, each stop
  condition, no duplicate-page loops, and per-list completeness accounting;
- stable, changing, stale, mixed, malformed, and refused snapshot/cursor cases,
  including proof that no pages from separate collections are silently merged;
- per-page raw response retention, provenance, collection digest verification,
  and honest partial/unknown labeling;
- clean-host operation with no inherited credential or implicit URL/artifact
  access, plus explicit tests/evidence that source-access outcomes remain
  separate from owner record assertions;
- independent fresh-reader inspection of both a complete supported collection
  and partial/refused collections, with incorrect inferences and missing
  information retained;
- provider/deployment qualification only where the owning acceptance scope
  requires it. Source-only unit evidence must not be presented as deployed
  behavior or cross-host qualification.

Passing serialization checks or a packet self-hash alone does not close the
broader continuation outcome. The roadmap's independent-reader and field
qualification evidence, owner joins, and any deployment gates remain governed
by their owning items.

## Local basis checked

- [Result read packet](result-read-packet.md): offline packaging and integrity
  limits.
- [Resume and explain roadmap](resume-and-explain.md): R1 one-read behavior,
  R2 field qualification distinction, and issue mapping.
- [Context and handoff reference](../reference/context-and-handoff.md): served
  handoff snapshot and `run brief` behavior.
- `sprintctl/vuoro_adapter.py` and `sprintctl/work_application.py`: current
  cursor and result contracts and owner read implementation.
- `sprintctl/briefing.py`: text is a one-response rendering and exposes
  completeness/cursor caveats.

These are local repository sources. No connected issue tracker evidence was
available in this work step, so issue approval and live status remain
unverified.
