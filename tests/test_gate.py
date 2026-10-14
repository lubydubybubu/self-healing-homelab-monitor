import json

from homelab_monitor.gate import Issue, diff, issue_set_hash, run_gate
from homelab_monitor.state import save_state


def write_host(d, host, reported):
    save_state(d / f"{host}.json", {"reported": {k: {"text": v, "urgent": False} for k, v in reported.items()}})


def test_hash_is_order_independent_and_ignores_text():
    a = [Issue("h1", "disk:/", "91%"), Issue("h2", "svc:web", "failed")]
    b = [Issue("h2", "svc:web", "inactive"), Issue("h1", "disk:/", "97%")]
    assert issue_set_hash(a) == issue_set_hash(b)


def test_hash_is_stable_known_value():
    # Pin the canonical encoding so a refactor cannot silently re-wake every agent.
    assert issue_set_hash([]) == "4f53cda18c2baa0c"
    assert issue_set_hash([Issue("h", "k", "")]) == issue_set_hash([Issue("h", "k", "other")])


def test_hash_changes_with_set():
    assert issue_set_hash([Issue("h", "a", "")]) != issue_set_hash([Issue("h", "b", "")])


def test_first_run_empty_is_not_change():
    assert diff(None, []).changed is False


def test_first_run_with_issues_is_change():
    r = diff(None, [Issue("h", "k", "t")])
    assert r.changed and [i.id for i in r.added] == ["h:k"]


def test_run_gate_change_only(tmp_path):
    hosts = ["srv", "pc"]
    write_host(tmp_path, "srv", {})
    assert run_gate(tmp_path, hosts).changed is False

    write_host(tmp_path, "srv", {"svc:web": "web failed"})
    r = run_gate(tmp_path, hosts)
    assert r.changed and [i.id for i in r.added] == ["srv:svc:web"]
    assert "+\tsrv\tsvc:web\tweb failed" in r.to_text()

    # same set again (even with new text) -> no wake
    write_host(tmp_path, "srv", {"svc:web": "web inactive"})
    r = run_gate(tmp_path, hosts)
    assert not r.changed and r.to_text() == ""

    # new issue on another host; old one shown as '='
    write_host(tmp_path, "pc", {"ssh": "ssh failed"})
    r = run_gate(tmp_path, hosts)
    assert [i.id for i in r.added] == ["pc:ssh"]
    assert "=\tsrv\tsvc:web" in r.to_text()

    # recovery
    write_host(tmp_path, "srv", {})
    r = run_gate(tmp_path, hosts)
    assert r.changed and [i.id for i in r.removed] == ["srv:svc:web"]
    assert r.removed[0].text == "web inactive"


def test_peek_does_not_consume(tmp_path):
    write_host(tmp_path, "srv", {})
    run_gate(tmp_path, ["srv"])
    write_host(tmp_path, "srv", {"x": "y"})
    assert run_gate(tmp_path, ["srv"], commit=False).changed
    assert run_gate(tmp_path, ["srv"]).changed  # still pending until committed
    assert not run_gate(tmp_path, ["srv"]).changed


def test_skip_list(tmp_path):
    write_host(tmp_path, "laptop", {"offline": "laptop offline"})
    r = run_gate(tmp_path, ["laptop"], skip={("laptop", "offline")})
    assert r.current == [] and not r.changed


def test_legacy_string_values_and_json(tmp_path):
    save_state(tmp_path / "h.json", {"reported": {"k": "plain text"}})
    r = run_gate(tmp_path, ["h"])
    d = r.to_dict()
    assert d["current"] == [{"host": "h", "key": "k", "text": "plain text"}]
    json.dumps(d)
