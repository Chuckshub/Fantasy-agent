#!/usr/bin/env bash
# Autonomous operator entry point.  usage: run_agent.sh cycle|pregame
# -u so a buffered stdout cannot make a live agent look dead.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1
# The analytics modules (DuckDB, numpy, matplotlib) live in the project venv,
# and this box has no system pip to install them into python3. So prefer the
# venv interpreter and fall back to python3 only if it is missing, which keeps
# every entry point on one interpreter rather than discovering the split at
# 09:30 on a Sunday.
PY="$(dirname "$(readlink -f "$0")")/.venv/bin/python"
[ -x "$PY" ] || PY=python3

MODE="${1:-cycle}"
mkdir -p logs
{
  echo "===== $(date '+%F %T') mode=$MODE ====="
  timeout 1500 "$PY" -u engine/agent.py --"$MODE"
  echo "exit=$?"
  # The cycle owns the store; commit whatever it changed so history survives.
  if [ "$MODE" = "cycle" ] && [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    git add -A
    git commit -q -m "agent cycle $(date '+%F %H:%M')" \
      -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" \
      && git push -q origin main 2>/dev/null
    echo "committed."
  fi
} >> logs/agent_runner.log 2>&1
