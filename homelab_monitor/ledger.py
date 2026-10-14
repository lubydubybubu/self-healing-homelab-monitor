"""At-most-once remediation ledger.

The agent must ``claim`` an issue before acting on it. A claim succeeds only if
the same issue (``host:key``) has not been acted on within ``window_s``
(default 24h). A second claim fails -> the runbook says: stop and ASK FIRST.
This turns "never retry a fix automatically" from a prompt instruction into a
mechanical guarantee the agent cannot talk itself out of.
"""

from __future__ import annotations

import json
import pathlib
import time

from .events import EventLog
from .state import save_state

DEFAULT_WINDOW_S = 24 * 3600


def split_issue(issue_id: str) -> tuple[str, str | None]:
    host, sep, key = issue_id.partition(":")
    return host, (key if sep else None)


class Ledger:
    """Optional ``event_log`` records every claim as a ``remediation`` event
    (``result`` = ``claimed`` or ``refused``) for the metrics command."""

    def __init__(self, path: pathlib.Path, window_s: int = DEFAULT_WINDOW_S, clock=time.time, event_log: EventLog | None = None):
        self.path = path
        self.window_s = window_s
        self.clock = clock
        self.event_log = event_log

    def _record(self, issue_id: str, action: str, result: str) -> None:
        if self.event_log is not None:
            host, key = split_issue(issue_id)
            self.event_log.append("remediation", host=host, key=key, action=action, result=result, source="claim")

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def claim(self, issue_id: str, action: str) -> bool:
        data = self._load()
        now = self.clock()
        data = {k: v for k, v in data.items() if now - v["at"] < self.window_s}  # prune expired
        if issue_id in data:
            save_state(self.path, data)
            self._record(issue_id, action, "refused")
            return False
        data[issue_id] = {"at": now, "action": action}
        save_state(self.path, data)
        self._record(issue_id, action, "claimed")
        return True

    def history(self) -> dict:
        return self._load()
