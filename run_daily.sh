#!/usr/bin/env bash
# The everyday check, for cron. Runs the daily report and commits the result so
# the store's history survives in git rather than only on disk.
#
# Installed by:  crontab -l   (remove with: crontab -e)
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1
# The analytics modules (DuckDB, numpy, matplotlib) live in the project venv,
# and this box has no system pip to install them into python3. So prefer the
# venv interpreter and fall back to python3 only if it is missing, which keeps
# every entry point on one interpreter rather than discovering the split at
# 09:30 on a Sunday.
PY="$(dirname "$(readlink -f "$0")")/.venv/bin/python"
[ -x "$PY" ] || PY=python3


LOG="logs/cron.log"
mkdir -p logs
{
  echo "===== $(date '+%F %T') ====="
  timeout 900 "$PY" engine/daily.py --quiet
  rc=$?
  echo "daily.py exit=$rc"
  # Commit whatever changed: the report, the store, refreshed derived data.
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    git add -A
    git commit -q -m "daily check $(date '+%F %H:%M')" \
      -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>" \
      && git push -q origin main 2>/dev/null
    echo "committed."
  else
    echo "nothing changed."
  fi
} >> "$LOG" 2>&1
