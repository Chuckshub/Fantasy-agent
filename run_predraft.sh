#!/usr/bin/env bash
# Load the safety queue and enable autopick on the real draft. Nothing else.
#
# Scope is deliberately narrow. It does NOT start the draft, does NOT touch
# league or draft settings, and does NOT make a pick. engine/safety.py strips
# every commissioner control out of the page before any interaction, because
# If you are a co-commissioner, START DRAFT is live in that DOM - so a
# mistyped selector physically cannot reach it.
#
#   usage: run_predraft.sh [draft_id]      (draft_id only for rehearsal)
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p logs
DID_ARG=""
[ $# -ge 1 ] && DID_ARG="--draft-id $1"

{
  echo "===== $(date '+%F %T') pre-draft setup ${1:-REAL} ====="
  ./run_chrome.sh; sleep 6

  echo "-- opening the draft room"
  timeout 180 python3 -u engine/submit.py --open $DID_ARG || { echo "OPEN FAILED"; exit 1; }

  echo "-- loading the queue"
  timeout 900 python3 -u engine/queue_sync.py --apply $DID_ARG --trials 30
  QRC=$?

  echo "-- enabling autopick"
  timeout 180 python3 -u engine/queue_sync.py --autopick on $DID_ARG
  ARC=$?

  echo "-- reading back what Sleeper holds"
  timeout 180 python3 -u engine/queue_sync.py --show $DID_ARG

  echo "queue_exit=$QRC autopick_exit=$ARC"
  timeout 120 python3 -u engine/notify_predraft.py "$QRC" "$ARC" || true
} >> logs/predraft.log 2>&1
