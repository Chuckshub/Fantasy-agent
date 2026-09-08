#!/usr/bin/env bash
# Restart the draft watcher if it has died. A 14-team, 15-round draft at two
# hours a pick can run for days - far longer than any interactive session - so
# nothing here may depend on a human being present.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1
pgrep -f 'engine/run[.]py --watch' >/dev/null && exit 0
# Only revive it while the draft is actually live.
S=$(curl -s --max-time 10 "https://api.sleeper.app/v1/draft/$(python3 -c "import json;print(json.load(open('config.json'))['draft_id'])")" \
    | python3 -c "import json,sys; print((json.load(sys.stdin) or {}).get('status',''))" 2>/dev/null)
[ "$S" = "drafting" ] || exit 0
./run_chrome.sh
mkdir -p logs
echo "[$(date '+%F %T')] watcher was dead - restarting" >> logs/draftday.log
setsid ./run_draft.sh < /dev/null > /dev/null 2>&1 &
