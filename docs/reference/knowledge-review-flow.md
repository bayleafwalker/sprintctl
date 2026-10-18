# Knowledge events, legacy review, and the current Workspace owner

Sprintctl owns work items and their structured events. The retained
[kctl implementation](https://github.com/bayleafwalker/kctl/blob/dcd82ee520f2ec53377c54c29b0bb3de9d228666/README.md)
reads Sprintctl events and maintains a separate local candidate/review store.
It never writes back to Sprintctl. Kctl is retiring; the deployed `knowledge`
domain now belongs to Workspace, with different operations and no kctl bridge.
See the pinned
[owner reconciliation](https://github.com/bayleafwalker/kctl/blob/dcd82ee520f2ec53377c54c29b0bb3de9d228666/docs/evidence/2026-10-18-legacy-knowledge-owner-reconciliation.md).

The local workflow below documents retained behavior for existing local data.
It is not an onboarding path into Workspace and does not import legacy records,
grant knowledge authority or establish acceptance of a served intake request.
Workspace contribution and review require their respective credential authorities
and current membership. Proposal authors cannot review their own proposals;
agents are capped at contributor. Follow the
[Workspace owner contract](https://github.com/bayleafwalker/vuoro/blob/aa5af4de351f011c1d4f795eaa72b692c5dfcb47/docs/architecture/knowledge-workspace-composition.md)
for that separate lifecycle. Repository-authored governing documents remain
Git-owned; Workspace does not replace their source authority.

## Record and read Sprintctl knowledge events

In a configured served repository, record a structured note on the scoped item:

```sh
sprintctl item note \
  --id sprintctl#42 \
  --type decision \
  --summary "Chose public-key verification for cross-service tokens" \
  --detail "Recipients can verify without receiving the signing secret" \
  --tags auth,security
```

Replace the example repository and item with the intended target. Served mode
uses the authenticated actor. Optional `--git-branch`, `--git-sha`,
`--evidence-item-id` and `--evidence-event-id` attach declared provenance;
recording them does not verify source bytes or approve a knowledge proposal.

Kctl's default durable extraction event types are `decision`, `blocker-resolved`,
`pattern-noted`, `lesson-learned` and `risk-accepted`. Sprintctl's raw
`--knowledge` and backlog-seeding filter uses its own four-type set, excluding
`blocker-resolved`; the two filters are not identical. Retained coordination
extraction also
recognizes `claim-handoff`, `claim-ownership-corrected`,
`claim-ambiguity-detected` and `coordination-failure`. The `claim-*` names are
historical event labels, not current CLI claim commands or proof requirements.
New reservation events should not be assumed to enter that legacy filter.
Custom extraction filters may select other event types explicitly.

Read the raw durable events through the owning Sprintctl interface:

```sh
sprintctl event list --sprint-id sprintctl#7 --knowledge --json
sprintctl usage --context --json
```

These are raw event candidates, not reviewed knowledge. A successful source
read or `kctl doctor` check does not prove that the deployed catalog supports
legacy kctl intake or review.

## Retained local extraction and review

Use an established local repository or a separate marker-free recovery directory,
an explicitly selected local Sprintctl database, and the intended Kctl database.
Do not repoint a shared checkout at stale local state. See
[backend and recovery guidance](../guides/remote-mode.md).

```sh
export SPRINTCTL_BACKEND=local
unset SPRINTCTL_URL SPRINTCTL_VUORO_PROFILE
export SPRINTCTL_DB=/path/to/local-sprintctl.db
export KCTL_DB=/path/to/local-kctl.db
kctl extract --sprintctl-db /path/to/local-sprintctl.db --sprint-id 7
kctl review list --kind durable --json
kctl review show --id 42
```

Extraction normally runs Sprintctl's health preflight before reading events.
For local diagnostics, `sprintctl maintain check --sprint-id 7 --json` is
read-only; it has no `--fix` option. `maintain sweep` changes stale item status
and can interrupt stale reservations across the selected database, even with a
sprint filter. It is a separate intended mutation, not an automatic preflight
repair.
In served mode, `maintain check` requests the existing read-only owner
diagnostic with server thresholds; `maintain sweep` remains unavailable. The
retained served Kctl source requests its own catalog diagnostic and fails on a missing
operation; it does not reconstruct that diagnostic from local state.
`--no-preflight` explicitly skips the check and does not fix a diagnostic or
supply missing authority.

Local review uses these commands:

```sh
kctl review approve --id 42 --reviewer operator
kctl review reject --id 43 --reason "Insufficient reusable evidence" --reviewer operator
```

Approval may include `--title`, `--detail` or `--tags` overrides; there is no
separate `review edit` or `review discard` command. Rejection records a rejected
candidate rather than deleting its history. Approval alone does not publish.
Local publication is a separate `kctl publish --id 42 --category decision
--body "Reviewed text"` operation. Supersession is supplied with `--supersedes`
on publication, not as a review subcommand.

Coordination candidates are listed with `kctl review list --kind coordination`.
They stay separate from the default durable publication stream. The retained
local publisher has an explicit `--coordination` option; this does not turn
coordination records into durable knowledge or a Workspace import.

## Use reviewed records to inform explicit work changes

Kctl output can inform a person's or agent's decision to create Sprintctl items.
It does not authorize or issue those writes. In local mode, `kctl status --json`,
`kctl review list --json` and `kctl render` inspect the local pipeline and its
derived publication view.

The retained local `sprint backlog-seed` command consumes raw Sprintctl durable
events, not Kctl's approved store:

```sh
sprintctl sprint backlog-seed --from-sprint-id 7 --to-sprint-id 8 --actor operator
```

This is a local mutation, unavailable in served mode. It creates backlog items;
it is not knowledge review or publication. Shared work changes use supported
served item/sprint commands explicitly, with the normal owner checks.

## Snapshots and authority

For local work, `sprintctl render --sprint-id 8` produces a derived snapshot that
can be reviewed and committed. Inspect successful output before replacing an
existing snapshot. The current served CLI refuses `render`; preserve an existing
snapshot and record that limitation instead of generating a local substitute or
redirecting a failed command over the committed file.

Authoritative shared item state remains in the served owner. A local database is
an authority only for its own local work; committed renders are historical views.
Kctl's retained local store owns its local candidate/review history, while
Workspace owns its separate served lifecycle. None of these derived views
confers another owner's acceptance or credentials.
