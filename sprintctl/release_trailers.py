"""Harvest ``Vuoro-Release:`` commit trailers into the producer outbox.

A commit that delivers a released work item carries the release digest as a
git trailer::

    Vuoro-Release: 3f0c...e9   (64 hex; an optional ``sha256:`` prefix is accepted)

At synchronization time the checkout that owns the ``.sprintctl`` state is
scanned from a per-ref cursor kept in the local outbox database.  Each valid
(commit, digest) pair becomes one ``release.commit-observed`` observation;
the server binds it to ``release_commit`` when the digest is a known release
of the repository (``pg.ingest_records``).

Only the *form* of a trailer is validated here (40-hex commit, 64-hex
digest).  Malformed trailers are counted and never enqueued.  Remote hints
never carry credentials: URL userinfo, query and fragment are stripped
before anything is written to the outbox, and a hint that still looks
credential-shaped is dropped.

The harvest is best effort and never blocks synchronization:
:func:`harvest_release_trailers` turns any failure (git missing, an
unreadable repository, a failed identity or capability lookup) into a
``skipped`` result with a reason.  A caller that talks to a server which may
predate this record type passes ``server_accepts``; when it says no, nothing
is enqueued and the cursor stays put, so the same commits are harvested once
the server accepts them.  An observation the server rejects must never enter
the outbox: every record shares one contiguous origin stream, so it could
be neither sent nor skipped.

Local (SQLite) mode has no harvest: it has no ingest ledger and no
synchronization pass (``sprintctl sync`` and ``authority sync`` refuse the
local backend), so there is nothing to observe the trailers into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Callable

from . import contracts, outbox


TRAILER_KEY = "Vuoro-Release"
EVENT_TYPE = "release.commit-observed"
# First harvest of a ref (or one whose cursor is no longer an ancestor of the
# ref, e.g. after a force-push) scans at most this many commits back from the
# ref tip.  Older delivering commits are out of scope for the harvest.
INITIAL_WINDOW = 500

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_PREFIX = "sha256:"
_QUERY_OR_FRAGMENT_RE = re.compile(r"[?#]")

_NOT_A_CHECKOUT = "not a git checkout"
_NO_COMMITS = "repository has no commits"
_QUIET_SKIPS = frozenset({None, _NOT_A_CHECKOUT, _NO_COMMITS})

_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS release_trailer_cursor (
    ref        TEXT PRIMARY KEY,
    commit_sha TEXT NOT NULL CHECK (length(commit_sha) = 40),
    updated_at TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class HarvestResult:
    """Evidence from one trailer harvest pass."""

    status: str  # "harvested" | "skipped"
    ref: str | None = None
    scanned_commits: int = 0
    enqueued: int = 0
    already_enqueued: int = 0
    malformed: int = 0
    malformed_trailers: tuple[dict[str, str], ...] = field(default_factory=tuple)
    cursor: str | None = None
    detail: str | None = None

    @property
    def noteworthy_skip(self) -> bool:
        """A skip the operator should hear about (not merely "no checkout")."""
        return self.status == "skipped" and self.detail not in _QUIET_SKIPS

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "ref": self.ref,
            "scanned_commits": self.scanned_commits,
            "enqueued": self.enqueued,
            "already_enqueued": self.already_enqueued,
            "malformed": self.malformed,
            "malformed_trailers": [dict(item) for item in self.malformed_trailers],
            "cursor": self.cursor,
            "detail": self.detail,
        }


def normalize_digest(value: str) -> str | None:
    """Return the canonical 64-hex digest, or ``None`` for a malformed value."""
    text = value.strip()
    if text[: len(_DIGEST_PREFIX)].lower() == _DIGEST_PREFIX:
        text = text[len(_DIGEST_PREFIX):]
    text = text.lower()
    return text if _DIGEST_RE.fullmatch(text) else None


def strip_url_credentials(url: str | None) -> str | None:
    """Return a remote URL without userinfo, query or fragment.

    Handles scheme URLs (``https://user:token@host/path``) and scp-like git
    remotes (``git@host:owner/repo``).  Local paths pass through unchanged.

    Userinfo is removed *before* cutting at ``?``/``#``: a password may
    itself contain those characters, and cutting first would keep its prefix.
    Everything up to the last ``@`` of the remote counts as userinfo.  A
    result that still matches :func:`contracts.credential_shape` is dropped
    (``None``): a hint is optional, a leaked credential is not.
    """
    if url is None:
        return None
    text = url.strip()
    if not text:
        return None
    if text.startswith(("/", ".", "~")):
        hint = text
    elif "://" in text:
        scheme, _sep, rest = text.partition("://")
        rest = _QUERY_OR_FRAGMENT_RE.split(rest.rpartition("@")[2], maxsplit=1)[0]
        hint = f"{scheme}://{rest}"
    else:
        hint = _QUERY_OR_FRAGMENT_RE.split(text.rpartition("@")[2], maxsplit=1)[0]
    if not hint or contracts.credential_shape(hint) is not None:
        return None
    return hint


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _ensure_cursor_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CURSOR_SCHEMA)
    conn.commit()


def get_cursor(conn: sqlite3.Connection, ref: str) -> str | None:
    _ensure_cursor_table(conn)
    row = conn.execute(
        "SELECT commit_sha FROM release_trailer_cursor WHERE ref = ?", (ref,)
    ).fetchone()
    return str(row[0]) if row is not None else None


def _set_cursor(conn: sqlite3.Connection, ref: str, commit_sha: str) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn.execute(
        """
        INSERT INTO release_trailer_cursor (ref, commit_sha, updated_at) VALUES (?, ?, ?)
        ON CONFLICT (ref) DO UPDATE SET commit_sha = excluded.commit_sha,
                                        updated_at = excluded.updated_at
        """,
        (ref, commit_sha, now),
    )
    conn.commit()


def _already_enqueued(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for row in conn.execute(
        "SELECT payload FROM outbox_record WHERE event_type = ?", (EVENT_TYPE,)
    ):
        payload = json.loads(row[0])
        pairs.add((str(payload.get("release_digest")), str(payload.get("commit_sha"))))
    return pairs


def _scan(repo_root: Path, revision_args: list[str]) -> list[tuple[str, list[str]]] | None:
    fmt = f"%H%x00%(trailers:key={TRAILER_KEY},valueonly,separator=%x00)%x1e"
    # ``--no-show-signature``: a user's ``log.showSignature=true`` would
    # otherwise prefix each record with gpg output and break ``%H`` parsing.
    proc = _git(
        repo_root, "log", "--no-show-signature", f"--format={fmt}", *revision_args, "--"
    )
    if proc.returncode != 0:
        return None
    commits: list[tuple[str, list[str]]] = []
    for chunk in proc.stdout.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        sha, _sep, rest = chunk.partition("\x00")
        values = [value for value in rest.split("\x00") if value.strip()] if rest else []
        commits.append((sha.strip(), values))
    return commits


def _shown(value: str) -> str:
    """A malformed trailer value safe to report (never echo a credential)."""
    text = value.strip()
    if contracts.credential_shape(text) is not None:
        return "<redacted credential-shaped value>"
    return text[:200]


def harvest_release_trailers(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    actor: str | Callable[[], str],
    window: int = INITIAL_WINDOW,
    server_accepts: Callable[[], bool] | None = None,
) -> HarvestResult:
    """Enqueue one observation per new (commit, digest) trailer on the HEAD ref.

    ``actor`` may be a callable so a served caller resolves the authenticated
    identity only when there is something to enqueue; ``server_accepts`` is
    consulted the same way, before ``actor``.  A checkout that is not a git
    repository (or has no commits) is skipped, and so is any failure: this
    never raises, so a broken harvest cannot abort the synchronization that
    runs it.
    """
    try:
        return _harvest(
            conn, repo_root, actor=actor, window=window, server_accepts=server_accepts
        )
    except Exception as exc:  # noqa: BLE001 - best effort; sync must go on
        return HarvestResult("skipped", detail=f"harvest failed: {_failure(exc)}")


def _failure(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    if contracts.credential_shape(text) is not None:
        return f"{type(exc).__name__}: <redacted credential-shaped detail>"
    return text[:300]


def _harvest(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    actor: str | Callable[[], str],
    window: int,
    server_accepts: Callable[[], bool] | None,
) -> HarvestResult:
    # Only the checkout that owns this state: never scan an enclosing repo.
    if not (repo_root / ".git").exists():
        return HarvestResult("skipped", detail=_NOT_A_CHECKOUT)
    toplevel = _git(repo_root, "rev-parse", "--show-toplevel")
    if (
        toplevel.returncode != 0
        or Path(toplevel.stdout.strip()).resolve() != repo_root.resolve()
    ):
        return HarvestResult("skipped", detail=_NOT_A_CHECKOUT)
    head = _git(repo_root, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    if head.returncode != 0:
        return HarvestResult("skipped", detail=_NO_COMMITS)
    head_sha = head.stdout.strip()
    symbolic = _git(repo_root, "symbolic-ref", "-q", "HEAD")
    ref = symbolic.stdout.strip() if symbolic.returncode == 0 else "HEAD"

    cursor = get_cursor(conn, ref)
    if cursor == head_sha:
        return HarvestResult("harvested", ref=ref, cursor=head_sha)
    revision_args: list[str]
    if cursor is not None and _git(
        repo_root, "merge-base", "--is-ancestor", cursor, head_sha
    ).returncode == 0:
        revision_args = [f"{cursor}..{head_sha}"]
    else:
        revision_args = [f"--max-count={window}", head_sha]
    commits = _scan(repo_root, revision_args)
    if commits is None:
        return HarvestResult("skipped", ref=ref, cursor=cursor, detail="git log failed")

    remote = _git(repo_root, "remote", "get-url", "origin")
    remote_hint = strip_url_credentials(remote.stdout) if remote.returncode == 0 else None

    seen = _already_enqueued(conn)
    malformed: list[dict[str, str]] = []
    pending: list[dict[str, str | None]] = []
    already = 0
    # git log lists newest first; enqueue oldest first.
    for sha, values in reversed(commits):
        if not _SHA_RE.fullmatch(sha):
            malformed.extend({"commit_sha": sha, "value": _shown(v)} for v in values)
            continue
        for value in values:
            digest = normalize_digest(value)
            if digest is None:
                malformed.append({"commit_sha": sha, "value": _shown(value)})
                continue
            if (digest, sha) in seen:
                already += 1
                continue
            seen.add((digest, sha))
            pending.append(
                {
                    "release_digest": digest,
                    "commit_sha": sha,
                    "ref": ref,
                    "remote_hint": remote_hint,
                }
            )

    if pending and server_accepts is not None and not server_accepts():
        # Cursor deliberately not advanced: harvested once the server rolls.
        return HarvestResult(
            "skipped",
            ref=ref,
            scanned_commits=len(commits),
            malformed=len(malformed),
            malformed_trailers=tuple(malformed),
            cursor=cursor,
            detail=(
                f"server does not accept {EVENT_TYPE} observations yet; "
                f"{len(pending)} trailer(s) left for a later sync"
            ),
        )
    if pending:
        resolved_actor = actor() if callable(actor) else actor
        for payload in pending:
            outbox.append_observation(
                conn,
                event_type=EVENT_TYPE,
                actor=resolved_actor,
                payload=payload,
            )
    _set_cursor(conn, ref, head_sha)
    return HarvestResult(
        "harvested",
        ref=ref,
        scanned_commits=len(commits),
        enqueued=len(pending),
        already_enqueued=already,
        malformed=len(malformed),
        malformed_trailers=tuple(malformed),
        cursor=head_sha,
    )


__all__ = [
    "EVENT_TYPE",
    "HarvestResult",
    "INITIAL_WINDOW",
    "TRAILER_KEY",
    "get_cursor",
    "harvest_release_trailers",
    "normalize_digest",
    "strip_url_credentials",
]
