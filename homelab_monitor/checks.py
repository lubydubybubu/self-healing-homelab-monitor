"""Deterministic health checks. No LLM is involved anywhere in this module.

Every check returns a mapping ``{condition_key: Condition}``. An empty mapping
means healthy. ``None`` means *unknown* (e.g. the Tailscale CLI itself failed):
unknown results never create or clear conditions, so a broken probe cannot
produce false "recovered" messages.

All side effects go through a :class:`Probe`, so tests inject a fake and no real
SSH/network call is ever made.
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .config import CheckSpec, HostSpec


@dataclass(frozen=True)
class Condition:
    text: str
    urgent: bool = False
    immediate: bool = False  # bypass anti-flap (report on first sighting)


class Probe:
    """Real side-effect layer. Replace with a fake in tests."""

    def run(self, argv: list[str], timeout: int = 40) -> tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 255, f"{type(e).__name__}"
        return p.returncode, p.stdout

    def tcp(self, host: str, port: int, timeout: float = 5) -> bool:
        try:
            socket.create_connection((host, int(port)), timeout=timeout).close()
            return True
        except OSError:
            return False

    def http(self, url: str, timeout: float = 10) -> int | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - URL comes from operator config
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, OSError):
            return None


def ssh_argv(host: HostSpec, remote: str) -> list[str]:
    assert host.ssh is not None
    s = host.ssh
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={s.connect_timeout}",
        "-o",
        f"StrictHostKeyChecking={s.strict_host_key_checking}",
        "-p",
        str(s.port),
    ]
    if s.key_path:
        argv += ["-o", "IdentitiesOnly=yes", "-i", s.key_path]
    return argv + [f"{s.user}@{s.address}", remote]


def _exec(probe: Probe, host: HostSpec, spec: CheckSpec, cmd: str, timeout: int = 60) -> tuple[int, str]:
    if spec.params.get("via", "local") == "ssh":
        return probe.run(ssh_argv(host, cmd), timeout)
    return probe.run(["sh", "-c", cmd], timeout)


def _cond(spec: CheckSpec, text: str) -> Condition:
    return Condition(text=text, urgent=spec.urgent, immediate=spec.immediate)


# ---------------------------------------------------------------- check impls


def check_tailscale_online(probe: Probe, host: HostSpec, spec: CheckSpec):
    rc, out = probe.run(["tailscale", "status", "--json"], 20)
    try:
        peers = (json.loads(out).get("Peer") or {}).values()
    except (ValueError, AttributeError):
        return None
    for p in peers:
        if host.tailscale_ip in (p.get("TailscaleIPs") or []):
            if p.get("Online"):
                return {}
            return {spec.id: _cond(spec, f"{host.label} offline (powered off, asleep or tailnet down)")}
    return None  # peer not found -> unknown, not "down"


def check_tcp(probe: Probe, host: HostSpec, spec: CheckSpec):
    h, port = spec.params["host"], int(spec.params["port"])
    if probe.tcp(h, port, float(spec.params.get("timeout", 5))):
        return {}
    name = spec.params.get("name", f"{h}:{port}")
    return {spec.id: _cond(spec, f"{name} not accepting TCP connections")}


def check_http(probe: Probe, host: HostSpec, spec: CheckSpec):
    url = spec.params["url"]
    expect = int(spec.params.get("expect_status", 200))
    status = probe.http(url, float(spec.params.get("timeout", 10)))
    if status == expect:
        return {}
    name = spec.params.get("name", url)
    got = "no response" if status is None else f"HTTP {status}"
    return {spec.id: _cond(spec, f"{name} unhealthy ({got}, expected {expect})")}


def check_ssh_reachable(probe: Probe, host: HostSpec, spec: CheckSpec):
    rc, _ = probe.run(ssh_argv(host, "true"), 30)
    if rc == 0:
        return {}
    return {spec.id: _cond(spec, f"{host.label} is up but remote check over SSH failed")}


def check_command(probe: Probe, host: HostSpec, spec: CheckSpec):
    rc, out = _exec(probe, host, spec, spec.params["command"], int(spec.params.get("timeout", 60)))
    if rc == 0:
        return {}
    name = spec.params.get("name", spec.id)
    return {spec.id: _cond(spec, f"{name} failed (exit {rc})")}


def check_systemd_units(probe: Probe, host: HostSpec, spec: CheckSpec):
    units = [str(u) for u in spec.params["units"]]
    user = "--user " if spec.params.get("user") else ""
    cmd = f"systemctl {user}is-active " + " ".join(shlex.quote(u) for u in units)
    rc, out = _exec(probe, host, spec, cmd, 30)
    states = out.split()
    if len(states) != len(units):
        return {f"{spec.id}:query": _cond(spec, f"could not query units on {host.label}")}
    result = {}
    for unit, state in zip(units, states, strict=True):
        if state != "active":
            short = unit.removesuffix(".service")
            result[f"svc:{short}"] = _cond(spec, f"service {short} on {host.label} is {state}")
    return result


def parse_qm_list(text: str) -> dict[str, tuple[str, str]]:
    """Parse ``qm list`` output -> {vmid: (name, status)}."""
    vms: dict[str, tuple[str, str]] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit():
            vms[parts[0]] = (parts[1], parts[2])
    return vms


def check_proxmox_vms(probe: Probe, host: HostSpec, spec: CheckSpec):
    rc, out = probe.run(ssh_argv(host, "qm list"), 60)
    if rc != 0:
        return {"pve": Condition(f"{host.label}: Proxmox unreachable or not responding", urgent=True, immediate=True)}
    vms = parse_qm_list(out)
    watch = {str(v) for v in spec.params.get("vms") or vms.keys()}
    result = {}
    for vid in sorted(watch):
        if vid not in vms:
            result[f"vm:{vid}"] = _cond(spec, f"VM {vid} missing from qm list")
        elif vms[vid][1] != "running":
            name, status = vms[vid]
            result[f"vm:{vid}"] = _cond(spec, f"VM {vid} ({name}) {status}")
    return result


def parse_df(text: str) -> dict[str, int]:
    """Parse ``df -P`` output -> {mountpoint: used_percent}."""
    usage: dict[str, int] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[4].endswith("%"):
            usage[parts[5]] = int(parts[4].rstrip("%"))
    return usage


def check_disk_usage(probe: Probe, host: HostSpec, spec: CheckSpec):
    paths = [str(p) for p in spec.params.get("paths", ["/"])]
    threshold = int(spec.params.get("threshold", 90))
    rc, out = _exec(probe, host, spec, "df -P " + " ".join(shlex.quote(p) for p in paths), 30)
    usage = parse_df(out)
    if not usage:
        return None
    return {
        f"disk:{mp}": _cond(spec, f"{host.label} disk {mp} {pct}% used (threshold {threshold}%)")
        for mp, pct in usage.items()
        if pct >= threshold
    }


CHECKS: dict[str, Callable] = {
    "tailscale_online": check_tailscale_online,
    "tcp": check_tcp,
    "http": check_http,
    "ssh_reachable": check_ssh_reachable,
    "command": check_command,
    "systemd_units": check_systemd_units,
    "proxmox_vms": check_proxmox_vms,
    "disk_usage": check_disk_usage,
}


def collect(host: HostSpec, probe: Probe) -> dict[str, Condition] | None:
    """Run all checks for a host.

    If a reachability check (tailscale_online / ssh_reachable) fails, the
    remaining checks are skipped - there is no point checking services on a
    machine that is off. Returns None if the very first check is unknown.
    """
    conditions: dict[str, Condition] = {}
    for i, spec in enumerate(host.checks):
        res = CHECKS[spec.type](probe, host, spec)
        if res is None:
            if i == 0 and spec.type == "tailscale_online":
                return None
            continue
        conditions.update(res)
        if res and spec.type in {"tailscale_online", "ssh_reachable"}:
            break
    return conditions
