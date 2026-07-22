---
doc_id: sprintctl.local-sqlite-initialization
status: draft
supersedes: null
---

# Local SQLite initialization protocol

This document owns sprintctl's primary local/recovery SQLite authority connection setup. It covers opening that database, enabling its connection invariants, and entering the existing versioned migration path.

## Connection contract

1. Open a connection and enable foreign-key enforcement before selecting WAL journal mode.
2. If WAL selection raises a primary or extended `SQLITE_BUSY` or `SQLITE_LOCKED` result, close that failed connection and retry. The sequence is bounded to five total attempts, with 10, 20, 40, and 80 millisecond delays between attempts.
3. Close the failed connection and propagate any other foreign-key or WAL initialization error immediately. There is no generic or unbounded retry.
4. After WAL succeeds, retain the existing `BEGIN IMMEDIATE` migration serialization. Connection retries do not change migration transactions, durability, or rollback behavior.

The current schema version is the number of migrations declared by the implementation in `sprintctl/db.py`; consumers and protocol documentation derive it from that source instead of permanently hardcoding a version here.

## Boundary

PostgreSQL connection and migration behavior is owned by the remote backend and is outside this protocol. The local outbox uses its own connection initializer and retry boundary; this document does not change or verify outbox behavior.
