# Inspect a result read packet offline

A read packet lets another reader inspect supplied work facts without source
credentials or the old conversation. It preserves the existing owner responses;
it does not authenticate them, import owner state or prove a complete result.

The local commands require the optional `inspection` extra. A source checkout
can use `uv sync --frozen --extra inspection` (the development extra also includes
the validator). The core CLI gives an installation hint when it is absent.

First save an existing acceptance-snapshot response through the bounded
[effect explanation](explain-effects.md) JSON command. Optionally save a
same-repository/item continuation response through [run brief](resume-work.md).
Then package the supplied files:

```sh
sprintctl result packet create --acceptance acceptance.json --continuation continuation.json --output result.json
```

Creation prints the canonical packet digest. Store or deliver that expectation
through an independent channel when integrity comparison is needed. A digest
copied from inside an untrusted packet supplies no independent expectation.
Optional `--source-ref` adds a caller-declared reference, not an authenticated
revision. A packaging timestamp is from the caller clock, not a source observation.

```sh
sprintctl result packet inspect result.json
sprintctl result packet inspect result.json --expected-sha256 sha256:YOUR_64_LOWERCASE_HEX_DIGITS --format json
```

Inspection distinguishes self-consistency, the caller's expected digest,
unverified source authenticity, different/unknown Release records, missing
receipts and incomplete owner collections. The expectation's independence is
itself unverified by the tool. A matching protected receipt cannot hide a
mismatched requested artifact. Cross-response atomicity and currentness are
not established. The snapshot identities are not seals of their whole responses.

The packet digest covers canonical JSON content, including complete embedded
responses and metadata, excluding only its own digest field. Formatting and
object-member order are outside that digest domain. Owner Release, artifact,
receipt and snapshot digests retain their own distinct meanings.

A valid conflicting read is inspectable and can exit zero. A packet self-digest
or expected-digest mismatch exits one after displaying the report. Zero is not
an acceptance verdict. Malformed versions/schemas, duplicate JSON keys,
nonfinite numbers, invalid Unicode, excessive nesting and files over2MiB are
refused. Known credential-shaped content is refused rather than silently
redacted while retaining a misleading hash.

Creation writes a private temporary file and publishes complete bytes without
overwriting an existing output. It never reads a file named inside a reference,
executes metadata, opens a database or calls an owner/network endpoint. JSON
inspection retains the complete packet and original owner responses. Text bounds
and quotes caller metadata and retains the existing bounded explanation/briefing.

Terminal Decision binding, actors, attempt history and target acknowledgment or
observation remain missing where the supplied read does not contain them. This
is not the separately guarded archival transfer fixture, a production export
endpoint, an owner import path, a resume capability or external artifact custody.
The exact contract and qualification gates are in the
[packet design](../plans/result-read-packet.md).
