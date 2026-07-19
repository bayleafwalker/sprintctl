# Multi-repository project scope

`--project` is an opt-in, read-only union over repositories selected by a
canonical `project.toml`. It does not create project state in sprintctl, add a
`project_id` column, or change any write command.

Supported views:

```sh
sprintctl sprint list --project /projects/dev/<home-repo>/project.toml --json
sprintctl item list --project /projects/dev/<home-repo>/project.toml --json
sprintctl next-work --project /projects/dev/<home-repo>/project.toml --json
sprintctl next-work --project /projects/dev/<home-repo>/project.toml --json --explain
sprintctl usage --context --project /projects/dev/<home-repo>/project.toml --json
```

When the command runs inside a project repo or materialized folder, omit the
value to resolve from the current directory:

```sh
sprintctl usage --context --project --json
```

Only members with `backlog = true` participate. Every returned sprint, item,
claim, conflict, decision, and next action carries `origin_repo`; text output
groups or labels rows by the same repository identity. This attribution is
required because numeric IDs are repository-scoped and may overlap.

The option accepts no value, the binding file, or a directory. With no value it
starts at the current directory. Directory resolution walks upward for
`project.toml`. From a derived project folder, it also follows the
`canonical_project` pointer in `project.context.json`, so a command run inside a
member worktree can use the folder or current directory as its project path.

## Sprint selection

Without `--sprint-id`, project context and next-work views select each member's
single non-closed `kind = "backlog"` sprint. If a member has no backlog, its
single active `active_sprint` is used as a compatibility fallback. Multiple
eligible backlogs or active sprints are reported as an unavailable repository;
other unambiguous members remain visible. `--sprint-id` looks for that explicit
ID in every selected repository and includes the repository that owns it.

The list views do not choose a default sprint: their existing filters are
applied independently to every selected repository and the rows are then
concatenated in project member order.

## JSON contracts

`next-work --project --json` remains a list, with `origin_repo` added to every
item. `item list` and `sprint list` follow the same rule.

The richer `next-work --json --explain` and `usage --context --json` project
forms use `contract_version = "project-1"`. They contain:

- project identity and ordered backlog repository IDs;
- an aggregate summary;
- attributed union lists for the primary view;
- a `repositories` array retaining each repository's existing version-1
  contract, plus explicit unavailable entries where selection was ambiguous.

The repository-local contracts are not redefined. Project scope wraps and
attributes them.

## Backend boundary

Remote mode creates one read store per member over the same PostgreSQL
connection and changes only the `repo_id` discriminator. This is the only
multi-repository execution path.

Local SQLite mode accepts a project with one `backlog = true` member when that
member matches the current repository. It rejects multi-repository projects
with a clear error; it never opens or combines sibling SQLite databases.

Omitting `--project` is the standalone path. A nearby `project.toml` is not
auto-activated, and the legacy output is byte-identical whether or not that file
exists.
