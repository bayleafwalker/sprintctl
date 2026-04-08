# Sample Makefile fragment for repositories that commit sprint snapshots.

.PHONY: sprint-snapshot sprint-check

sprint-snapshot:
	mkdir -p docs/sprint-snapshots
	sprintctl render > docs/sprint-snapshots/sprint-current.txt

sprint-check:
	sprintctl maintain check
