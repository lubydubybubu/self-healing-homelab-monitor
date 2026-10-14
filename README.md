# self-healing-homelab-monitor

English | [한국어](README.ko.md)

Deterministic homelab health checks that alert **only when something changes**,
plus an **issue-set gate** that wakes an LLM remediation agent **only when the
set of problems changes**. The agent follows a written runbook with explicit
*allowed* and *ask-first* actions and may attempt each fix at most once.

> This repository is a sanitized, generalized version of a system I run on my
> own homelab (a Proxmox server with several VMs, a Windows desktop and a
> laptop). Hostnames, addresses, users, keys, VM IDs and channel IDs have been
> replaced by a config file with placeholder values.

## Problem

Homelab monitoring tends to fail in one of two ways:

- **Too noisy.** A check every 5 minutes that posts every result trains you to
  ignore the channel.
- **Too expensive / too risky to automate.** Putting an LLM agent in the loop on
  every check burns tokens 288 times a day per host, and an agent with shell
  access and no rules will eventually "fix" something by rebooting it.

The design goal: checks are free and boring, notifications are rare and
meaningful, and the agent is woken only for new situations, with a narrow,
written mandate.

## Architecture

```mermaid
flowchart LR
    subgraph sched["Scheduler (cron / any job runner), every 5 min"]
        C["homelab-monitor check &lt;host&gt;<br/>deterministic, no LLM"]
        G["homelab-monitor gate<br/>issue-set hash"]
    end
    H1[(Hypervisor<br/>VMs, disks)]
    H2[(App VM<br/>systemd, HTTP)]
    H3[(Desktop<br/>tailnet, SSH)]
    S[("state/&lt;host&gt;.json")]
    N["Notifiers<br/>stdout · Discord webhook"]
    A["LLM agent<br/>+ RUNBOOK.md"]
    L[("ledger.json<br/>at-most-once")]

    C -- SSH / TCP / HTTP probes --> H1 & H2 & H3
    C -- diff vs previous --> S
    C -- only on change --> N
    S --> G
    G -- "only if set changed:<br/>+/-/= diff" --> A
    A -- claim host:key --> L
    A -- allowed fix --> H1 & H2
    A -- "report: cause / action / result<br/>(owner mention)" --> N
```

### Flow: check → diff → notify → gate → agent → report

1. **Check.** `homelab-monitor check <host>` runs the host's configured checks
   (`tailscale_online`, `ssh_reachable`, `tcp`, `http`, `systemd_units`,
   `proxmox_vms`, `disk_usage`, `command`). Each yields a set of conditions
   keyed like `svc:web-app` or `vm:101`. Probe failures that mean "I don't
   know" (e.g. the tailnet CLI itself failed) return *unknown* and never create
   or clear conditions.
2. **Diff.** The condition set is compared with the host's state file:
   - first run = silent baseline;
   - a new condition must appear on 2 consecutive runs (anti-flap) unless it is
     marked `immediate` (e.g. hypervisor unreachable);
   - a reported condition that disappears yields one "recovered" event;
   - text changes (91% → 92%) are not new events;
   - in quiet hours non-urgent events are held and flushed in the morning.
   State is written atomically (temp file + rename).
3. **Notify.** Notifiers are called only when there are events. Urgent events
   mention the owner. The Discord webhook URL and owner ID come from
   environment variables; `allowed_mentions` blocks `@everyone`/role pings.
4. **Gate.** `homelab-monitor gate` reads all state files, builds the set of
   `host:key` identities and hashes it (SHA-256 over sorted canonical JSON).
   If the hash equals the last one, output is empty and nothing else happens.
   Otherwise it prints a `+`/`-`/`=` diff and records the new hash, so each
   change is delivered once.
5. **Agent.** Whatever runs on a non-empty gate (an LLM agent, or a plain
   script) gets the diff as input and [`RUNBOOK.example.md`](RUNBOOK.example.md)
   as instructions. Before acting it must `homelab-monitor claim host:key`;
   a second claim within 24h exits with code 3 → ask the owner instead.
6. **Report.** `homelab-monitor report <host> "Cause / Action / Result"` sends
   one owner-mention message per handled issue.

See [`docs/agent-adapter.md`](docs/agent-adapter.md) for how to wire this to
cron + any CLI agent, to an agent framework's scheduled job, or to no LLM at
all.

## Safety model

- **Detection is deterministic.** No model decides whether something is broken.
- **The agent's mandate is a whitelist.** Allowed without asking: restart a
  configured service once, start a stopped configured VM, kill clearly runaway
  processes, clean regenerable caches, send Wake-on-LAN during daytime.
  Everything else (reboots, config changes, deletion beyond caches, hardware
  warnings, anything repeating) is **ask first**.
- **At most once per issue, mechanically.** The ledger makes "don't retry a
  failed fix" a property of the tooling, not just of the prompt.
- **Secrets stay out of config and output.** Webhook URLs and user IDs come
  from env vars; SSH uses `BatchMode=yes` and `StrictHostKeyChecking=yes`.
- **Unknown is not healthy and not broken.** Probe failures don't generate
  false recoveries.
- CI runs gitleaks on every push.

## Cost model

| Component | Frequency | Cost |
|---|---|---|
| Checks | every 5 min per host (288/day) | CPU + a few SSH/HTTP calls; no tokens |
| Notifications | on state change only | free (webhook) |
| Gate | every 5 min | reads a few JSON files |
| LLM agent | only when the issue **set** changes | typically 0 runs on a quiet day; ~2 per incident (appear, recover) |

Alert text changing (disk 91% → 92%) does not wake the agent, because the
gate hashes issue identity, not wording.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                          # 56 tests, all with fakes (no SSH/network)
python demo/local_demo.py          # docker-free walkthrough on 127.0.0.1

cp config.example.yaml config.yaml          # edit hosts/checks
cp RUNBOOK.example.md RUNBOOK.md            # edit allowed actions
homelab-monitor check all --dry-run
homelab-monitor gate --peek
```

Docker demo (fake target container + monitor in dry-run mode):

```bash
docker compose up --build
docker compose stop target    # problem appears after 2 cycles, gate prints a diff
docker compose start target   # recovered
```

Cron example:

```cron
*/5 * * * *  cd /opt/monitor && .venv/bin/homelab-monitor check all
*/5 * * * *  cd /opt/monitor && sleep 30 && PATH=.venv/bin:$PATH ./examples/wake-agent.sh
```

## Repository layout

```
homelab_monitor/   config.py  checks.py  state.py  gate.py  notify.py  ledger.py  cli.py
tests/             config, state diffing, gate hashing, notify, checks/CLI (fakes only)
demo/              docker + local demo configs, local_demo.py
docs/              agent-adapter.md
examples/          wake-agent.sh
RUNBOOK.example.md config.example.yaml docker-compose.yml Dockerfile
```

## What's in the real deployment vs. this repo

| | Real deployment (private) | This repo |
|---|---|---|
| Hosts | Proxmox host + VMs, Windows desktop via SSH/PowerShell, laptop via tailnet presence | Generic check types driven by YAML |
| Windows metrics | CPU/memory/disk via encoded PowerShell over SSH | Not included (use `command` check via SSH) |
| Proxmox extras | storage pools, SMART health, guest-agent service checks inside VMs, memory/IO pressure of the agent VM | `proxmox_vms`, `disk_usage`, `systemd_units`; the rest is left out |
| Notifications | Discord bot: one live status embed per host channel (edited in place) + owner-mention messages, in Korean | Discord webhook + stdout, English, plain messages |
| Scheduler/agent | Hermes Agent cron jobs (script-only, no LLM) + a gated agent job | Framework-agnostic CLI contracts |
| VM auto-start | Done by the monitor itself with a 30-minute cooldown | Moved to the agent behind `claim` |
| At-most-once | Runbook instruction + cooldown | Same rule, enforced by a ledger |
| Tests / CI / demo | None (personal script) | 56 pytest tests, ruff, gitleaks, docker + local demo |

## Limitations

- Single-node scheduler; no HA, no metrics history or dashboards. For that, use
  Prometheus/Alertmanager, Uptime Kuma, Netdata, etc. This project is about the
  *gate + runbook-driven remediation* pattern, not about replacing them.
- The ledger and state files assume one writer at a time (cron runs don't overlap).
- The runbook is enforced by the agent's instructions plus the claim ledger;
  the tooling does not sandbox the agent's shell. Run the agent as a
  least-privilege user with sudo rules scoped to the allowed actions.
- The Docker demo and CI docker job have not been executed in the environment
  this draft was prepared in (no Docker available there); unit tests and the
  local demo have.

## Role and how it was built

I designed this system, run it on my own homelab, and define its operating
rules (what may be automated, what must be asked). The code — both the private
original and this public generalization — was implemented with AI coding
agents under my direction and reviewed by me. The private original runs on my
homelab; this public version is verified by its test suite and the local demo,
not by running it against my real hosts.

## License

MIT © lUbY
