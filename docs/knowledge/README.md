# Knowledge Outputs

This directory holds committed outputs rendered from `kctl`.

- Canonical committed render path: `docs/knowledge/knowledge-base.md`
- Render command: `kctl render --output docs/knowledge/knowledge-base.md`
- Approved coordination or workflow entries also render into `docs/knowledge/knowledge-base.md`; this repo does not use a separate committed `knowledge-base-ops.md`
- Keep ad hoc renders, draft exports, and scratch markdown out of the repo. `.gitignore` allows only the canonical output file in this directory.

The rendered knowledge base is a publish artifact derived from approved and published entries in the local `kctl` database. It should be updated intentionally as part of a knowledge-publishing task, not mixed into unrelated feature work.
