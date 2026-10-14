"""Issue-set gate: decides whether the (expensive) LLM agent should wake up.

The gate reads every host state file and builds the set of currently reported
issues. Identity is ``host + key`` only - *not* the human text - so a disk going
from 91% to 92% does not wake the agent again. The set is hashed with SHA-256
over a canonical, sorted encoding, so the hash is stable across runs, dict
ordering and Python versions.

``run_gate`` compares the hash with the last one it saw (``gate.json``) and
returns a diff. Only a changed hash means "wake the agent".
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from .state import save_state


@dataclass(frozen=True, order=True)
class Issue:
    host: str
    key: str
    text: str = field(compare=False)

    @property
    def id(self) -> str:
        return f"{self.host}:{self.key}"

    def line(self) -> str:
        return f"{self.host}\t{self.key}\t{self.text}"


@dataclass
class GateResult:
    changed: bool
    hash: str
    previous_hash: str | None
    current: list[Issue]
    added: list[Issue]
    removed: list[Issue]

    def to_dict(self) -> dict:
        def dump(xs):
            return [{"host": i.host, "key": i.key, "text": i.text} for i in xs]

        return {
            "changed": self.changed,
            "hash": self.hash,
            "previous_hash": self.previous_hash,
            "current": dump(self.current),
            "added": dump(self.added),
            "removed": dump(self.removed),
        }

    def to_text(self) -> str:
        """Agent-facing diff. Empty string when nothing changed."""
        if not self.changed:
            return ""
        out = [f"# issue set {self.previous_hash or 'none'} -> {self.hash}"]
        out += [f"+\t{i.line()}" for i in self.added]
        out += [f"-\t{i.line()}" for i in self.removed]
        out += [f"=\t{i.line()}" for i in self.current if i not in self.added]
        return "\n".join(out)


def issue_set_hash(issues: Iterable[Issue]) -> str:
    ids = sorted({i.id for i in issues})
    canonical = json.dumps(ids, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


def current_issues(state_dir: pathlib.Path, hosts: Iterable[str], skip: set[tuple[str, str]] = frozenset()) -> list[Issue]:
    issues = []
    for host in hosts:
        f = state_dir / f"{host}.json"
        if not f.exists():
            continue
        st = json.loads(f.read_text(encoding="utf-8"))
        for key, val in (st.get("reported") or {}).items():
            if (host, key) in skip:
                continue
            text = val["text"] if isinstance(val, dict) else str(val)
            issues.append(Issue(host, key, text))
    return sorted(issues)


def diff(previous: dict | None, issues: list[Issue]) -> GateResult:
    prev_hash = (previous or {}).get("hash")
    prev_ids = set((previous or {}).get("ids") or [])
    h = issue_set_hash(issues)
    cur_ids = {i.id for i in issues}
    added = [i for i in issues if i.id not in prev_ids]
    removed = [Issue(*pid.split(":", 1), text=(previous or {}).get("texts", {}).get(pid, "")) for pid in sorted(prev_ids - cur_ids)]
    # First run with an empty set is "no change"; first run with issues is a change.
    changed = h != prev_hash if previous is not None else bool(issues)
    return GateResult(changed, h, prev_hash, issues, added if changed else [], removed if changed else [])


def run_gate(state_dir: pathlib.Path, hosts: Iterable[str], skip: set[tuple[str, str]] = frozenset(), *, commit: bool = True) -> GateResult:
    gate_file = state_dir / "gate.json"
    previous = json.loads(gate_file.read_text(encoding="utf-8")) if gate_file.exists() else None
    issues = current_issues(state_dir, hosts, skip)
    result = diff(previous, issues)
    if commit and (result.changed or previous is None):
        save_state(
            gate_file,
            {
                "hash": result.hash,
                "ids": sorted(i.id for i in issues),
                "texts": {i.id: i.text for i in issues},
            },
        )
    return result
