"""Append-only event log (``<state_dir>/events.jsonl``) used for metrics.

One JSON object per line. Event kinds:

* ``issue_opened``   - an issue entered the reported set (``seed: true`` on the
  silent first-run baseline, whose real start time is unknown)
* ``issue_resolved`` - a reported issue disappeared
* ``remediation``    - an agent claimed / finished a fix. ``result`` is one of
  ``claimed`` / ``refused`` (from ``claim``) or ``fixed`` / ``failed`` /
  ``escalated`` (from ``report --result``)

Writes use ``O_APPEND`` + ``fsync`` with mode 0600. Logging is best effort:
:meth:`EventLog.append` never raises, because losing a metrics line must never
break monitoring or remediation.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
from collections.abc import Callable
from typing import Any

EVENTS_FILE = "events.jsonl"
REPORT_RESULTS = ("fixed", "failed", "escalated")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class EventLog:
    def __init__(self, path: pathlib.Path, clock: Callable[[], dt.datetime] = _utcnow):
        self.path = pathlib.Path(path)
        self.clock = clock

    @classmethod
    def in_state_dir(cls, state_dir: pathlib.Path, **kw) -> EventLog:
        return cls(pathlib.Path(state_dir) / EVENTS_FILE, **kw)

    def append(self, event: str, *, ts: dt.datetime | None = None, **fields: Any) -> bool:
        """Append one event. Returns False (and warns on stderr) instead of raising."""
        try:
            rec = {"ts": (ts or self.clock()).isoformat(timespec="seconds"), "event": event}
            rec.update({k: v for k, v in fields.items() if v is not None})
            line = (json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        except Exception as e:  # noqa: BLE001 - logging must never break the caller
            print(f"warning: event log write failed ({type(e).__name__}): {e}", file=sys.stderr)
            return False


def parse_ts(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def load_events(path: pathlib.Path) -> tuple[list[dict[str, Any]], int]:
    """Return ``(events sorted by time, number of unparseable lines)``. Missing file = empty."""
    events: list[dict[str, Any]] = []
    bad = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    e["_t"] = parse_ts(e["ts"])
                    events.append(e)
                except (ValueError, KeyError, TypeError, AttributeError):
                    bad += 1
    except FileNotFoundError:
        pass
    events.sort(key=lambda e: e["_t"])
    return events, bad


def issue_transitions(prev: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    """Diff the ``reported`` sets of two host states into opened/resolved records.

    Pure: the caller decides where to write them. Driven by the reported set (not
    by notifications), so anti-flap applies and quiet-hours holding does not delay
    the measured detection time.
    """
    first = not prev or "reported" not in prev
    before = {} if first else prev["reported"]
    after = new.get("reported") or {}
    out: list[dict[str, Any]] = []
    for key in sorted(set(after) - set(before)):
        v = after[key]
        rec = {"event": "issue_opened", "key": key, "text": str(v.get("text", ""))[:200], "urgent": bool(v.get("urgent"))}
        if first:
            rec["seed"] = True
        out.append(rec)
    for key in sorted(set(before) - set(after)):
        out.append({"event": "issue_resolved", "key": key, "text": str(before[key].get("text", ""))[:200]})
    return out
