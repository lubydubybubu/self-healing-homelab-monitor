import datetime as dt

from homelab_monitor.checks import Condition
from homelab_monitor.config import QuietHours
from homelab_monitor.state import evaluate, load_state, save_state

C = Condition


def step(prev, conds, now, **kw):
    return evaluate("h", prev, conds, now, **kw)


def test_first_run_is_silent_baseline(day):
    st, ev = step(None, {"disk:/": C("disk 95%")}, day)
    assert ev == []
    assert "disk:/" in st["reported"]


def test_anti_flap_requires_two_sightings(day):
    st, _ = step(None, {}, day)
    st, ev = step(st, {"svc:web": C("web failed")}, day)
    assert ev == []
    st, ev = step(st, {"svc:web": C("web failed")}, day)
    assert [(e.kind, e.key) for e in ev] == [("problem", "svc:web")]
    # third sighting: already reported -> no repeat
    st, ev = step(st, {"svc:web": C("web failed")}, day)
    assert ev == []


def test_single_blip_never_reported(day):
    st, _ = step(None, {}, day)
    st, ev1 = step(st, {"svc:web": C("x")}, day)
    st, ev2 = step(st, {}, day)
    assert ev1 == [] and ev2 == []
    assert st["seen"] == {}


def test_immediate_bypasses_flap(day):
    st, _ = step(None, {}, day)
    st, ev = step(st, {"pve": C("down", urgent=True, immediate=True)}, day)
    assert [e.key for e in ev] == ["pve"] and ev[0].urgent


def test_recovery_reported_once(day):
    st, _ = step(None, {"vm:101": C("VM 101 stopped")}, day)
    st, ev = step(st, {}, day)
    assert [(e.kind, e.text) for e in ev] == [("recovered", "VM 101 stopped")]
    st, ev = step(st, {}, day)
    assert ev == []


def test_text_change_is_not_a_new_event(day):
    st, _ = step(None, {"disk:/": C("91%")}, day)
    st, ev = step(st, {"disk:/": C("92%")}, day)
    assert ev == [] and st["reported"]["disk:/"]["text"] == "92%"


def test_unknown_leaves_state_untouched(day):
    st, _ = step(None, {"offline": C("off")}, day)
    st2, ev = step(st, None, day)
    assert ev == [] and st2["reported"] == st["reported"]


def test_quiet_hours_hold_and_flush(night, day):
    q = QuietHours(23, 8)
    st, _ = step(None, {}, night, quiet_hours=q)
    st, _ = step(st, {"disk:/": C("disk")}, night, quiet_hours=q)
    st, ev = step(st, {"disk:/": C("disk")}, night, quiet_hours=q)
    assert ev == [] and len(st["held"]) == 1
    st, ev = step(st, {"disk:/": C("disk")}, day, quiet_hours=q)
    assert [e.key for e in ev] == ["disk:/"] and st["held"] == []


def test_urgent_delivered_during_quiet_hours(night):
    q = QuietHours(23, 8)
    st, _ = step(None, {}, night, quiet_hours=q)
    st, ev = step(st, {"vm:1": C("stopped", urgent=True, immediate=True)}, night, quiet_hours=q)
    assert [e.key for e in ev] == ["vm:1"]


def test_flap_threshold_one(day):
    st, _ = step(None, {}, day, flap_threshold=1)
    st, ev = step(st, {"x": C("x")}, day, flap_threshold=1)
    assert len(ev) == 1


def test_state_roundtrip_atomic(tmp_path, day):
    p = tmp_path / "sub" / "h.json"
    st, _ = step(None, {"k": C("t")}, day)
    save_state(p, st)
    assert load_state(p) == st
    assert not p.with_suffix(".json.tmp").exists()
    assert load_state(tmp_path / "missing.json") is None


def test_events_sorted_and_rendered(day):
    st, _ = step(None, {"b": C("B"), "a": C("A")}, day)
    st, ev = step(st, {}, dt.datetime(2026, 1, 1, 13, tzinfo=dt.timezone.utc))
    assert [e.key for e in ev] == ["a", "b"]
    assert ev[0].render() == "✅ [h] recovered: A"
