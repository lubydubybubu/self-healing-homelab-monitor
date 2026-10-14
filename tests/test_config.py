import json
import pathlib

import pytest

from homelab_monitor.config import ConfigError, QuietHours, load_config, parse_config

ROOT = pathlib.Path(__file__).resolve().parents[1]


def minimal(**over):
    d = {"hosts": [{"name": "a", "checks": [{"type": "tcp", "host": "192.0.2.1", "port": 22}]}]}
    d.update(over)
    return d


def test_example_config_parses():
    cfg = load_config(ROOT / "config.example.yaml")
    assert [h.name for h in cfg.hosts] == ["hypervisor", "app-vm", "desktop", "laptop"]
    assert cfg.state_dir == ROOT / "state"
    assert ("laptop", "offline") in cfg.gate_skip
    assert cfg.host("desktop").checks[0].id == "offline"
    assert cfg.host("hypervisor").ssh.key_path.endswith("monitor_ed25519")
    assert cfg.quiet_hours.contains(23) and cfg.quiet_hours.contains(3) and not cfg.quiet_hours.contains(12)


def test_demo_config_parses():
    cfg = load_config(ROOT / "demo" / "config.demo.yaml")
    assert cfg.host("demo-target").checks


def test_json_config_accepted(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(minimal(state_dir="/tmp/x")))
    cfg = load_config(p)
    assert str(cfg.state_dir) == "/tmp/x"
    assert cfg.notifiers == [{"type": "stdout"}]  # default


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"hosts": []}, "at least one host"),
        ({"hosts": [{"name": "a"}, {"name": "a"}]}, "duplicate host"),
        ({"hosts": [{"name": "a", "checks": [{"type": "nope"}]}]}, "unknown check type"),
        ({"hosts": [{"name": "a", "checks": [{"type": "proxmox_vms"}]}]}, "needs an ssh block"),
        ({"hosts": [{"name": "a", "checks": [{"type": "tailscale_online"}]}]}, "tailscale_ip"),
        ({"hosts": [{"name": "a", "checks": [{"type": "tcp", "host": "x"}]}]}, "'port'"),
        ({"hosts": [{"name": "a", "checks": [{"type": "systemd_units", "units": []}]}]}, "non-empty"),
        ({"hosts": [{"name": "a", "checks": [{"type": "http", "url": "u"}, {"type": "http", "url": "v"}]}]}, "duplicate check id"),
        (minimal(notifiers=[{"type": "discord_webhook"}]), "webhook_url_env"),
        (minimal(notifiers=[{"type": "carrier-pigeon"}]), "unknown notifier"),
        (minimal(utc_offset="KST"), "utc_offset"),
        (minimal(flap_threshold=0), "flap_threshold"),
        (minimal(gate={"skip": ["nocolon"]}), "host:key"),
        (minimal(quiet_hours={"start": 25, "end": 8}), "0-23"),
    ],
)
def test_invalid_configs(data, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(data)


def test_utc_offset_and_ssh_defaults():
    cfg = parse_config(
        {
            "utc_offset": "+09:00",
            "hosts": [{"name": "h", "ssh": {"user": "u", "address": "192.0.2.5"}, "checks": [{"type": "ssh_reachable"}]}],
        }
    )
    assert cfg.tz.utcoffset(None).total_seconds() == 9 * 3600
    assert cfg.host("h").ssh.port == 22 and cfg.host("h").ssh.key_path is None


def test_quiet_hours_variants():
    assert QuietHours(1, 5).contains(3) and not QuietHours(1, 5).contains(5)
    assert not QuietHours(0, 0).contains(0)
