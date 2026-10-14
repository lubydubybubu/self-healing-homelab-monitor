"""Command line entry point: ``python -m homelab_monitor <command>``."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from . import __version__
from .checks import Probe, collect
from .config import ConfigError, load_config
from .gate import run_gate
from .ledger import Ledger
from .notify import build_notifiers, notify_events
from .state import evaluate, load_state, save_state


def cmd_check(cfg, args, probe=None) -> int:
    probe = probe or Probe()
    notifiers = build_notifiers(cfg.notifiers, dry_run=args.dry_run)
    names = [h.name for h in cfg.hosts] if args.host == "all" else [args.host]
    now = dt.datetime.now(cfg.tz)
    for name in names:
        host = cfg.host(name)
        path = cfg.state_dir / f"{name}.json"
        conditions = collect(host, probe)
        new_state, events = evaluate(
            name,
            load_state(path),
            conditions,
            now,
            flap_threshold=cfg.flap_threshold,
            quiet_hours=cfg.quiet_hours,
        )
        notify_events(notifiers, host.label, events)
        save_state(path, new_state)
    return 0


def cmd_gate(cfg, args) -> int:
    res = run_gate(cfg.state_dir, [h.name for h in cfg.hosts], cfg.gate_skip, commit=not args.peek)
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False))
    else:
        text = res.to_text()
        if text:
            print(text)
    return 0


def cmd_report(cfg, args) -> int:
    notifiers = build_notifiers(cfg.notifiers, dry_run=args.dry_run)
    label = cfg.host(args.host).label
    for n in notifiers:
        n.send(f"{label} · remediation report", args.message.splitlines(), mention=True)
    return 0


def cmd_claim(cfg, args) -> int:
    ok = Ledger(cfg.state_dir / "ledger.json", window_s=args.window).claim(args.issue, args.action)
    print("claimed" if ok else "already-attempted: ASK FIRST")
    return 0 if ok else 3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="homelab-monitor", description="Deterministic homelab monitor + agent gate")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="run checks for one host (or 'all'); notify on change only")
    c.add_argument("host")
    c.add_argument("--dry-run", action="store_true", help="print notifications to stdout only")
    c.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="repeat forever (demo/container mode)")

    g = sub.add_parser("gate", help="print issue-set diff; empty output = do not wake the agent")
    g.add_argument("--json", action="store_true")
    g.add_argument("--peek", action="store_true", help="do not record the new hash")

    r = sub.add_parser("report", help="send an owner-mention remediation report")
    r.add_argument("host")
    r.add_argument("message")
    r.add_argument("--dry-run", action="store_true")

    k = sub.add_parser("claim", help="claim at-most-once remediation for host:key (exit 3 = already tried)")
    k.add_argument("issue")
    k.add_argument("action")
    k.add_argument("--window", type=int, default=24 * 3600)

    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ConfigError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.cmd == "check":
        while True:
            cmd_check(cfg, args)
            if not args.loop:
                return 0
            if args.dry_run:
                gate = run_gate(cfg.state_dir, [h.name for h in cfg.hosts], cfg.gate_skip).to_text()
                if gate:
                    print("--- gate changed: an agent would be woken with ---\n" + gate, flush=True)
            time.sleep(args.loop)
    return {"gate": cmd_gate, "report": cmd_report, "claim": cmd_claim}[args.cmd](cfg, args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
