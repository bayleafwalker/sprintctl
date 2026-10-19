# Served invocation handshake — Sprintctl#2822

The authenticated served `_invoke_operation` now awaits `client.handshake()`
inside its existing fresh-client `async with client:` block before invoking the
operation. Handshake failures propagate unwrapped, so an incompatible protocol,
malformed handshake, HTTP error or wrong environment stops the call before any
catalog read or invocation. The client then closes as before.

Invocation arguments, session attribution, repository scope, idempotency
options, credential resolver selection (default, delegated and pinned
`_credential_resolver`), one fresh client and event loop per synchronous call,
and the lazy optional `vuoro_client` import are unchanged. Catalog-only helpers
do not handshake. The vuoro-client package, protocol and authentication design,
CLI, schemas and deployment are unchanged.

## Frozen oracle

The oracle `tests/test_served_invoke_handshake_oracle.py` was frozen before the
product edit, with SHA256
`f5085854d395d49e68a2a04a9c695437971fbee4b183e5732d450b132a9eed11`. It runs the
released vuoro-client 0.1.1 over an `httpx.MockTransport` that records the
request order. [The freeze record](../../verification/fixtures/served-invoke-handshake-freeze-2822.json)
pins the oracle, the unmodified `sprintctl/served.py`
(`f866ddba5d0ef350ee61197579f4af618ee028ce37183440f34432d729b47b64`), the
vuoro-client file hashes, the runtime versions and the expected outcome of each
test. The record's SHA256 is
`3c92c0f927c5dff70ffddc5cfe7d85eefa5dfe656a7dbd295f24364c0367af12`.

The record was taken at `4a59bd6afddb6f6a6fe86ab80a27fb4d28ea11c9`. The change
was built on `5ec2afbbe7b6f6ab304b7706c20365a812869cf3`, where `served.py` still
has the frozen hash.

| Run | Oracle | Full local suite (`not pg and not perf`) |
| --- | --- | --- |
| Before (base source) | 17 failed, 2 passed | 17 failed (the oracle only), 3,262 passed, 7 skipped |
| After | 19 passed | 3,279 passed, 7 skipped, 0 failed |

Before the change, every failing oracle test observed
`[GET /api/catalog/v1, POST /api/invoke/v1]` with no
`GET /api/meta/v1/handshake`. The two controls that pass without the fix are the
real-client handshake control and the fresh-client-per-call control. Every run
cleared inherited `SPRINTCTL_*` variables.

## Collateral test doubles

With the fix, 61 existing tests failed. All 61 failed because a test double had
no `handshake` method: 41 used `_FakeAsyncVuoroClient` in `tests/test_served.py`,
8 used local `Client` doubles directly, and 12 showed the same `AttributeError`
through CLI error output. No other cause was found. Each double now has a
minimal `handshake()`, and no invocation assertion was weakened:

- `tests/test_served.py` counts handshakes and asserts that the handshake comes
  before every invocation. Both fresh-client-per-call tests also assert one
  handshake per client.
- The credential-pinning doubles in the native evidence, proposal and reserve
  intake tests, plus the PostgreSQL-only reserve test, call the pinned resolver
  from `handshake()`, as the real authenticated handshake does. Their existing
  one-resolution and token assertions still pass, so the pinned snapshot also
  covers the handshake.
- CLI doubles in the item note, maintain check and next-work tests return a
  compatible empty result.

## Boundaries

This is local transport-replay and test-double evidence only. It is not
live-service evidence, and no served backend was contacted. The
PostgreSQL-marked suite was not run here. Its one edited double was checked only
by compilation. The change does not qualify #2127, the EC01 cohort, EC05 or
provider migration.

In a virtual environment without `vuoro_client`, `import sprintctl.served` and
`sprintctl --help` succeed, and importing `sprintctl.served` does not load
`vuoro_client`.
