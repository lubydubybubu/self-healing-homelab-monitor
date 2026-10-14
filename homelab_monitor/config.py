"""Configuration loading and validation.

The config file is YAML (JSON is accepted too, since JSON is a YAML subset and
is used as a fallback when PyYAML is not installed).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

CHECK_TYPES = {
    "tailscale_online",
    "tcp",
    "http",
    "ssh_reachable",
    "command",
    "systemd_units",
    "proxmox_vms",
    "disk_usage",
}
NOTIFIER_TYPES = {"stdout", "discord_webhook"}


class ConfigError(ValueError):
    """Raised for any invalid configuration."""


@dataclass
class SSHTarget:
    user: str
    address: str
    key_path: str | None = None
    port: int = 22
    connect_timeout: int = 8
    strict_host_key_checking: str = "yes"


@dataclass
class CheckSpec:
    type: str
    id: str
    params: dict[str, Any]
    urgent: bool = False
    immediate: bool = False


@dataclass
class HostSpec:
    name: str
    label: str
    checks: list[CheckSpec]
    ssh: SSHTarget | None = None
    tailscale_ip: str | None = None


@dataclass
class QuietHours:
    start: int = 23
    end: int = 8

    def contains(self, hour: int) -> bool:
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= hour < self.end
        return hour >= self.start or hour < self.end


@dataclass
class Config:
    state_dir: pathlib.Path
    hosts: list[HostSpec]
    notifiers: list[dict[str, Any]]
    tz: dt.tzinfo = dt.timezone.utc
    flap_threshold: int = 2
    quiet_hours: QuietHours | None = None
    gate_skip: set[tuple[str, str]] = field(default_factory=set)

    def host(self, name: str) -> HostSpec:
        for h in self.hosts:
            if h.name == name:
                return h
        raise ConfigError(f"unknown host: {name}")


_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def _parse_offset(value: str) -> dt.tzinfo:
    m = _OFFSET.match(str(value))
    if not m:
        raise ConfigError(f"utc_offset must look like +09:00, got {value!r}")
    sign = 1 if m.group(1) == "+" else -1
    return dt.timezone(sign * dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def _load_text(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover - exercised only without PyYAML
        return json.loads(text)
    return yaml.safe_load(text) or {}


def _req(d: dict, key: str, where: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"{where}: missing required key '{key}'")
    return d[key]


def _parse_ssh(raw: dict, where: str) -> SSHTarget:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}.ssh must be a mapping")
    key = raw.get("key_path")
    return SSHTarget(
        user=_req(raw, "user", f"{where}.ssh"),
        address=_req(raw, "address", f"{where}.ssh"),
        key_path=os.path.expanduser(key) if key else None,
        port=int(raw.get("port", 22)),
        connect_timeout=int(raw.get("connect_timeout", 8)),
        strict_host_key_checking=str(raw.get("strict_host_key_checking", "yes")),
    )


def _parse_check(raw: dict, host: HostSpec, idx: int) -> CheckSpec:
    where = f"hosts[{host.name}].checks[{idx}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    ctype = _req(raw, "type", where)
    if ctype not in CHECK_TYPES:
        raise ConfigError(f"{where}: unknown check type '{ctype}'")
    params = {k: v for k, v in raw.items() if k not in {"type", "id", "urgent", "immediate"}}
    needs_ssh = ctype in {"ssh_reachable", "proxmox_vms"} or (
        ctype in {"command", "systemd_units", "disk_usage"} and params.get("via", "local") == "ssh"
    )
    if needs_ssh and host.ssh is None:
        raise ConfigError(f"{where}: check '{ctype}' needs an ssh block on the host")
    if ctype == "tailscale_online" and not host.tailscale_ip:
        raise ConfigError(f"{where}: tailscale_online needs host.tailscale_ip")
    if ctype == "tcp":
        _req(params, "host", where)
        _req(params, "port", where)
    if ctype == "http":
        _req(params, "url", where)
    if ctype == "command":
        _req(params, "command", where)
    if ctype == "systemd_units" and not params.get("units"):
        raise ConfigError(f"{where}: systemd_units needs a non-empty 'units' list")
    default_id = {"tailscale_online": "offline", "ssh_reachable": "ssh"}.get(ctype, ctype)
    cid = str(raw.get("id", default_id))
    if not re.match(r"^[A-Za-z0-9_.-]+$", cid):
        raise ConfigError(f"{where}: id may only contain letters, digits, '_', '.', '-'")
    return CheckSpec(
        type=ctype,
        id=cid,
        params=params,
        urgent=bool(raw.get("urgent", False)),
        immediate=bool(raw.get("immediate", False)),
    )


def parse_config(data: dict[str, Any], base_dir: pathlib.Path | None = None) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    base_dir = base_dir or pathlib.Path.cwd()
    hosts_raw = data.get("hosts") or []
    if not hosts_raw:
        raise ConfigError("at least one host is required")
    hosts: list[HostSpec] = []
    names: set[str] = set()
    for i, hr in enumerate(hosts_raw):
        name = _req(hr, "name", f"hosts[{i}]")
        if name in names:
            raise ConfigError(f"duplicate host name: {name}")
        names.add(name)
        host = HostSpec(
            name=name,
            label=hr.get("label", name),
            checks=[],
            ssh=_parse_ssh(hr["ssh"], f"hosts[{name}]") if hr.get("ssh") else None,
            tailscale_ip=hr.get("tailscale_ip"),
        )
        ids: set[str] = set()
        for j, cr in enumerate(hr.get("checks") or []):
            spec = _parse_check(cr, host, j)
            if spec.id in ids:
                raise ConfigError(f"hosts[{name}]: duplicate check id '{spec.id}'")
            ids.add(spec.id)
            host.checks.append(spec)
        hosts.append(host)

    notifiers = data.get("notifiers") or [{"type": "stdout"}]
    for n in notifiers:
        if n.get("type") not in NOTIFIER_TYPES:
            raise ConfigError(f"unknown notifier type: {n.get('type')}")
        if n["type"] == "discord_webhook" and not n.get("webhook_url_env"):
            raise ConfigError("discord_webhook notifier needs webhook_url_env (never put the URL in the file)")

    qh = data.get("quiet_hours")
    quiet = None
    if qh:
        quiet = QuietHours(int(qh.get("start", 23)), int(qh.get("end", 8)))
        if not (0 <= quiet.start < 24 and 0 <= quiet.end < 24):
            raise ConfigError("quiet_hours start/end must be 0-23")

    skip: set[tuple[str, str]] = set()
    for item in (data.get("gate") or {}).get("skip") or []:
        if ":" not in item:
            raise ConfigError(f"gate.skip entries look like host:key, got {item!r}")
        h, k = item.split(":", 1)
        skip.add((h, k))

    flap = int(data.get("flap_threshold", 2))
    if flap < 1:
        raise ConfigError("flap_threshold must be >= 1")

    state_dir = pathlib.Path(os.path.expanduser(str(data.get("state_dir", "./state"))))
    if not state_dir.is_absolute():
        state_dir = base_dir / state_dir
    return Config(
        state_dir=state_dir,
        hosts=hosts,
        notifiers=notifiers,
        tz=_parse_offset(data.get("utc_offset", "+00:00")),
        flap_threshold=flap,
        quiet_hours=quiet,
        gate_skip=skip,
    )


def load_config(path: str | os.PathLike) -> Config:
    p = pathlib.Path(path)
    return parse_config(_load_text(p.read_text(encoding="utf-8")), base_dir=p.parent.resolve())
