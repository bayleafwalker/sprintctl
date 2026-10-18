# Private offline reader-event capture

`scripts/collect_reader_events.py` is private measurement preparation for
`sprintctl#2783` / `sprintctl#2661`. It consumes an existing byte stream; it never
launches a reader or changes a runtime. There are no model, credential,
configuration, network, owner, database or subprocess operations.

Run against an already retained stream, with a fresh destination:

```sh
python scripts/collect_reader_events.py \
  --output-dir /tmp/reader-capture-new \
  --interpretation codex-documentation-v1 < retained-events.jsonl
```

This does not qualify live event availability. The interpretation uses synthetic
examples and type names from [OpenAI non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode),
checked 2026-10-18. It is private evidence metadata, not a public event contract.
OpenCode remains `--interpretation opaque`: its protected adapter integration does
not establish a supported fresh CLI reader transport or equivalent event schema.

The fresh directory is private (`0700`), with exclusive `0600` files: exact
`raw.bin`, bounded chunk receive timestamps in `receive-records.jsonl`, and
`summary.json`. Existing destinations, including symlinks, are refused. Storage
failures return failure and may prevent the summary itself. Whatever partial
evidence exists remains for inspection; no crash durability guarantee is made.

Raw capture stops at 32 MiB without reading an extra byte. `observed-eof`,
`raw-cap-reached` and `capture-error` are distinct. Reaching exactly the cap means
the remainder is unknown; it establishes neither truncation nor EOF. Interpretation
allows at most 100,000 event lines, 1 MiB per line, JSON nesting 64 (checked before
recursive decoding), 4,096 tracked tool items, 64 KiB retained message text and
64 bounded diagnostics. At most 4,096 receive records are retained; omitted record
counts remain explicit. Tiny reads cannot expand metadata indefinitely. Raw bytes
continue to their independent bound when interpretation limits are reached.

The Codex interpretation counts observed command, MCP and web tool identities,
deduplicating lifecycle updates within each observed turn. It retains identities,
start/completion/failure facts and anomalies. Counts are not file-read counts or
complete-attempt totals. No observed turn means unknown attribution. Multiple
terminal events invalidate single-terminal usage; missing or malformed usage is
unavailable, never zero. Other event types remain observable unknowns.

Message text is retained separately from terminal-event presence; neither is a
graded answer or proof of process success. A zero exit status means bounded stream
capture/interpretation completed without detected errors, not that a reader
succeeded. Empty input, malformed JSONL, capture/interpretation limits and storage
failures return failure. Omitted receive metadata is explicitly reported even
when the raw stream is complete. `capture_complete` means observed stream EOF,
including when its contents are malformed; inspect diagnostics separately.

Chunk receive times measure the collector's observation interval. Replaying an
old stream measures replay observation, not its original reader latency. EOF is
not process exit. Active effort, complete-attempt metrics, startup-versus-reader
reads, process lifecycle and reader success stay unknown. Partial traces cannot
certify no extra reads. A raw hash checks retained bytes; it authenticates neither
the provider nor an owner record.

All tests use synthetic streams and temporary folders. Existing nine-reader trial
artifacts stay unchanged. `agentops#2654`, supported transports, eligible real
interrupted tasks, executed owner/presentation bindings and task-specific freezes
remain gates before qualification. This utility establishes no historical result
transfer, authority, source trust, model performance or recovery-effort savings.
