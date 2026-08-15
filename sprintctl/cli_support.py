"""Small dependency-free helpers shared by the CLI and extracted commands."""

from __future__ import annotations

import re
from typing import Any

from . import reservation as _reservation


def _redacted_postgres_error(exc: Exception, url: str | None) -> str:
    """Return a connection error without exposing PostgreSQL credentials."""
    message = str(exc)
    if url:
        message = message.replace(url, "<redacted SPRINTCTL_URL>")
    return re.sub(
        r"(postgres(?:ql)?://)[^\s@]+@",
        r"\1<redacted>@",
        message,
        flags=re.IGNORECASE,
    )


def note_reservation_activity(store: Any, backend: Any, item_id: int | None) -> None:
    """Advance the caller's own reservation clocks after a successful mutation.

    Activity is derived from work, not from ceremony: a session that edits,
    annotates, or re-links an item it reserved has demonstrably not gone away.
    Only the reserving session matches (never a bare actor name), reads never
    call this, and a failure here must never fail the mutation that already
    committed -- the clock is advisory.

    This is the direct-backend half.  Served callers cannot use it (the store
    is remote), so they attach ``session_id`` to the invocation instead and the
    authority does the same bookkeeping server-side.
    """
    session_id = _reservation.ambient_session_id()
    note = getattr(backend, "note_session_activity", None)
    if not session_id or note is None or item_id is None:
        return
    try:
        note(store, int(item_id), session_id=session_id)
    except Exception:  # pragma: no cover - advisory bookkeeping only
        pass
