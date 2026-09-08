#!/usr/bin/env bash
# Keep the Chrome that writes to Sleeper alive.
#
# Sleeper has no write API, so setting a lineup means driving the real web app
# over the DevTools protocol (engine/setlineup.py). That needs a logged-in
# Chrome with --remote-debugging-port up at all times, not just when someone is
# at the desk.
#
# Chrome is a GUI application and cron runs with an empty environment, so
# without DISPLAY it simply refuses to start. That is why the 08:00 pre-draft
# job failed silently and left no queue loaded an hour before the real draft.
# Discover a live X display rather than hardcoding one, since the number
# changes across reboots.
set -uo pipefail
PROFILE="$HOME/.statking-chrome"
LOG="$HOME/statking/logs/chrome.log"
mkdir -p "$HOME/statking/logs"

LEAGUE=$(python3 -c "import json;print(json.load(open('$HOME/statking/config.json'))['league_id'])" 2>/dev/null)
TEAM_URL="https://sleeper.com/leagues/$LEAGUE/team"

# The draft room check that used to live here is gone with the draft. In-season
# the only requirement is that Chrome is up and logged in; setlineup.py
# navigates to whatever page it needs.

if curl -s --max-time 4 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  exit 0
fi

# Which X display to use is guesswork from cron, and the first guess is not
# always right: after a reboot the socket scan picked a stale :1024 and the
# script gave up on it, leaving the agent unable to write a lineup until the
# next tick happened to find a live browser to copy DISPLAY from. So collect
# every plausible display and actually try them, rather than committing to one.
candidates=""
add_candidate() {
  case " $candidates " in *" $1 "*) return 0;; esac
  candidates="$candidates $1"
}
[ -n "${DISPLAY:-}" ] && add_candidate "$DISPLAY"
for pid in $(pgrep -u "$(id -u)" -f 'chrome|firefox|gnome-session|xfce4-session' 2>/dev/null); do
  [ -r "/proc/$pid/environ" ] || continue    # the process may have exited already
  d=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^DISPLAY=//p' | head -1)
  [ -n "$d" ] && add_candidate "$d"
done
for sock in /tmp/.X11-unix/X*; do
  [ -e "$sock" ] || continue
  add_candidate ":${sock##*/X}"
done
add_candidate ":0"

if [ -z "$(echo "$candidates" | tr -d ' ')" ]; then
  echo "[$(date '+%F %T')] NO X DISPLAY FOUND - cannot start Chrome" >> "$LOG"
  exit 1
fi

if [ -z "${XAUTHORITY:-}" ] && [ -f "$HOME/.Xauthority" ]; then
  export XAUTHORITY="$HOME/.Xauthority"
fi

for d in $candidates; do
  export DISPLAY="$d"
  nohup google-chrome --remote-debugging-port=9222 --user-data-dir="$PROFILE" \
    --no-first-run --no-default-browser-check --window-size=1500,950 \
    "$TEAM_URL" >> "$LOG" 2>&1 &
  pid=$!
  for i in $(seq 1 10); do
    sleep 2
    if curl -s --max-time 3 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
      echo "[$(date '+%F %T')] chrome restarted on DISPLAY=$DISPLAY" >> "$LOG"
      exit 0
    fi
  done
  # This display did not work. Do not leave the attempt lying around competing
  # for the profile lock with the next one.
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  echo "[$(date '+%F %T')] chrome did not come up on DISPLAY=$d - trying next" >> "$LOG"
done
echo "[$(date '+%F %T')] chrome FAILED on every display:$candidates" >> "$LOG"
exit 1
