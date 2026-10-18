# Outbox kernel-sync producer-death verification — sprintctl#2807

This verification-only child adds executable F01/I01 evidence under the ratified
[outbox ADR](../plans/adr-outbox-sync-model.md). It covers local observation and
authority-command request appends. Commands remain requests; this campaign
performs no remote arbitration or authority transition. Parent #1131 stays open.

The accepted product base is `1ec78fa9a2f1dbacc4d5ec832c14b961e09abc12`.
The new helper and fixture bytes were frozen separately before execution; they
were not part of that base commit. Native Release
`9ea503a871eef7c4fd3fc6e22ea8fee2e342fd1903cf66bd67ef502862758171`
and original source-freeze note #5821 identify this child. The separate v2
freeze/campaign chronology is recorded in correction note #5826. All product files remain
unchanged; `sprintctl/releasecore.py` was absent at this base.

The final focused campaign passed **144 tests in 24.06 seconds**, including
**70 new tests**, without skips. These counts reconcile against the retained
[XML](../../verification/traces/outbox-fsync-2807/focused.xml) and
[collected node IDs](../../verification/traces/outbox-fsync-2807/collected.txt).
Four exact-head canonical CI jobs and independent retained-capture review are
still required before publication closeout.

| Evidence | Count | Meaning |
| --- | ---: | --- |
| Observation and command normal appends | 2 | Public append returned normally |
| Actual producer death at qualified entry/exit | 4 | Two record classes × before/after; actual terminal wait status 9 and return code -9 |
| Completed old/repeated sync controls | 2 | Old seed prefix or already-synced target never becomes a new target |
| Generic file sync histories | 6 | fsync/fdatasync observed and killed before/after on owned files |
| Other controls and metadata checks | 56 | Refusals and discriminating guards, not successful recovery histories |
| Retained raw DB/WAL objects | 51 | Includes raw files from refused cases; not 51 accepted histories |

One shared tracer owns spawning, typed syscall entry/exit pairing, descriptor
identity and lifetime checks, kill decisions and exact-PID terminal reaping.
Outbox policy hooks cannot resume, wait for or kill the producer. The controller
starts before BOOT and rejects unsupported topology, descriptor changes, ABI,
marker sequence and unexpected signals. COPY observers and retry workers have
separate PIDs and wait owners, finite output/deadline guards and kill/reap paths.
The raw reader busy timeout is 200ms and its query progress deadline is 500ms.
These are reader bounds only. Untouched product outbox connections retain a
30000ms busy timeout; producer and retry process owners impose external 10s and
3s deadlines respectively. The deliberate pre-BOOT stall uses a 3s guard. No product PRAGMA was tuned.
A stalled COPY while the tracee is stopped and a stalled post-recovery retry are
refused at their exact injected roles and phases. A pre-BOOT stalled producer is
also actually killed and reaped. Startup metadata children are parent-owned and
already terminal before the producer starts; the producer launches none.

Eligibility requires the unchanged seed/header/generation prefix, valid frame
salts and cumulative checksums, and exactly one new final commit marker. It is
not inferred from syscall ordinal. At the same stopped point, three independent
COPY reads establish that the main DB plus candidate WAL contains the full target,
the same DB plus seed WAL contains exactly the seed, and the same DB without WAL
contains no target. Omission can produce no outbox schema; no checkpoint is forced
to manufacture a seed in that copy. Original bytes are checked unchanged.

The oracle checks all 16 persisted record fields and four stream fields against
independently authored known answers. Stream UUID and generated timestamps have
explicit canonical identity/time rules. Recovery after terminal reap must equal
the complete paused candidate or, for allowed before-sync absence, exact seed.
Existing target `created_at` is compared exactly, not only against a time range.
Public retry uses the identical request envelope and preserves all existing state.
Both observed before-sync deaths recovered the complete target. The permitted
seed-only alternative was not exercised at this kernel boundary in this campaign.

The 32 generic refusal controls require their specific exception type and reason;
cleanup injection controls additionally prove the injection actually happened.
The actual O_PATH descriptor error is a kernel EBADF return, separate from the
subsequent SIGKILL return code. Synthetic ABI/syscall/cleanup mutations remain
labelled separately. Ten format controls and three persisted-manifest controls
require their own rejection reason. Valid changed-generation and uncommitted-tail
WAL mutations remain independently SQLite-readable copies; invalid eligibility
does not become a claim of product corruption or automatic product repair.

Actual runtime was Linux 6.18.56 x86_64 on btrfs, Python 3.13.16 and SQLite 3.53.1,
WAL with synchronous=2/FULL and autocheckpoint=1000. Actual producer sync was
syscall 74 (`fsync`) and WAL magic `0x377f0682`. Generic owned-file controls also
exercised syscall 75 (`fdatasync`); that does not imply SQLite used it. The VFS
name is explicitly unknown. Exact source ID, compile options, mount entry and
imported source paths/hashes are retained in each producer runtime record.

SIGKILL preserves kernel caches. This is bounded process-death evidence, with no
power failure, physical-device durability, named-VFS, arbitrary-platform, formal
refinement, deployment, or complete #1131 qualification claim. Unsupported
platforms skip before loading the tracer; a failed supported Linux capability or
ptrace denial fails the campaign and cannot count as passing qualification.

The [source freeze](../../verification/fixtures/outbox-fsync-source-freeze-2807-v3.json),
[context](../../verification/contexts/outbox-fsync-boundaries.json),
[result](../../verification/results/outbox-fsync-boundaries-2807.json) and
[raw capture manifest](../../verification/traces/outbox-fsync-2807/manifest.json)
separate reusable intent, executed evidence and remaining unknowns. The extractor
`verification/traces/extract-outbox-fsync-captures.py` verifies original source pins,
reconciles histories/XML and copies the exact raw objects. Captures are synthetic
fixture data. Local `/tmp` runs are session-local; committed publication becomes
cross-host replicated only when the canonical source PR lands.

The marker-free v2 source candidate had all four CI jobs green. A late review
correctly found that parent role tags prove invocation, not that a child reached
the requested stall. Its stall-control clearance was withdrawn before merge.
V3 freezes the marker fix separately and reports the new 144-case campaign above.
The worker writes bounded, exclusive PID/role/fresh-nonce proof before stalling.
After actual kill/reap, the owner checks that proof. Four real-worker falsifiers
reject missing or mismatched markers. Six raw child-marker JSON objects are
retained alongside the 51 raw DB/WAL objects. Normal producer recovery and the
local command-request boundary remain unchanged.

Original v1/v2 freezes and prior captures remain historical. The v3 runtime's
Git HEAD identifies the preceding source candidate; the v3 freeze and imported
source hashes identify the exact helpers executed. Independent raw v3 replay passed: all 122 manifest/index objects, 30 WAL
checksum chains, 24 copied SQLite reads and the child-marker proof match.
Correction note #5837 records this epoch. All four amended-head CI jobs remain
publication gates. The
[publication provenance](../../verification/fixtures/outbox-fsync-publication-provenance-2807.json)
records exact commands, artifact hashes and all source-freeze epochs.
