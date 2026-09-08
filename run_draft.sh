#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p logs
exec python3 -u engine/run.py --watch --auto >> logs/draftday.log 2>&1
