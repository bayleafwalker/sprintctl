"""Backend-neutral row normalisation and serializers for sprintctl.

Both storage backends (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) return
the same public value contract: plain dicts with ISO-8601 string timestamps.
This module is the single home for the helpers that produce that contract, so
the two backends cannot drift apart in serialization behaviour.

Nothing in this module opens a connection or executes SQL; it only transforms
rows already fetched by a backend.
"""

from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone
from typing import Any
from uuid import UUID


def iso_timestamp(value: Any) -> str | None:
    """Return an ISO-8601 string for a timestamp-like value, or None.

    Fractional seconds are preserved: producer ledger hashes cover these
    timestamps, so truncating them on a read turns an otherwise identical
    served record into a different record when a client audits it.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def normalize_row(row: dict) -> dict:
    """Normalise a backend row to the SQLite-compatible public value contract.

    PostgreSQL (psycopg) returns ``datetime``/``date``/``UUID`` objects;
    SQLite returns strings.  Normalising here lets every downstream consumer
    treat both backends identically.  For SQLite rows this is a no-op.
    """
    out: dict = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = iso_timestamp(value)
        elif isinstance(value, _dt.date):
            out[key] = value.isoformat()
        elif isinstance(value, UUID):
            out[key] = str(value)
        else:
            out[key] = value
    return out
