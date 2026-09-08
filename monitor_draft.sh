#!/usr/bin/env bash
# Emit an event only when something is worth knowing. A 210-pick draft would
# produce 210 notifications if every pick were reported, so this reports our own
# picks, our turn coming up, periodic progress, health transitions, and the end.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1

DRAFT=$(python3 -c "import json;print(json.load(open('config.json'))['draft_id'])")
SLOT=$(python3 -c "import json;print(json.load(open('config.json'))['draft_slot'])")
TEAMS=$(python3 -c "import json;print(json.load(open('config.json'))['teams'])")
API="https://api.sleeper.app/v1/draft/$DRAFT"

last_n=-1
last_health="up"
announced_turn=""
# Highest pick of ours already announced. Seed it from the live draft so a
# restart mid-draft does not replay every pick we have ever made as a fresh
# notification; it is 0 before the draft starts, which is what we want then.
last_mine=$(curl -s --max-time 20 "$API/picks" 2>/dev/null | python3 -c "
import json,sys
try: ps=json.load(sys.stdin)
except Exception: ps=[]
print(max([p.get('pick_no',0) for p in ps if p.get('draft_slot')==$SLOT] or [0]))
" 2>/dev/null)
[ -n "${last_mine:-}" ] || last_mine=0

# Our pick numbers in a snake draft.
ROUNDS=$(python3 -c "import json;print(json.load(open('config.json')).get('rounds') or 15)")
ours=$(python3 -c "
t=$TEAMS; s=$SLOT
print(' '.join(str((r-1)*t + (s if r%2 else t-s+1)) for r in range(1,$ROUNDS+1)))")

while true; do
  picks=$(curl -s --max-time 20 "$API/picks" 2>/dev/null)
  status=$(curl -s --max-time 20 "$API" 2>/dev/null \
           | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('status',''))" 2>/dev/null)
  n=$(printf '%s' "$picks" | python3 -c "import json,sys;print(len(json.load(sys.stdin)))" 2>/dev/null)
  [ -z "${n:-}" ] && n=$last_n

  # --- health: report only on transition, never every cycle
  health="up"
  pgrep -f 'engine/run[.]py --watch' >/dev/null || health="watcher-down"
  curl -s --max-time 6 http://127.0.0.1:9222/json/version >/dev/null 2>&1 || health="chrome-down"
  if [ "$health" != "$last_health" ]; then
    if [ "$health" = "up" ]; then
      echo "RECOVERED: automation healthy again (picks=$n)"
    else
      echo "ALERT: $health -- cron guard should revive within 2 min (picks=$n)"
    fi
    last_health="$health"
  fi

  if [ "$n" != "$last_n" ] && [ "$n" -ge 0 ] 2>/dev/null; then
    # did WE just pick?
    # Report every one of our picks newer than the last we announced, not just
    # the newest one when it happens to be the very last pick made. Polling at
    # 120s, several picks can land between cycles: at pick 162 the count went
    # 161 -> 170 in one window, our own pick was no longer the last, and the
    # announcement was silently replaced by a "progress" line.
    mine=$(printf '%s' "$picks" | python3 -c "
import json,sys
ps=json.load(sys.stdin)
for p in ps:
    if p.get('draft_slot')==$SLOT and p.get('pick_no',0) > $last_mine:
        m=p.get('metadata') or {}
        print(f\"{p['pick_no']}|{m.get('first_name','')} {m.get('last_name','')}|{m.get('position','')}|{m.get('team','')}\")
" 2>/dev/null)
    announced=0
    if [ -n "$mine" ]; then
      while IFS='|' read -r pno pname ppos ptm; do
        [ -n "$pno" ] || continue
        echo "WE PICKED at $pno: $pname ($ppos $ptm)"
        last_mine=$pno
        announced=1
      done <<< "$mine"
    fi
    if [ "$announced" = "0" ] && [ $((n % 10)) -eq 0 ]; then
      echo "progress: $n picks made"
    fi
    last_n=$n
  fi

  # --- are we on the clock, or one away?
  nxt=$((n + 1))
  for p in $ours; do
    if [ "$p" = "$nxt" ] && [ "$announced_turn" != "$nxt" ]; then
      echo "ON THE CLOCK: pick $nxt is ours -- watcher is armed and will submit"
      announced_turn="$nxt"
    elif [ "$p" = "$((nxt + 1))" ] && [ "$announced_turn" != "near$p" ]; then
      echo "one away: pick $p is ours, $nxt on the clock now"
      announced_turn="near$p"
    fi
  done

  if [ "$status" = "complete" ]; then
    echo "DRAFT COMPLETE: $n picks. Roster is set."
    break
  fi
  sleep 120
done
