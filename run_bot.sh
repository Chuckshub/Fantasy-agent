#!/usr/bin/env bash
# Discord command listener for #NFL-Fantasy-2026.
# -u because a buffered stdout makes the log useless for diagnosing a bot that
# looks dead but is merely quiet.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
# The analytics modules (DuckDB, numpy, matplotlib) live in the project venv,
# and this box has no system pip to install them into python3. So prefer the
# venv interpreter and fall back to python3 only if it is missing, which keeps
# every entry point on one interpreter rather than discovering the split at
# 09:30 on a Sunday.
PY="$(dirname "$(readlink -f "$0")")/.venv/bin/python"
[ -x "$PY" ] || PY=python3

mkdir -p logs
exec "$PY" -u engine/bot.py >> logs/bot.log 2>&1
