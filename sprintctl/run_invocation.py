"""Pure utilities for the hosted run invocation receipt contract."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

OWNER_RECORD_DOMAIN = b"sprintctl-run-owner-record/v1\x00"
JCS_SAFE_INTEGER = 9007199254740991
INVOCATION_ID_RE = re.compile(r"^inv_[0-9A-HJKMNP-TV-Z]{26}$")
ATTEMPT_ID_RE = re.compile(r"^att_[0-9A-HJKMNP-TV-Z]{26}$")


def _valid_unicode(value: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("JCS strings must contain valid Unicode scalar values") from exc


def _jcs_string(value: str) -> str:
    _valid_unicode(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if not -JCS_SAFE_INTEGER <= value <= JCS_SAFE_INTEGER:
            raise ValueError("JCS integer is outside the interoperable safe range")
        return str(value)
    if isinstance(value, float):
        raise ValueError("JCS projection does not permit floating-point values")  # noqa: TRY004
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise TypeError("JCS object property names must be strings")
            _valid_unicode(key)
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(
            _jcs_string(key) + ":" + _jcs(value[key]) for key in keys
        ) + "}"
    raise ValueError(f"unsupported JCS value type: {type(value).__name__}")


def jcs_bytes(value: Any) -> bytes:
    """Serialize the contract's RFC 8785 profile to UTF-8 bytes.

    The owner projection uses no floats. Refusing all floating-point values
    avoids runtime-dependent spelling and is stricter than the full JCS number
    domain by design; integers must stay inside the interoperable safe range.
    """
    return _jcs(value).encode("utf-8", errors="strict")


def owner_record_digest(projection: dict[str, Any]) -> str:
    """Return the Cloud-compatible raw lowercase SHA-256 for an owner record."""
    return hashlib.sha256(OWNER_RECORD_DOMAIN + jcs_bytes(projection)).hexdigest()


def owner_timestamp(value: Any) -> str:
    """Normalize a PostgreSQL timestamptz to the frozen six-digit UTC form."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise TypeError("run creation timestamp is not a timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
