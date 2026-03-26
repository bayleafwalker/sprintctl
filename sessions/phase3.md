## Task: Knowledge promotion, policy profiles, API wrapper
## Spec reference: sprintctl_starter.md (agent reads as needed)

## This session builds:
- `sprintctl/knowledge.py` — KnowledgeCandidate table; promotion flow: candidate → approved → published; derive candidates from events and handoffs
- `sprintctl/profiles.py` — named policy profiles (e.g. `default`, `fast`, `review-heavy`); profile selection at sprint or track level; no per-field policy sprawl
- `sprintctl/api.py` — thin HTTP wrapper (FastAPI or Flask) exposing read endpoints: sprint state, item list, claim list, render; no write mutations via API in this phase
- additions to `sprintctl/cli.py` — `knowledge promote`, `knowledge list`, `profile set` subcommands
- `tests/test_knowledge.py`, `tests/test_api.py`

## Stop at:
- No UI, no webhook integrations
- API is read-only
- Published knowledge is written to `docs/knowledge/` as markdown files; no external publish targets

## Acceptance criteria:
- A KnowledgeCandidate derived from a system event can be promoted to `approved` and then `published`, producing a file under `docs/knowledge/`
- Switching a sprint to a non-default policy profile changes effective TTLs and heartbeat intervals returned by `policies.get()`
- `GET /sprint/{id}` returns current sprint state as JSON matching the DB record
- `pytest tests/` passes in full across all three phases with no skips
