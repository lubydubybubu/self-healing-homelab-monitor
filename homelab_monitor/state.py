"""State diffing: turn the current condition set into change-only events.

Pure functions only - the caller owns I/O. Rules (ported from the real system):

* First run establishes a baseline silently (no "everything is broken" flood).
* A new condition must be seen ``flap_threshold`` consecutive times before it is
  reported (anti-flap), unless the condition is marked ``immediate``.
* A reported condition that disappears produces exactly one "recovered" event.
* During quiet hours non-urgent events are held and flushed at the first run
  after quiet hours end. Urgent events are always delivered.
* ``conditions=None`` (unknown) leaves the state untouched.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
from dataclasses import asdict, dataclass
from typing import Any

from .checks import Condition
from .config import QuietHours

STATE_VERSION = 1


@dataclass(frozen=True)
class Event:
    kind: str  # "problem" | "recovered"
    host: str
    key: str
    text: str
    urgent: bool = False

    def render(self) -> str:
        icon = "⚠️" if self.kind == "problem" else "✅"
        verb = "" if self.kind == "problem" else "recovered: "
        return f"{icon} [{self.host}] {verb}{self.text}"


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "reported": {}, "seen": {}, "held": []}


def evaluate(
    host: str,
    prev: dict[str, Any] | None,
    conditions: dict[str, Condition] | None,
    now: dt.datetime,
    *,
    flap_threshold: int = 2,
    quiet_hours: QuietHours | None = None,
) -> tuple[dict[str, Any], list[Event]]:
    """Return ``(new_state, events_to_deliver_now)``."""
    first = not prev or "reported" not in prev
    st = empty_state() if first else json.loads(json.dumps(prev))  # deep copy
    st.setdefault("held", [])
    if conditions is None:
        st["last_unknown"] = now.isoformat(timespec="seconds")
        return st, []

    reported: dict[str, dict[str, Any]] = st["reported"]
    seen = {k: st["seen"].get(k, 0) + 1 for k in conditions}
    events: list[Event] = []

    if first:
        reported = {k: {"text": c.text, "urgent": c.urgent} for k, c in conditions.items()}
    else:
        for key in sorted(conditions):
            c = conditions[key]
            if key in reported:
                reported[key]["text"] = c.text  # keep latest wording, no new event
                continue
            if seen[key] >= flap_threshold or c.immediate:
                events.append(Event("problem", host, key, c.text, c.urgent))
                reported[key] = {"text": c.text, "urgent": c.urgent}
        for key in sorted(set(reported) - set(conditions)):
            old = reported.pop(key)
            events.append(Event("recovered", host, key, old["text"], False))

    quiet = bool(quiet_hours and quiet_hours.contains(now.hour))
    deliver: list[Event] = []
    held = [Event(**e) for e in st["held"]]
    if quiet:
        for e in events:
            (deliver if e.urgent else held).append(e)
    else:
        deliver = held + events
        held = []

    st.update(
        reported=reported,
        seen=seen,
        held=[asdict(e) for e in held],
        last=now.isoformat(timespec="seconds"),
    )
    return st, deliver


def load_state(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    """Atomic write (tmp file + rename) so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
    os.replace(tmp, path)
