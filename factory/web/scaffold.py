"""Shared web plumbing for every page the factory serves.

One page scaffold (dark, static assets, no frameworks), the escaping and
formatting helpers every renderer uses, read-only trace access, and the
phase-name → plain-English map that keeps jargon off the screen. Rendering
modules (``kanban``, ``runs``) build bodies; ``board.py`` and ``runs.py``
own the HTTP plumbing that wraps them.
"""

import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REFRESH_S = 5  # <noscript> fallback refresh; JS polling (app.js) replaces it when enabled
TEXT_CAP = 240  # payload display cap; full text stays one click away
ARCHIVE_SEARCH_HINT = "filter archived tasks"

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
_STATIC_TYPES = {
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


def static_asset(name: str) -> tuple[bytes, str] | None:
    """(bytes, content-type) for a whitelisted asset, else None.

    The whitelist doubles as the traversal guard: ``..`` and subpaths never
    match a bare filename, so nothing outside ``static/`` can be reached.
    """
    if name not in _STATIC_TYPES:
        return None
    path = STATIC_DIR / name
    if not path.is_file():
        return None
    return path.read_bytes(), _STATIC_TYPES[name]


# --- read-only trace access --------------------------------------------------


def ui_connect(db_path: Path) -> sqlite3.Connection:
    """Open a trace db for reading only.

    ``query_only`` makes SQLite reject any write statement on this connection —
    the UI cannot corrupt the trace even if a rendering bug tried to. Note
    ``sqlite3.connect`` would create a missing file; the request handler checks
    existence before calling this, and no current caller bypasses that check.
    """
    conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


# --- formatting helpers ------------------------------------------------------


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def badge(status: str) -> str:
    return f'<span class="badge {_esc_badge(status)}">{esc(status)}</span>'


def _esc_badge(status: str) -> str:
    return esc(status).replace("'", "&#x27;").replace('"', "&quot;")


def short(text: str, cap: int = 96) -> str:
    """Truncate honestly: the marker says how much was left out."""
    if len(text) <= cap:
        return text
    return f"{text[:cap]} … [+{len(text) - cap} chars omitted]"


def pretty_payload(payload: str) -> str:
    """Pretty-print a JSON payload, escaping everything; long ones collapse."""
    try:
        text = json.dumps(json.loads(payload), ensure_ascii=False, indent=2, sort_keys=True)
    except ValueError:
        text = payload
    if len(text) <= TEXT_CAP:
        return f"<pre>{esc(text)}</pre>"
    return (
        f"<pre>{esc(short(text, TEXT_CAP))}</pre>"
        f"<details><summary class='muted'>show full</summary>"
        f"<pre>{esc(text)}</pre></details>"
    )


def duration_seconds(started_at, ended_at) -> float | None:
    """Seconds between two ISO timestamps; None when either is missing/invalid."""
    try:
        t0, t1 = datetime.fromisoformat(started_at), datetime.fromisoformat(ended_at)
        return (t1 - t0).total_seconds()
    except (TypeError, ValueError):
        return None


def fmt_duration(started_at, ended_at) -> str:
    """Human duration between two ISO timestamps; '' when either is missing."""
    secs = duration_seconds(started_at, ended_at)
    return f"{secs:.0f}s" if secs is not None else ""


def fmt_elapsed(secs: int) -> str:
    """mm:ss (h:mm:ss past the hour) — same format the JS ticker prints."""
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def started_epoch(started_at: str) -> int:
    """Unix epoch of an ISO timestamp, for the client-side elapsed ticker."""
    try:
        dt = datetime.fromisoformat(started_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # legacy rows wrote naive UTC
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return 0


# --- phase names → plain English ---------------------------------------------

_PHASE_LABELS = {
    "plan": "planning",
    "plan_gates": "checking the plan",
    "handoff": "briefing the builder",
    "commit:plan": "committing the plan",
    "checkpoint": "waiting on you",
    "commit": "committing",
    "deliver": "opening the PR",
}


def phase_label(name: str) -> str:
    """A phase row's plain-English verb; falls back to the raw name.

    Round-suffixed names carry their meaning in the prefix (``build:r2``,
    ``review:scorer:r1``, ``fix:r3``) — the prefix maps, the suffix stays
    in the raw name shown next to the label.
    """
    if name in _PHASE_LABELS:
        return _PHASE_LABELS[name]
    base = name.split(":", 1)[0]
    if base == "review":
        parts = name.split(":")
        return f"reviewing ({parts[1]})" if len(parts) > 2 else "reviewing"
    return {
        "build": "building",
        "test": "testing",
        "score": "scoring findings",
        "gate": "review gate",
        "fix": "fixing findings",
    }.get(base, name)


# --- page scaffold -----------------------------------------------------------


def page(title: str, body: str, live: bool = False) -> str:
    """The one scaffold every page wears: dark theme, shared header, real assets.

    ``live`` adds a <noscript> meta refresh fallback; with JavaScript the
    ``[data-live]`` region on the page polls its ``data-fragment-src`` instead,
    so no full reload ever happens.
    """
    noscript = f'<noscript><meta http-equiv="refresh" content="{REFRESH_S}"></noscript>' if live else ""
    return (
        f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">{noscript}\n'
        f"<title>{esc(title)}</title>\n"
        f'<link rel="stylesheet" href="/static/style.css">\n'
        f'<script src="/static/app.js" defer></script></head><body>\n'
        f"<header><h1>software factory</h1>"
        f"<nav><a href='/'>board</a> · <a href='/runs'>runs</a></nav></header>\n"
        f"{body}\n"
        f"<footer class='muted'>loopback only · one run per repo · live regions poll every 4s — "
        f"no full reloads</footer>\n"
        f"</body></html>\n"
    )
