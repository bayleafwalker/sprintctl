# Per-repo authority + projection cutover dogfood (item #1163)

> **Archived.** The `sprintctl pilot` command surface this procedure drives was
> retired, and `sprintctl/pilot.py` and `sprintctl/cutover.py` were deleted. The
> steps below are no longer executable and are kept only as a record of what
> Phase 28 set out to prove. `sprintctl sync` replaced `pilot sync`.

Phase 28 built three independent per-repository opt-in flags:

- the observation-only **shadow pilot** (`sprintctl/pilot.py`) — mirrors
  authoritative events into a local outbox and syncs them into a cached
  projection;
- the **authority-command rollout mode** (`sprintctl/authority_config.py`) —
  `off` / `shadow` / `enforce`;
- **guarded projection reads** (`sprintctl/projection_reads.py`) — some read
  surfaces served from the cached projection instead of the backend, with
  freshness disclosure.

Each shipped and is tested independently. What Phase 28 proved was
**observation shadowing**, not an authoritative command or normal-read
cutover for any specific repository. This page is the operator procedure for
running this repository (`sprintctl` itself) through that cutover as a
dogfood, and the evidence `sprintctl pilot cutover-evidence` assembles to
support the promotion decision.

**Scope.** An opt-in `sprintctl`-repo pilot: parity histories,
watermark/reconciliation lag, stale-tool incidents, a rollback rehearsal, and
an explicit promotion gate.

**Non-scope.** This is not a fleet cutover and it does not delete a backend
(see [`adr-outbox-sync-model.md`](../plans/adr-outbox-sync-model.md)). It
never decides to promote a repository by itself — `promotable` is evidence
for an operator-directed decision, the same posture
[`capability-receipts.md`](capability-receipts.md) documents for capability
receipts. Item #1164 ("retire split backend mode") depends on this evidence
but is explicitly out of scope here.

## Running the dogfood

1. Opt this repository into the shadow pilot and let it observe real
   traffic for a while:

   ```bash
   sprintctl pilot enable
   sprintctl pilot sync            # after some authoritative activity
   sprintctl pilot verify --sprint-id <id> --json
   ```

2. Optionally move the authority-command mode to `shadow` (never `enforce`
   during the dogfood itself — that is the eventual promotion, not evidence
   for it):

   ```bash
   sprintctl authority-config set --mode shadow
   ```

3. Assemble the evidence packet:

   ```bash
   sprintctl pilot cutover-evidence --sprint-id <id> --json
   ```

   This combines, read-only except for the rehearsal noted below:

   - **config** — the three flags' current state;
   - **parity** — `sprintctl pilot verify`'s comparison between the
     authoritative event history and the mirrored shadow observations for
     one sprint (omit with `--skip-parity` before the pilot has ever
     synchronized; the gate then blocks on `parity-not-evaluated`);
   - **watermark** — the cached projection's freshness assessment
     (`sprintctl/projection.py:assess_freshness`), bounded by
     `--max-watermark-age-seconds` (default: the same
     `DEFAULT_STALE_AFTER_SECONDS` guarded reads use);
   - **stale_tools** — `sprintctl doctor`'s existing read-only provenance and
     capability findings, reused as-is; any `error`-severity finding is a
     blocking incident;
   - **rollback_rehearsal** — see below; skip with
     `--skip-rollback-rehearsal`;
   - **promotable** / **blockers** — the explicit gate. `promotable` is
     `true` only when every other section above is green.

4. Review `blockers`. Each one names exactly what is not yet evidence-backed
   green: `pilot-not-enabled`, `parity-not-evaluated`, `parity-diverged`,
   `watermark-<reason>` (`missing`, `never-synchronized`,
   `schema-upgrade-required`, or `stale`), `stale-tool-incidents`, or
   `rollback-rehearsal-failed`.

5. Promotion itself — moving the authority-command mode to `enforce` and/or
   enabling guarded projection reads as the repository's normal path — is a
   separate, explicit operator action once `promotable` is `true`:

   ```bash
   sprintctl authority-config set --mode enforce
   sprintctl projection-reads enable
   ```

   `sprintctl pilot cutover-evidence` never runs these itself.

## The rollback rehearsal

`rehearse_rollback` (`sprintctl/cutover.py`) proves the rollback path works
*before* an operator needs it under pressure: it disables the
authority-command mode and guarded projection reads, confirms the disabled
state took effect, then restores whatever was configured immediately before
the rehearsal ran. Running the evidence command itself therefore never
changes this repository's opted-in state by side effect — only an explicit
`sprintctl authority-config set` / `sprintctl projection-reads enable|disable`
does that.

The rehearsal only ever touches the two small per-repo JSON files those
modules own (`.sprintctl/authority-command.json`,
`.sprintctl/projection-reads.json`). Per those modules' own invariants
(see their docstrings), neither can write authoritative backend or
projection data, so the rehearsal cannot corrupt or diverge either.

**Rollback**, if promotion evidence later turns out to be wrong: disable the
per-repo flag(s) and retain the current backend.

```bash
sprintctl authority-config set --mode off
sprintctl projection-reads disable
```

Both are additive/guarded read paths and configuration files; disabling them
returns every read and command surface to its unconditional current-backend
behavior, exactly as the rehearsal above demonstrates.
