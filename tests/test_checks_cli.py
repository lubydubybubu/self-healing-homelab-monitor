import json

from conftest import FakeProbe, ts_json

from homelab_monitor import cli
from homelab_monitor.checks import collect, parse_df, parse_qm_list, ssh_argv
from homelab_monitor.config import parse_config
from homelab_monitor.ledger import Ledger

QM = """      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID
       101 app-vm               running    4096              32.00 1234
       102 nas                  stopped    2048              16.00 0
"""
DF = """Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/sda1 100 95 5 95% /
/dev/sdb1 100 10 90 10% /data
"""


def host(checks, **extra):
    h = {"name": "h", "label": "H", "ssh": {"user": "u", "address": "192.0.2.9", "key_path": "/k"}, "checks": checks}
    h.update(extra)
    return parse_config({"hosts": [h]}).host("h")


def test_parsers():
    assert parse_qm_list(QM) == {"101": ("app-vm", "running"), "102": ("nas", "stopped")}
    assert parse_df(DF) == {"/": 95, "/data": 10}


def test_ssh_argv_is_batch_and_strict():
    argv = ssh_argv(host([]), "true")
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
    assert argv[-2:] == ["u@192.0.2.9", "true"] and "/k" in argv


def test_proxmox_and_disk_checks():
    h = host([{"type": "proxmox_vms", "urgent": True}, {"type": "disk_usage", "via": "ssh", "paths": ["/", "/data"]}])
    p = FakeProbe({"qm list": (0, QM), "df -P": (0, DF)})
    c = collect(h, p)
    assert set(c) == {"vm:102", "disk:/"}
    assert c["vm:102"].urgent


def test_proxmox_unreachable_is_urgent_immediate():
    c = collect(host([{"type": "proxmox_vms"}]), FakeProbe({"qm list": (255, "")}))
    assert c["pve"].urgent and c["pve"].immediate


def test_offline_short_circuits_and_unknown():
    h = host([{"type": "tailscale_online"}, {"type": "ssh_reachable"}], tailscale_ip="100.64.0.1")
    p = FakeProbe({"tailscale status": (0, ts_json("100.64.0.1", False))})
    assert set(collect(h, p)) == {"offline"}
    assert not any("ssh" in call[0] for call in p.calls)
    assert collect(h, FakeProbe({"tailscale status": (1, "not json")})) is None


def test_systemd_units_and_tcp_http():
    h = host(
        [
            {"type": "systemd_units", "via": "ssh", "units": ["web.service", "bot.service"]},
            {"type": "tcp", "id": "smb", "host": "192.0.2.3", "port": 445},
            {"type": "http", "id": "hc", "url": "http://192.0.2.3/h"},
        ]
    )
    p = FakeProbe({"is-active": (3, "active\nfailed\n")}, tcp_up={("192.0.2.3", 445): True}, http_status={"http://192.0.2.3/h": 503})
    c = collect(h, p)
    assert set(c) == {"svc:bot", "hc"}
    assert "HTTP 503" in c["hc"].text


def test_ledger_at_most_once(tmp_path):
    t = [1000.0]
    led = Ledger(tmp_path / "l.json", window_s=100, clock=lambda: t[0])
    assert led.claim("h:svc:web", "restart") is True
    assert led.claim("h:svc:web", "restart") is False
    assert led.claim("h:other", "x") is True
    t[0] += 101
    assert led.claim("h:svc:web", "restart") is True


def test_cli_end_to_end_with_fake_probe(tmp_path, monkeypatch, capsys):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(
        json.dumps(
            {
                "state_dir": str(tmp_path / "state"),
                "hosts": [{"name": "t", "label": "Target", "checks": [{"type": "tcp", "id": "web", "host": "192.0.2.1", "port": 80}]}],
            }
        )
    )
    probe = FakeProbe(tcp_up={("192.0.2.1", 80): True})
    monkeypatch.setattr(cli, "Probe", lambda: probe)

    def run(*argv):
        assert cli.main(["-c", str(cfg_file), *argv]) == 0
        return capsys.readouterr().out

    assert run("check", "all") == ""  # baseline
    probe.tcp_up[("192.0.2.1", 80)] = False
    assert run("check", "t") == ""  # first sighting (anti-flap)
    out = run("check", "t")
    assert "⚠️ [t] 192.0.2.1:80 not accepting TCP connections" in out
    assert run("check", "t") == ""  # no repeat
    g = run("gate")
    assert g.startswith("# issue set") and "+\tt\tweb" in g
    assert run("gate") == ""  # consumed
    assert cli.main(["-c", str(cfg_file), "claim", "t:web", "restart"]) == 0
    assert cli.main(["-c", str(cfg_file), "claim", "t:web", "restart"]) == 3
    capsys.readouterr()
    probe.tcp_up[("192.0.2.1", 80)] = True
    assert "recovered" in run("check", "t")
    assert "-\tt\tweb" in run("gate")


def test_cli_bad_config(tmp_path, capsys):
    assert cli.main(["-c", str(tmp_path / "missing.yaml"), "gate"]) == 2
