# Agent adapter

The monitor never calls an LLM. It exposes three stable CLI contracts that any
scheduler + agent combination can consume:

| Contract | Command | Output |
|---|---|---|
| Gate | `homelab-monitor gate` | empty stdout = nothing changed; otherwise a diff (`+`/`-`/`=` lines, tab-separated `host key text`) |
| Gate (machine) | `homelab-monitor gate --json` | `{"changed": bool, "hash", "previous_hash", "current", "added", "removed"}` |
| Claim | `homelab-monitor claim host:key "action"` | exit 0 = may act once; exit 3 = already attempted, ask the owner |
| Report | `homelab-monitor report host "text"` | sends an owner-mention message via the configured notifiers |

The gate records the new hash when it reports a change, so each change is
delivered exactly once. Use `--peek` to inspect without consuming.

## Pattern A: plain cron + any CLI agent

```cron
*/5 * * * *  cd /opt/monitor && homelab-monitor check all
*/5 * * * *  cd /opt/monitor && sleep 30 && ./examples/wake-agent.sh
```

`examples/wake-agent.sh` runs the gate and, only on non-empty output, starts the
agent with `RUNBOOK.md` as instructions and the diff as input. Substitute your
agent command (Claude Code, Codex, OpenCode, an in-house script, …).

## Pattern B: agent framework with a "script gate" cron

Frameworks such as Hermes Agent let a scheduled job run a script first and wake
the model only if the script prints something. Point that pre-check at
`homelab-monitor gate`; the model then receives the diff as context and the
runbook as the job prompt. Nothing in this repo depends on a specific framework.

## Pattern C: no LLM at all

Replace the agent with a deterministic script that maps known keys to fixes
(e.g. `svc:*` → `systemctl restart`), still calling `claim` before acting. The
gate/claim/report contracts are identical.

## Cost

With checks every 5 minutes there are 288 checks/day per host, all free. The
agent runs only when the issue *set* changes — in a quiet week that is zero
times; a typical incident costs two wakes (appear + recover; the recover wake
is usually a no-op).
