"""
cleanroom/audit.py — Shared audit-log write/read layer for the Phase 4 UI backend.

audit.jsonl has two independent producers:

  - daemon.py (Phase 3, UNTOUCHED by this module). One line per live news
    article, hardened-only. Its lines never carry source/mode/trace_id —
    those fields didn't exist when daemon.py was written, and daemon.py is
    not being modified to add them.

  - This module (Phase 4). One line per side of a judge-submitted "Try to
    hack the agent" comparison: source="judge", mode="hardened"|
    "unhardened", and a shared trace_id so the UI can pair the two lines
    back into one split-screen result.

Both producers append to the SAME file, in daemon.py's existing line
format (one JSON object per line, `default=str` for datetimes). This
module never rewrites daemon's existing lines — normalize_event() fills in
defaults for whatever fields are missing, applied only on read.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT_LOG_DEFAULT = Path("audit.jsonl")

# Applied on read only. New (Phase 4) lines already carry these fields
# explicitly, so this is a no-op for them; old (Phase 3 daemon) lines get
# them backfilled here instead of being rewritten on disk.
_READ_DEFAULTS: dict[str, Any] = {
    "source": "daemon",
    "mode": "hardened",
    "trace_id": None,
}


def new_trace_id() -> str:
    return uuid.uuid4().hex


def normalize_event(raw: dict) -> dict:
    """Backward-compat defaults for a parsed JSON line. Safe on both old
    (Phase 3 daemon) and new (Phase 4 judge) lines."""
    event = dict(raw)
    for key, default in _READ_DEFAULTS.items():
        event.setdefault(key, default)
    return event


def append_event(event: dict, audit_path: Path = AUDIT_LOG_DEFAULT) -> None:
    """Appends one complete event dict as a JSON line. Byte-for-byte the
    same serialization daemon.py's _append_audit() uses (default=str so
    datetimes serialize without a custom encoder), so lines from both
    producers are format-identical on disk."""
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def record_judge_attack(
    hardened_fields: dict[str, Any],
    unhardened_fields: dict[str, Any],
    trace_id: str | None = None,
    audit_path: Path = AUDIT_LOG_DEFAULT,
) -> str:
    """Writes both arms of one judge-submitted split-screen comparison as
    two JSONL lines sharing a single trace_id. This function owns the
    shared envelope (timestamp, source, mode, trace_id) so the caller only
    supplies pipeline-specific fields (symbols_extracted, outcome, reason,
    ...) and the two arms can never end up mismatched or tagged with
    different ids. If trace_id is not supplied, one is generated — pass it
    explicitly when the caller (server.py) already streamed the same id to
    the client over SSE, so the persisted line and the live stage events
    correlate exactly."""
    trace_id = trace_id or new_trace_id()
    now = datetime.now(timezone.utc).isoformat()

    hardened_event = {
        "timestamp": now,
        "source": "judge",
        "mode": "hardened",
        "trace_id": trace_id,
        **hardened_fields,
    }
    unhardened_event = {
        "timestamp": now,
        "source": "judge",
        "mode": "unhardened",
        "trace_id": trace_id,
        **unhardened_fields,
    }

    append_event(hardened_event, audit_path)
    append_event(unhardened_event, audit_path)
    return trace_id


def read_all(audit_path: Path = AUDIT_LOG_DEFAULT) -> list[dict]:
    """Reads and normalizes every line currently in the file — used to
    backfill an SSE client with history on connect. A line that fails to
    parse as JSON (e.g. a torn write caught mid-append) is skipped, not
    raised, so one bad line never breaks the whole read."""
    if not audit_path.exists():
        return []
    events = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(normalize_event(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return events


def raw_line_count(audit_path: Path = AUDIT_LOG_DEFAULT) -> int:
    """Total raw lines in the file, including any blank/malformed ones.
    This — not the count of successfully-parsed events — is the correct
    offset unit for tail_new(), so a malformed line can never desync the
    backfill/tail boundary and cause a duplicated or dropped event."""
    if not audit_path.exists():
        return 0
    return len(audit_path.read_text(encoding="utf-8").splitlines())


def tail_new(audit_path: Path, from_line_count: int) -> tuple[list[dict], int]:
    """Reads events starting at raw line index from_line_count, returns
    (new_events, new_total_raw_line_count). Re-reads the whole file each
    call — cheap for a hackathon-scale audit.jsonl (hundreds of lines),
    not built for a multi-GB log."""
    if not audit_path.exists():
        return [], from_line_count
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    new_events = []
    for line in lines[from_line_count:]:
        line = line.strip()
        if not line:
            continue
        try:
            new_events.append(normalize_event(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return new_events, len(lines)