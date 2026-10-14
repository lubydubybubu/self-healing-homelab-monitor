"""Pluggable notifiers. Secrets come from environment variables, never the config file."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Protocol, TextIO

from .state import Event


class Notifier(Protocol):
    def send(self, title: str, lines: list[str], *, mention: bool = False) -> None: ...


class StdoutNotifier:
    def __init__(self, stream: TextIO | None = None):
        self.stream = stream or sys.stdout

    def send(self, title: str, lines: list[str], *, mention: bool = False) -> None:
        prefix = "[mention owner] " if mention else ""
        print(f"{prefix}{title}", file=self.stream)
        for line in lines:
            print(f"  {line}", file=self.stream)
        self.stream.flush()


class DiscordWebhookNotifier:
    """Posts to a Discord webhook. URL and optional owner user id come from env vars."""

    def __init__(self, webhook_url_env: str, mention_user_id_env: str | None = None, opener=None):
        self.webhook_url_env = webhook_url_env
        self.mention_user_id_env = mention_user_id_env
        self._open = opener or urllib.request.urlopen

    def payload(self, title: str, lines: list[str], mention: bool) -> dict[str, Any]:
        uid = os.environ.get(self.mention_user_id_env or "", "") if mention else ""
        body = "\n".join([f"**{title}**", *lines])
        content = (f"<@{uid}> " if uid else "") + body
        return {
            "content": content[:1990],
            # Never let monitor text trigger @everyone/role pings.
            "allowed_mentions": {"parse": [], "users": [uid] if uid else []},
        }

    def send(self, title: str, lines: list[str], *, mention: bool = False) -> None:
        url = os.environ.get(self.webhook_url_env)
        if not url:
            raise RuntimeError(f"environment variable {self.webhook_url_env} is not set")
        req = urllib.request.Request(
            url,
            data=json.dumps(self.payload(title, lines, mention)).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "self-healing-homelab-monitor"},
            method="POST",
        )
        with self._open(req, timeout=20):
            pass


def build_notifiers(specs: list[dict[str, Any]], *, dry_run: bool = False) -> list[Notifier]:
    if dry_run:
        return [StdoutNotifier()]
    out: list[Notifier] = []
    for s in specs:
        if s["type"] == "stdout":
            out.append(StdoutNotifier())
        elif s["type"] == "discord_webhook":
            out.append(DiscordWebhookNotifier(s["webhook_url_env"], s.get("mention_user_id_env")))
    return out


def notify_events(notifiers: list[Notifier], host_label: str, events: list[Event]) -> bool:
    """Change-only: nothing is sent when there are no events. Returns True if sent."""
    if not events:
        return False
    lines = [e.render() for e in events]
    for n in notifiers:
        try:
            n.send(host_label, lines, mention=any(e.urgent for e in events))
        except Exception as exc:  # one broken notifier must not block the others
            print(f"notifier {type(n).__name__} failed: {type(exc).__name__}", file=sys.stderr)
    return True
