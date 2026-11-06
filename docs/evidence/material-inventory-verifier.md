# Material inventory verifier

Status: offline comparison tool only. This code does not enable material-v2,
admit database writers, provide an external restore epoch, fence old databases,
or reconcile an outbox after restore.

The versioned manifest records a selected PostgreSQL catalog snapshot and
manually classified Python SQL candidates for the eight relations named in
`sprintctl.material_inventory.COVERED`. It includes relation and column shape,
defaults, indexes, inheritance, constraints, trigger definitions and enabled
modes, trigger function definitions and privileges, role memberships, and
effective table, column, schema and function privileges for explicitly named
roles. A canonical SHA256 detects accidental manifest edits. Verification also
requires the exact source commit and source-file set from the manifest and
hashes the complete bytes of each source file. An SQL edit on the same line is
refused even when the discovered table and candidate line are unchanged. The
CLI rejects modified and nonignored untracked files in the checkout. Git ignored
files are outside that check and must not hold writer source used for a reviewed
manifest.

The source scanner is a conservative discovery aid, not a SQL parser. It finds
some literal SQL target forms and dynamic `execute` calls that need explicit
classification. It cannot discover indirect calls, Python wrappers that hide
SQL, stored procedures that are not trigger functions, migrations, privileged
maintenance entry points or external clients. The eight covered relations and
the discovered source paths therefore do not establish complete writer
coverage.

To use a reviewed manifest, the operator must first capture a candidate from a
fresh disposable PostgreSQL 16 database initialized from the exact source
commit, classify every source candidate and attach the resulting manifest to
independent review. Never treat capture from a live database as an approved
baseline or capture and verify in the same step. The verifier runs its catalog
queries in a read-only transaction and refuses a non-PostgreSQL-16 server,
source drift, malformed manifests, inventory drift or missing covered tables.

Current tests compare the initialized disposable catalog with a captured
in-transaction baseline, then deliberately add a column, remove a trigger and
change a grant. Each mutation must be detected and rolled back. Unit tests also
exercise source-candidate drift, explicit classification, duplicate JSON keys,
canonical digests and stale or tampered manifests. These controls qualify the
comparison mechanics; they do not independently qualify a checked-in catalog
manifest or prove that every database writer is represented.

The existing material-v2 audit still applies: writers must enter the global
gate before any SQL, runtime roles must not bypass admission, and an external
epoch authority must fence restored or forked databases through commit. None of
those controls is implemented here.
