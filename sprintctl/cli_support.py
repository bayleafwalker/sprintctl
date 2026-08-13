"""Small dependency-free helpers shared by the CLI and extracted commands."""

from __future__ import annotations

import re


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
