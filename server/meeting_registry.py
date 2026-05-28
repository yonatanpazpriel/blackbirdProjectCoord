"""Local meeting registry mapping Google Meet URLs to scheduling context.

When ``schedule_calendar_call`` runs, it persists a row here keyed by the
generated Meet URL so charlie-meet can later call ``get_meeting_context``
and recover the full Linear ticket dump without having to ask the attendee
verbally.

Why a flat JSON file: zero deps, easy to inspect, single-process. The Flask
webhook is the only writer, so contention is negligible. We still do an
``os.replace`` from a tempfile to keep the on-disk file consistent if the
process is killed mid-write.

Keying: the canonical key is the Google Meet ``meet_link`` (e.g.
``https://meet.google.com/abc-defg-hij``). It's stable, unique per event,
and visible to anyone in the meeting — including the Tavus bot user that
auto-joins via the conferencing alias. Callers that don't know the meet
link (e.g. charlie-meet at the very start of the call) can use
``claim_most_recent_unclaimed`` to pull the newest scheduled-but-untouched
record within a freshness window.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_REGISTRY_PATH = _REPO_ROOT / "data" / "meetings.json"


def _registry_path() -> pathlib.Path:
    override = os.environ.get("MEETING_REGISTRY_PATH")
    if override:
        return pathlib.Path(override)
    return _DEFAULT_REGISTRY_PATH


def _load(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"meetings": []}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"meetings": []}
    if not isinstance(data, dict) or not isinstance(data.get("meetings"), list):
        return {"meetings": []}
    return data


def _save(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".meetings_", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_meeting(record: dict[str, Any]) -> dict[str, Any]:
    """Persist a meeting record. ``record`` must contain ``meet_link``.

    If a record with the same ``meet_link`` already exists, it is replaced
    (idempotent re-scheduling). ``recorded_at`` is set/updated to now.
    """
    meet_link = (record.get("meet_link") or "").strip()
    if not meet_link:
        raise ValueError("record.meet_link is required")

    path = _registry_path()
    data = _load(path)
    meetings = data["meetings"]

    stored = dict(record)
    stored["meet_link"] = meet_link
    stored.setdefault("claimed_at", None)
    stored["recorded_at"] = _now_iso()

    for idx, existing in enumerate(meetings):
        if existing.get("meet_link") == meet_link:
            meetings[idx] = stored
            _save(path, data)
            return stored

    meetings.append(stored)
    _save(path, data)
    return stored


def lookup_by_meet_link(meet_link: str) -> dict[str, Any] | None:
    """Exact-match lookup by Meet URL. Does not mutate ``claimed_at``."""
    if not meet_link:
        return None
    path = _registry_path()
    data = _load(path)
    for entry in data["meetings"]:
        if entry.get("meet_link") == meet_link:
            return entry
    return None


def claim_most_recent_unclaimed(
    *, max_age_hours: float = 6.0
) -> dict[str, Any] | None:
    """Find the newest meeting that has never been claimed and mark it claimed.

    Used by charlie-meet when it joins a meet and has no meet_link to key
    off of. Returns ``None`` if no unclaimed meeting was recorded within
    ``max_age_hours``.

    Concurrency: single-process Flask. We re-read, mutate, and re-write.
    Good enough for the demo; revisit if multiple processes touch this.
    """
    path = _registry_path()
    data = _load(path)
    meetings = data["meetings"]
    if not meetings:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    def _recorded_at(entry: dict[str, Any]) -> datetime:
        try:
            return datetime.fromisoformat(str(entry.get("recorded_at") or ""))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    candidates = [
        entry
        for entry in meetings
        if not entry.get("claimed_at") and _recorded_at(entry) >= cutoff
    ]
    if not candidates:
        return None

    chosen = max(candidates, key=_recorded_at)
    chosen["claimed_at"] = _now_iso()
    _save(path, data)
    return chosen
