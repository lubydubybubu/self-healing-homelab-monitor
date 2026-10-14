"""Read-only metrics over ``events.jsonl``.

Episodes: each ``issue_opened`` is paired with the next ``issue_resolved`` for
the same ``host + key``. Seeded opens (first-run baseline, real start unknown)
are excluded from counts and timing. An episode counts as *remediated* when a
``remediation`` event with ``result == "fixed"`` for the same issue falls after
its open and before the next open of that issue - a fix report usually lands
just after the monitor already saw the recovery, so the window is not cut at
the resolve time.

Period filter (``--since``): opened / resolved / remediation counts use the
event time; time-to-resolve uses the resolve time.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from typing import Any

FOREVER = dt.datetime.max.replace(tzinfo=dt.timezone.utc)


def parse_since(s: str | None) -> dt.timedelta | None:
    if not s or s == "all":
        return None
    m = re.fullmatch(r"(\d+)([dhm])", s)
    if not m:
        raise ValueError("--since must look like 7d, 24h, 30m or all")
    n, unit = int(m.group(1)), m.group(2)
    return {"d": dt.timedelta(days=n), "h": dt.timedelta(hours=n), "m": dt.timedelta(minutes=n)}[unit]


def percentile(xs: list[float], p: float) -> float | None:
    """Linear-interpolated percentile, ``p`` in 0..100. ``None`` for no data."""
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(secs: list[float]) -> dict[str, Any]:
    return {
        "n": len(secs),
        "median_s": percentile(secs, 50),
        "p90_s": percentile(secs, 90),
        "mean_s": sum(secs) / len(secs) if secs else None,
    }


def _issue(e: dict[str, Any]) -> tuple[str, str]:
    return (str(e.get("host")), str(e.get("key")))


def episodes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    opens: dict[tuple[str, str], dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for e in events:
        iid = _issue(e)
        if e.get("event") == "issue_opened" and iid not in opens:
            opens[iid] = e
        elif e.get("event") == "issue_resolved" and iid in opens:
            o = opens.pop(iid)
            out.append({"issue": iid, "opened": o["_t"], "resolved": e["_t"], "seed": bool(o.get("seed"))})
    out += [{"issue": iid, "opened": o["_t"], "resolved": None, "seed": bool(o.get("seed"))} for iid, o in opens.items()]
    out.sort(key=lambda ep: ep["opened"])

    next_open: dict[int, dt.datetime] = {}
    last_by_issue: dict[tuple[str, str], int] = {}
    for i, ep in enumerate(out):
        if ep["issue"] in last_by_issue:
            next_open[last_by_issue[ep["issue"]]] = ep["opened"]
        last_by_issue[ep["issue"]] = i
    fixes = [e for e in events if e.get("event") == "remediation" and e.get("result") == "fixed"]
    for i, ep in enumerate(out):
        end = next_open.get(i, FOREVER)
        ep["secs"] = (ep["resolved"] - ep["opened"]).total_seconds() if ep["resolved"] else None
        ep["fixed"] = any(_issue(r) == ep["issue"] and ep["opened"] <= r["_t"] < end for r in fixes)
    return out


def summarize(events: list[dict[str, Any]], since: dt.timedelta | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    cut = now - since if since else None

    def in_window(t: dt.datetime) -> bool:
        return cut is None or t >= cut

    opened, resolved, by_result, by_action = Counter(), Counter(), Counter(), Counter()
    for e in events:
        if not in_window(e["_t"]):
            continue
        name = "{}:{}".format(*_issue(e))
        kind = e.get("event")
        if kind == "issue_opened" and not e.get("seed"):
            opened[name] += 1
        elif kind == "issue_resolved":
            resolved[name] += 1
        elif kind == "remediation":
            by_result[e.get("result") or "unknown"] += 1
            by_action[f"{e.get('source') or '?'}:{e.get('action') or '?'}"] += 1
    eps = episodes(events)
    done = [ep for ep in eps if ep["resolved"] and not ep["seed"] and in_window(ep["resolved"])]
    return {
        "since": cut.isoformat(timespec="seconds") if cut else None,
        "events_total": len(events),
        "opened": dict(sorted(opened.items())),
        "opened_total": sum(opened.values()),
        "resolved": dict(sorted(resolved.items())),
        "resolved_total": sum(resolved.values()),
        "still_open": sorted("{}:{}".format(*ep["issue"]) for ep in eps if not ep["resolved"]),
        "time_to_resolve": stats([ep["secs"] for ep in done]),
        "remediations": dict(sorted(by_result.items())),
        "remediations_total": sum(by_result.values()),
        "remediation_actions": dict(sorted(by_action.items())),
        "mttr_with_fix": stats([ep["secs"] for ep in done if ep["fixed"]]),
        "mttr_without_fix": stats([ep["secs"] for ep in done if not ep["fixed"]]),
    }


def fmt_duration(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s" if s % 60 else f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def to_text(r: dict[str, Any], label: str) -> str:
    lines = [f"Homelab monitor metrics ({label})", f"- events recorded: {r['events_total']}"]
    if r.get("bad_lines"):
        lines.append(f"- unparseable lines skipped: {r['bad_lines']}")
    if not r["events_total"]:
        lines.append("- no events yet (events.jsonl missing or empty)")
        return "\n".join(lines)
    lines.append(f"- issues opened: {r['opened_total']} | resolved: {r['resolved_total']}")
    for k in sorted(set(r["opened"]) | set(r["resolved"])):
        lines.append(f"  - {k}: opened {r['opened'].get(k, 0)} / resolved {r['resolved'].get(k, 0)}")
    if r["still_open"]:
        lines.append(f"- still open: {', '.join(r['still_open'])}")
    t = r["time_to_resolve"]
    lines.append(f"- time to resolve (n={t['n']}): median {fmt_duration(t['median_s'])} | p90 {fmt_duration(t['p90_s'])}")
    results = ", ".join(f"{k} {v}" for k, v in r["remediations"].items()) or "none"
    lines.append(f"- remediation events: {r['remediations_total']} ({results})")
    for k, v in r["remediation_actions"].items():
        lines.append(f"  - {k}: {v}")
    for title, key in (("with a fixed remediation", "mttr_with_fix"), ("without", "mttr_without_fix")):
        s = r[key]
        lines.append(f"- MTTR {title}: mean {fmt_duration(s['mean_s'])} | median {fmt_duration(s['median_s'])} (n={s['n']})")
    return "\n".join(lines)
