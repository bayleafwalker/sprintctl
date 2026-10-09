# Explicit Release contracts through the served reservation operation

`work.reservation.reserve` accepts an optional `acceptance_contract` object.
This forwards the existing owner contract to the execution reservation's
transactional Release freeze. The operation retains its `work:write` authority;
it grants no effect acceptance or application authority.

An explicit contract requires role `execution` (also the omitted-role default)
and `expected_revision` naming the item's observed edit or Release revision.
An invalid contract, a nonexecution role, a missing basis or a stale basis
refuses without creating a reservation or Release. Omitted contracts retain the
existing default behavior. Owner validation and canonical digest semantics are
unchanged; no schema migration is needed.

For selected protected proof work, set:

```json
{"review_required": true, "effect_verification_required": true}
```

Preserve the returned reservation and Release digest before proposing an
effect. Acceptance then requires the exact protected verifier receipt under
the existing owner rules. This is a work Release requirement, not permission
to accept or apply an effect. Legacy CLI invocations remain unchanged; a
schema-driven native client may supply the optional field directly.
