#!/bin/sh
# Wake an LLM agent only when the issue set changed.
# Usage: AGENT_CMD='your-agent --prompt-file RUNBOOK.md --stdin' ./examples/wake-agent.sh
set -eu
CONFIG="${CONFIG:-config.yaml}"
DIFF="$(homelab-monitor -c "$CONFIG" gate)"
[ -z "$DIFF" ] && exit 0            # nothing changed -> no model call, no cost

if [ -z "${AGENT_CMD:-}" ]; then
  printf 'AGENT_CMD not set; would wake agent with:\n%s\n' "$DIFF"
  exit 0
fi
printf '%s\n' "$DIFF" | sh -c "$AGENT_CMD"
