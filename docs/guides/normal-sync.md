# Normal synchronization

Sprintctl keeps a durable, repository-local producer outbox and a read-only
projection cache beneath `.sprintctl`:

- `sync-outbox.db` holds locally authored observations and explicit authority
  requests.
- `sync-projection.db` caches remote observations and authority decisions.

Observations append to the outbox as normal work memory; no rollout flag is
needed. Run synchronization against a configured served backend to upload a
bounded batch and atomically advance both cached watermarks:

```bash
sprintctl sync --batch-size 100 --json
```

The command is safe to retry. Remote ingest deduplicates producer stream
records, so an interrupted response cannot create a second observation.
Authority requests remain durable when their outcome is unknown. Normal sync
may pull a decision that already exists, but it never originates or retries an
authority effect; use the explicit authority reconciliation command to do so.

## Vuoro-Release trailer harvest

Before uploading, both `sprintctl sync` and `sprintctl authority sync` (served
mode) scan the checkout that owns `.sprintctl` for `Vuoro-Release:` commit
trailers. They enqueue one `release.commit-observed` observation per
(commit, digest) with `{release_digest, commit_sha, ref, remote_hint}`:

- The scan follows the ref `HEAD` points at (`refs/heads/<branch>`, or `HEAD`
  when detached) from a per-ref cursor stored in the outbox database
  (`release_trailer_cursor`). The first harvest of a ref scans the last 500
  commits, and so does a harvest whose cursor is no longer an ancestor of the
  tip (after a force-push). Re-running with no new commits does nothing, and a
  pair already in the outbox is never enqueued twice.
- Only the form is checked: a 40-hex commit and a 64-hex digest. An optional
  `sha256:` prefix is removed and hex is lower-cased. Malformed trailers are
  counted under `release_trailers.malformed` in the sync result and never
  enqueued.
- `remote_hint` is the `origin` URL with userinfo, query and fragment removed,
  so a token-bearing remote URL never reaches the outbox. A hint that still
  looks like a credential is dropped (`null`).
- In served mode the observation actor is the authenticated identity
  (`work.identity.current`). It is resolved only when something is enqueued.
- In served mode the harvest runs only against a server whose catalog
  advertises `release.commit-observed` among the record types
  `work.batch.apply` accepts. Against an older server nothing is enqueued, the
  cursor stays put, and the rest of the sync proceeds; the same commits are
  harvested once the server is upgraded.
- The harvest never fails a sync. A missing `git`, an unreadable repository or
  a failed identity or catalog lookup reports `release_trailers.status:
  skipped` with the reason in `detail`.

On ingest the server records the pair in `release_commit` (idempotently) when
the digest is a release of the repository. An unknown digest is kept only as
its ingest record and never fails the batch.

Local (SQLite) mode has no harvest: it has no ingest ledger and no sync pass
to carry the observations.

## Upgrading from v0.2

On the first normal append or synchronization, Sprintctl copies a legacy
`shadow-pilot-outbox.db` or `shadow-pilot-projection.db` into the normal
locations if those locations do not already exist. The legacy files remain
untouched so that a retained v0.2 backup can be used for rollback. Once the
normal files exist they always win, making the migration idempotent.

Projection-backed reads remain guarded by `projection-reads`. They explicitly
fall back to the authoritative backend when the normal cache is absent, stale,
or incompatible.
