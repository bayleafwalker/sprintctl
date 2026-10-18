# Resume and effect readers: bounded qualification

The six cases in `tests/pg/test_resume_result_readers.py` exercise native
PostgreSQL owner records, serialize the read response, and render it in a new
Python process. That process receives only the response on stdin, runs outside
the checkout, and has no database URL, predecessor conversation or credentials
in its supplied environment.

| Population | Cases | Expected distinctions |
| --- | --- | --- |
| Resume, n=3 | Replacement Release; new revise Decision; absent comparison baseline | Changed Release and Decision records stay identifiable; absent baseline and unchecked content stay unknown |
| Effect explanation, n=3 | Matching protected check; changed requested artifact digest; omitted receipt reference | Receipt matching cannot hide artifact mismatch; an accepted intent cannot fill an omitted receipt or prove a target effect |

The resume fixture records a predecessor and successor with different principal,
client and grant identities in one workspace/repository. The successor reads
through its own run. The effect fixtures use synthetic protected artifact-check
assertions recorded through the native evidence API. Those assertions are not
execution attestations or target observations.

Executed check:

```sh
timeout --foreground -k 30s 900s scripts/pg-disposable-tests.sh -k 'fresh_process_identifies_owner_changes or independent_reader_exposes_missing'
```

PostgreSQL 16.15: **6 passed**, with the fixture cleanup checker reporting zero
remaining rows. Both populations passed all three prescribed cases. The
versioned context/result packets live under `verification/contexts/` and
`verification/results/` as `resume-result-reader-proofs.json`.

The proof class is **example-tested**: actual owner records cross a fresh-process
serialization boundary and satisfy explicit assertions. No real provider swap,
fresh LLM/human trial, time saved, human recovery effort, deployed acceptance,
or general archival export/import qualification is established. Recovery duration
and review effort remain unmeasured, never zero. The software cases detect the
specified mismatches and missing inputs; they do not qualify the complete
request-to-target-to-terminal-acceptance chain.
