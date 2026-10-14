"""Shared fakes. No test ever performs real SSH, network or subprocess calls."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from homelab_monitor.checks import Probe


class FakeProbe(Probe):
    def __init__(self, run_map=None, tcp_up=None, http_status=None):
        self.run_map = run_map or {}  # substring of joined argv -> (rc, stdout)
        self.tcp_up = tcp_up or {}  # (host, port) -> bool
        self.http_status = http_status or {}
        self.calls: list[list[str]] = []

    def run(self, argv, timeout=40):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, result in self.run_map.items():
            if needle in joined:
                return result
        return 127, ""

    def tcp(self, host, port, timeout=5):
        return self.tcp_up.get((host, int(port)), False)

    def http(self, url, timeout=10):
        return self.http_status.get(url)


def ts_json(ip: str, online: bool) -> str:
    return json.dumps({"Peer": {"n1": {"TailscaleIPs": [ip], "Online": online}}})


@pytest.fixture
def day():
    return dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def night():
    return dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.timezone.utc)
