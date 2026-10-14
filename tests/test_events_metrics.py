import datetime as dt
import json
import stat

import pytest
from conftest import FakeProbe

from homelab_monitor import cli
from homelab_monitor.checks import Condition
from homelab_monitor.events import EventLog, issue_transitions, load_events
from homelab_monitor.ledger import Ledger
from homelab_monitor.metrics import episodes, parse_since, percentile, summarize, to_text
from homelab_monitor.state import evaluate

T0 = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc)


def at(minutes: float) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def ev(event, minutes, host="h", key="k", **kw):
    return {"event": event, "host": host, "key": key, "ts": at(minutes).isoformat(), "_t": at(minutes), **kw}


# --- event log -------------------------------------------------------------


def test_append_creates_0600_jsonl(tmp_path):
    log = EventLog.in_state_dir(tmp_path / "state", clock=lambda: T0)
    assert log.append("issue_opened", host="h", key="k", text="x", ignored=None)
    assert log.append("issue_resolved", host="h", key="k")
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    lines = [json.loads(x) for x in log.path.read_text().splitlines()]
    assert [x["event"] for x in lines] == ["issue_opened", "issue_resolved"]
    assert lines[0] == {"ts": "2026-01-01T00:00:00+00:00", "event": "issue_opened", "host": "h", "key": "k", "text": "x"}


def test_append_failure_returns_false_not_raise(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    assert EventLog(blocker / "events.jsonl").append("issue_opened", host="h") is False
    assert "event log write failed" in capsys.readouterr().err


def test_load_events_missing_and_bad_lines(tmp_path):
    assert load_events(tmp_path / "missing.jsonl") == ([], 0)
    p = tmp_path / "e.jsonl"
    p.write_text(
        '{"ts": "2026-01-01T00:05:00Z", "event": "b"}\nnot json\n\n{"event": "no ts"}\n{"ts": "2026-01-01T00:00:00", "event": "a"}\n'
    )
    events, bad = load_events(p)
    assert [e["event"] for e in events] == ["a", "b"] and bad == 2


# --- open/resolve hooks ----------------------------------------------------


def test_transitions_seed_flap_and_resolve(day):
    st, _ = evaluate("h", None, {"disk:/": Condition("disk 95%")}, day)
    assert issue_transitions(None, st) == [{"event": "issue_opened", "key": "disk:/", "text": "disk 95%", "urgent": False, "seed": True}]
    conds = {"disk:/": Condition("disk 95%"), "svc:web": Condition("web failed", urgent=True)}
    st2, _ = evaluate("h", st, conds, day)  # first sighting of svc:web: anti-flap, not reported
    assert issue_transitions(st, st2) == []
    st3, _ = evaluate("h", st2, conds, day)
    assert issue_transitions(st2, st3) == [{"event": "issue_opened", "key": "svc:web", "text": "web failed", "urgent": True}]
    st4, _ = evaluate("h", st3, {"svc:web": Condition("web failed")}, day)
    assert issue_transitions(st3, st4) == [{"event": "issue_resolved", "key": "disk:/", "text": "disk 95%"}]


def _cfg(tmp_path, flap=2):
    f = tmp_path / "c.json"
    f.write_text(
        json.dumps(
            {
                "state_dir": str(tmp_path / "state"),
                "flap_threshold": flap,
                "hosts": [{"name": "t", "label": "T", "checks": [{"type": "tcp", "id": "web", "host": "192.0.2.1", "port": 80}]}],
            }
        )
    )
    return f


def _events(tmp_path):
    return [json.loads(x) for x in (tmp_path / "state" / "events.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("flap,checks_until_open", [(1, 1), (3, 3)])
def test_check_logs_open_after_flap_threshold_and_resolve(tmp_path, monkeypatch, capsys, flap, checks_until_open):
    cfg = _cfg(tmp_path, flap)
    probe = FakeProbe(tcp_up={("192.0.2.1", 80): True})
    monkeypatch.setattr(cli, "Probe", lambda: probe)
    assert cli.main(["-c", str(cfg), "check", "all"]) == 0  # healthy baseline: nothing to log
    assert not (tmp_path / "state" / "events.jsonl").exists()
    probe.tcp_up[("192.0.2.1", 80)] = False
    for _ in range(checks_until_open - 1):
        cli.main(["-c", str(cfg), "check", "t"])
    assert not (tmp_path / "state" / "events.jsonl").exists()
    cli.main(["-c", str(cfg), "check", "t"])
    cli.main(["-c", str(cfg), "check", "t"])  # still down: no duplicate open
    probe.tcp_up[("192.0.2.1", 80)] = True
    cli.main(["-c", str(cfg), "check", "t"])
    e = _events(tmp_path)
    assert [(x["event"], x["host"], x["key"]) for x in e] == [("issue_opened", "t", "web"), ("issue_resolved", "t", "web")]
    assert "seed" not in e[0]


def test_check_seeds_first_run_issues(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "Probe", lambda: FakeProbe())
    cli.main(["-c", str(cfg), "check", "all"])
    assert _events(tmp_path)[0]["seed"] is True


def test_check_survives_event_log_failure(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    probe = FakeProbe(tcp_up={("192.0.2.1", 80): True})
    monkeypatch.setattr(cli, "Probe", lambda: probe)
    cli.main(["-c", str(cfg), "check", "all"])
    (tmp_path / "state" / "events.jsonl").mkdir()  # make every append fail
    probe.tcp_up[("192.0.2.1", 80)] = False
    cli.main(["-c", str(cfg), "check", "t"])
    assert cli.main(["-c", str(cfg), "check", "t"]) == 0
    out = capsys.readouterr()
    assert "not accepting TCP" in out.out and "event log write failed" in out.err
    state = json.loads((tmp_path / "state" / "t.json").read_text())
    assert "web" in state["reported"]  # monitoring state still advanced


# --- remediation events ----------------------------------------------------


def test_ledger_records_claimed_and_refused(tmp_path):
    log = EventLog(tmp_path / "e.jsonl", clock=lambda: T0)
    led = Ledger(tmp_path / "l.json", event_log=log, clock=lambda: 1000.0)
    assert led.claim("h:svc:web", "restart") and not led.claim("h:svc:web", "restart")
    e, _ = load_events(log.path)
    assert [(x["host"], x["key"], x["result"], x["source"]) for x in e] == [
        ("h", "svc:web", "claimed", "claim"),
        ("h", "svc:web", "refused", "claim"),
    ]


def test_report_records_result_and_stays_backward_compatible(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    assert cli.main(["-c", str(cfg), "report", "t", "plain message", "--dry-run"]) == 0
    assert not (tmp_path / "state" / "events.jsonl").exists()
    argv = [
        "-c",
        str(cfg),
        "report",
        "t",
        "Cause/Action/Result",
        "--dry-run",
        "--issue",
        "t:web",
        "--action",
        "restart",
        "--result",
        "fixed",
    ]
    assert cli.main(argv) == 0
    assert "remediation report" in capsys.readouterr().out
    (rec,) = _events(tmp_path)
    assert (rec["event"], rec["host"], rec["key"], rec["action"], rec["result"], rec["source"]) == (
        "remediation",
        "t",
        "web",
        "restart",
        "fixed",
        "report",
    )
    with pytest.raises(SystemExit):
        cli.main(["-c", str(cfg), "report", "t", "m", "--result", "fixed"])  # --issue required
    with pytest.raises(SystemExit):
        cli.main(["-c", str(cfg), "report", "t", "m", "--issue", "web", "--result", "maybe"])


# --- metrics math ----------------------------------------------------------


def test_percentile():
    assert percentile([], 50) is None
    assert percentile([5], 90) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(1, 11)), 90) == pytest.approx(9.1)


def test_summarize_ttr_and_mttr_split():
    events = [
        ev("issue_opened", 0, key="seeded", seed=True),
        ev("issue_resolved", 5, key="seeded"),  # excluded from counts and timing
        ev("issue_opened", 0, key="a"),
        ev("remediation", 2, key="a", action="restart", result="claimed", source="claim"),
        ev("issue_resolved", 4, key="a"),
        ev("remediation", 4.5, key="a", action="restart", result="fixed", source="report"),  # lands after resolve
        ev("issue_opened", 10, key="a"),
        ev("issue_resolved", 30, key="a"),  # second episode: no fix
        ev("issue_opened", 0, key="b"),
        ev("issue_resolved", 60, key="b"),
        ev("issue_opened", 100, key="c"),  # still open
    ]
    r = summarize(sorted(events, key=lambda e: e["_t"]), now=at(200))
    assert r["opened"] == {"h:a": 2, "h:b": 1, "h:c": 1} and r["opened_total"] == 4
    assert r["resolved_total"] == 4  # includes the seeded resolve
    assert r["still_open"] == ["h:c"]
    assert r["time_to_resolve"]["n"] == 3
    assert r["time_to_resolve"]["median_s"] == 20 * 60
    assert r["time_to_resolve"]["p90_s"] == pytest.approx((20 + 0.8 * 40) * 60)
    assert r["mttr_with_fix"] == {"n": 1, "median_s": 240, "p90_s": 240, "mean_s": 240}
    assert r["mttr_without_fix"]["n"] == 2 and r["mttr_without_fix"]["mean_s"] == 40 * 60
    assert r["remediations"] == {"claimed": 1, "fixed": 1}
    assert r["remediation_actions"] == {"claim:restart": 1, "report:restart": 1}
    assert [ep["fixed"] for ep in episodes(sorted(events, key=lambda e: e["_t"])) if ep["issue"] == ("h", "a")] == [True, False]
    assert "MTTR with a fixed remediation: mean 4m" in to_text(r, "all time")


def test_since_filter():
    events = [
        ev("issue_opened", 0, key="old"),
        ev("issue_resolved", 10, key="old"),
        ev("issue_opened", 100, key="new"),
        ev("issue_resolved", 103, key="new"),
    ]
    r = summarize(events, since=dt.timedelta(minutes=30), now=at(110))
    assert r["opened"] == {"h:new": 1} and r["resolved"] == {"h:new": 1}
    assert r["time_to_resolve"]["n"] == 1 and r["time_to_resolve"]["median_s"] == 180
    assert parse_since("all") is None and parse_since("7d") == dt.timedelta(days=7) and parse_since("24h") == dt.timedelta(hours=24)
    with pytest.raises(ValueError):
        parse_since("week")


def test_metrics_cli_empty_and_json(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    assert cli.main(["-c", str(cfg), "metrics"]) == 0
    assert "no events yet" in capsys.readouterr().out
    log = EventLog.in_state_dir(tmp_path / "state")
    now = dt.datetime.now(dt.timezone.utc)
    log.append("issue_opened", ts=now - dt.timedelta(minutes=3), host="t", key="web")
    log.append("issue_resolved", ts=now - dt.timedelta(minutes=1), host="t", key="web")
    assert cli.main(["-c", str(cfg), "metrics", "--since", "24h", "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["time_to_resolve"]["median_s"] == 120 and r["bad_lines"] == 0
    assert cli.main(["-c", str(cfg), "metrics", "--since", "soon"]) == 2
