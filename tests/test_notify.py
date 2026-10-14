import io
import json

from homelab_monitor.notify import DiscordWebhookNotifier, StdoutNotifier, build_notifiers, notify_events
from homelab_monitor.state import Event


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, title, lines, *, mention=False):
        self.sent.append((title, lines, mention))


class Broken:
    def send(self, *a, **k):
        raise RuntimeError("boom")


def test_no_events_no_notification():
    r = Recorder()
    assert notify_events([r], "Host", []) is False
    assert r.sent == []


def test_events_sent_with_mention_only_when_urgent():
    r = Recorder()
    notify_events([r], "Host", [Event("problem", "h", "k", "disk 95%")])
    notify_events([r], "Host", [Event("problem", "h", "vm:1", "VM stopped", urgent=True)])
    assert r.sent[0] == ("Host", ["⚠️ [h] disk 95%"], False)
    assert r.sent[1][2] is True


def test_broken_notifier_does_not_block_others(capsys):
    r = Recorder()
    notify_events([Broken(), r], "Host", [Event("recovered", "h", "k", "t")])
    assert len(r.sent) == 1
    assert "Broken failed" in capsys.readouterr().err


def test_stdout_notifier_format():
    buf = io.StringIO()
    StdoutNotifier(buf).send("T", ["a", "b"], mention=True)
    assert buf.getvalue() == "[mention owner] T\n  a\n  b\n"


def test_discord_payload_reads_env_and_blocks_mass_mentions(monkeypatch):
    captured = {}

    def fake_open(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    monkeypatch.setenv("TEST_HOOK", "https://discord.invalid/api/webhooks/x/y")
    monkeypatch.setenv("TEST_UID", "42")
    n = DiscordWebhookNotifier("TEST_HOOK", "TEST_UID", opener=fake_open)
    n.send("Title", ["@everyone line"], mention=True)
    assert captured["url"].startswith("https://discord.invalid/")
    assert captured["body"]["content"].startswith("<@42> **Title**")
    assert captured["body"]["allowed_mentions"] == {"parse": [], "users": ["42"]}


def test_discord_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    try:
        DiscordWebhookNotifier("NOPE").send("t", [])
    except RuntimeError as e:
        assert "NOPE" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_build_notifiers_dry_run_forces_stdout():
    ns = build_notifiers([{"type": "discord_webhook", "webhook_url_env": "X"}], dry_run=True)
    assert len(ns) == 1 and isinstance(ns[0], StdoutNotifier)
    ns = build_notifiers([{"type": "stdout"}, {"type": "discord_webhook", "webhook_url_env": "X"}])
    assert [type(n).__name__ for n in ns] == ["StdoutNotifier", "DiscordWebhookNotifier"]
