#!/usr/bin/env python3
"""Auto-submit a draft pick into the Sleeper draft room via Chrome.

Sleeper has no write API, so a pick can only be made by driving the real
React draft room. This module attaches to a Chrome tab over the DevTools
protocol (see engine/cdp.py), filters the player list, clicks the draft
button on the one matching row, and then confirms the result against
Sleeper's public picks API - which is authoritative and cannot be fooled by
a stale or mis-rendered DOM.

Setup (once, and Chrome must be started this way):

    google-chrome --remote-debugging-port=9222 \
                  --user-data-dir=$HOME/.statking-chrome

then log into Sleeper in that window and open the draft room. Chrome 136+
refuses a debugging port on the default profile, so the separate profile is
required, not optional.

CLI:
    python3 engine/submit.py --check            page + draft state, no action
    python3 engine/submit.py --dry-run          resolve the engine's pick, stop
                                                short of clicking
    python3 engine/submit.py --submit           actually make the pick

Every path is bound to an expected pick number. If the draft has moved on -
because someone else picked, or because Sleeper's own timer autopicked for
us - the submit aborts instead of drafting into the wrong slot. That failure
is not hypothetical: it happened during testing.
"""
import sys, os, re, json, time, argparse, subprocess, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp
import safety
import sync as SY
from value import build_board, load_config
import draft as D

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(HERE, "logs", "submit_log.txt")

SEARCH_SETTLE_SEC = 1.2     # Sleeper debounces the player search
VERIFY_TIMEOUT_SEC = 20     # how long to wait for the pick to appear in the API
VERIFY_POLL_SEC = 1.5


class SubmitError(Exception):
    """Anything that means we must not click, or that the click went wrong."""


# ------------------------------------------------------------------ logging
def log(line):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"[{stamp}] {line}\n")


def notify(title, msg):
    for cmd in (["notify-send", "-u", "critical", title, msg],
                ["zenity", "--info", "--text", f"{title}\n{msg}"]):
        try:
            subprocess.run(cmd, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            continue


# ------------------------------------------------------------------ helpers
def norm(s):
    """Compare names ignoring case, punctuation, and suffixes."""
    s = re.sub(r"[^a-z]", "", (s or "").lower())
    for suf in ("jr", "sr", "ii", "iii", "iv", "v"):
        if s.endswith(suf) and len(s) > len(suf) + 3:
            s = s[: -len(suf)]
    return s


_DEF_NAMES = None


def def_display_name(team):
    """Sleeper shows defenses as 'Baltimore Ravens', not 'BAL DEF'."""
    global _DEF_NAMES
    if _DEF_NAMES is None:
        _DEF_NAMES = {}
        try:
            players = json.load(open(os.path.join(HERE, "data", "players_nfl.json")))
            for pid, p in players.items():
                if (p or {}).get("position") == "DEF":
                    nm = f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip()
                    if nm:
                        _DEF_NAMES[p.get("team") or pid] = nm
        except Exception as e:
            log(f"could not load DEF names: {e}")
    return _DEF_NAMES.get(team)


def display_name(player):
    if player["pos"] == "DEF":
        return def_display_name(player.get("team")) or player["name"]
    return player["name"]


# ------------------------------------------------------------ injected page JS
JS_STATE = r"""
(() => {
  const inp = document.querySelector('.player-search input');
  const rows = [...document.querySelectorAll('div.player-rank-item2')];
  return JSON.stringify({
    href_draft_id: (location.pathname.match(/(\d{10,})/) || [])[1] || null,
    is_draft_room: /\/draft\/nfl\/\d+/.test(location.pathname),
    has_search: !!inp,
    search_value: inp ? inp.value : null,
    row_count: rows.length,
    enabled_draft_buttons: rows.filter(r => {
      const b = r.querySelector('.draft-button');
      return b && !b.classList.contains('disable');
    }).length
  });
})()
"""

JS_SET_SEARCH = r"""
(() => {
  const inp = document.querySelector('.player-search input');
  if (!inp) return JSON.stringify({ ok: false, why: 'no search input' });
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
  setter.call(inp, %s);
  inp.dispatchEvent(new Event('input', { bubbles: true }));
  return JSON.stringify({ ok: true, value: inp.value });
})()
"""

# Rows are read and clicked in ONE evaluation so nothing can re-render in
# between and shift the row we matched onto a different player.
JS_MATCH_AND_CLICK = r"""
(() => {
  const want = %s;                       // {name, pos, team, click}
  const norm = s => (s || '').toLowerCase().replace(/[^a-z]/g, '');
  const rows = [...document.querySelectorAll('div.player-rank-item2')];
  const seen = [], hits = [];
  for (const r of rows) {
    const nw = r.querySelector('.name-wrapper');
    const nameNode = nw && nw.childNodes[0];
    const name = nameNode ? String(nameNode.nodeValue || '').trim() : '';
    const metaEl = r.querySelector('.name-wrapper .position');
    const parts = (metaEl ? metaEl.innerText : '').split('\n').map(s => s.trim());
    const pos = parts[0] || '', team = parts[1] || '', inj = parts[2] || '';
    const btn = r.querySelector('.draft-button');
    const rec = { name, pos, team, inj,
                  enabled: !!btn && !btn.classList.contains('disable') };
    seen.push(rec);
    const posOk = pos === want.pos;
    const teamOk = !want.team || team === want.team;
    // Defenses render as 'Baltimore Ravens'; position+team identifies them.
    const nameOk = want.pos === 'DEF' ? true : norm(name) === norm(want.name);
    if (posOk && teamOk && nameOk) hits.push({ rec, btn });
  }
  if (hits.length !== 1) {
    return JSON.stringify({ ok: false, why: 'expected exactly 1 match, got ' +
                            hits.length, matches: hits.map(h => h.rec),
                            rows_seen: seen.slice(0, 25), row_count: rows.length });
  }
  const hit = hits[0];
  if (!hit.rec.enabled) {
    return JSON.stringify({ ok: false, why: 'draft button disabled - not on the clock',
                            match: hit.rec });
  }
  if (!want.click) {
    return JSON.stringify({ ok: true, clicked: false, match: hit.rec });
  }
  hit.btn.click();
  return JSON.stringify({ ok: true, clicked: true, match: hit.rec });
})()
"""


# ------------------------------------------------------------------- session
class DraftRoom:
    def __init__(self, draft_id, port=cdp.DEFAULT_PORT, host="127.0.0.1"):
        self.draft_id = str(draft_id)
        self.page, self.tab = cdp.attach(f"/draft/nfl/{self.draft_id}", port, host)
        # Never leave a commissioner control clickable in a room we automate.
        safety.guard(self.page)

    def state(self):
        st = self.page.evaluate(JS_STATE)
        if not isinstance(st, dict):
            raise SubmitError(f"unreadable page state: {st!r}")
        return st

    def assert_room(self):
        st = self.state()
        if not st.get("is_draft_room"):
            raise SubmitError("attached tab is not a draft room")
        if st.get("href_draft_id") != self.draft_id:
            raise SubmitError(
                f"tab is on draft {st.get('href_draft_id')}, expected {self.draft_id}")
        if not st.get("has_search"):
            raise SubmitError("player search box not found - draft room not loaded")
        return st

    def search(self, query):
        res = self.page.evaluate(JS_SET_SEARCH % json.dumps(query))
        if not (isinstance(res, dict) and res.get("ok")):
            raise SubmitError(f"could not set search box: {res}")
        time.sleep(SEARCH_SETTLE_SEC)

    def match_and_click(self, player, click):
        want = {"name": display_name(player), "pos": player["pos"],
                "team": player.get("team") or "", "click": bool(click)}
        return self.page.evaluate(JS_MATCH_AND_CLICK % json.dumps(want))

    def clear_search(self):
        try:
            self.page.evaluate(JS_SET_SEARCH % json.dumps(""))
        except Exception:
            pass

    def close(self):
        self.page.close()


# ------------------------------------------------------------------ the pick
def picks_count(draft_id):
    return len(SY.picks_made(draft_id) or [])


def verify_pick(draft_id, expected_pick_no, expected_pid, user_id,
                timeout=VERIFY_TIMEOUT_SEC):
    """Confirm against Sleeper what was actually drafted. Authoritative."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        for pk in SY.picks_made(draft_id) or []:
            if pk.get("pick_no") == expected_pick_no:
                last = pk
                got_pid = str(pk.get("player_id"))
                m = pk.get("metadata") or {}
                who = f"{m.get('first_name','')} {m.get('last_name','')}".strip()
                if got_pid != str(expected_pid):
                    return False, f"pick {expected_pick_no} went to {who} " \
                                  f"(id {got_pid}), not the player we clicked"
                if user_id and pk.get("picked_by") and pk["picked_by"] != user_id:
                    return False, f"pick {expected_pick_no} is credited to " \
                                  f"{pk['picked_by']}, not us"
                return True, f"confirmed: pick {expected_pick_no} = {who}"
        time.sleep(VERIFY_POLL_SEC)
    return False, (f"pick {expected_pick_no} never appeared in the Sleeper API "
                   f"within {timeout}s (last seen: {last})")


def submit(player, expected_pick_no, cfg=None, room=None, dry_run=True,
           port=cdp.DEFAULT_PORT):
    """Draft `player` at `expected_pick_no`. Returns a result dict.

    Refuses to click unless the draft room, the pick number, and the matched
    row all agree. `dry_run=True` does every check and stops before clicking.
    """
    cfg = cfg or load_config()
    draft_id = str(cfg["draft_id"])
    user_id = cfg.get("user_id")
    own_room = room is None
    if own_room:
        room = DraftRoom(draft_id, port=port)
    out = {"player": player["name"], "pos": player["pos"],
           "pick_no": expected_pick_no, "dry_run": dry_run}
    try:
        room.assert_room()

        # The draft must be sitting exactly on the pick we were told to make.
        n = picks_count(draft_id)
        if n + 1 != expected_pick_no:
            raise SubmitError(
                f"draft is on pick {n + 1}, not {expected_pick_no} - aborting "
                f"rather than drafting into the wrong slot")

        room.search(display_name(player))
        res = room.match_and_click(player, click=False)
        if not (isinstance(res, dict) and res.get("ok")):
            raise SubmitError(f"cannot identify the row: {res.get('why')} "
                              f"| {json.dumps(res)[:400]}")

        if dry_run:
            room.clear_search()
            out.update(ok=True, clicked=False, matched=res.get("match"),
                       detail="dry run - all checks passed, did not click")
            log(f"DRY RUN pick {expected_pick_no}: would take {player['name']} "
                f"({player['pos']}) - matched {res.get('match')}")
            return out

        # Re-check immediately before clicking: the timer may have autopicked
        # for us in the seconds the search took to settle.
        n2 = picks_count(draft_id)
        if n2 + 1 != expected_pick_no:
            raise SubmitError(
                f"draft moved to pick {n2 + 1} while we were searching - "
                f"aborting")

        res = room.match_and_click(player, click=True)
        if not (isinstance(res, dict) and res.get("ok") and res.get("clicked")):
            raise SubmitError(f"click did not go through: {json.dumps(res)[:400]}")

        ok, detail = verify_pick(draft_id, expected_pick_no, player["pid"], user_id)
        room.clear_search()
        out.update(ok=ok, clicked=True, matched=res.get("match"), detail=detail)
        if ok:
            log(f"SUBMITTED pick {expected_pick_no}: {player['name']} "
                f"({player['pos']}) - {detail}")
        else:
            log(f"SUBMIT FAILED pick {expected_pick_no}: {player['name']} - {detail}")
            notify("DRAFT SUBMIT FAILED", detail)
        return out
    except (SubmitError, cdp.CDPError) as e:
        room.clear_search()
        out.update(ok=False, clicked=False, detail=str(e))
        log(f"ABORT pick {expected_pick_no} ({player['name']}): {e}")
        if not dry_run:
            notify("DRAFT SUBMIT ABORTED", str(e))
        return out
    finally:
        if own_room:
            room.close()


# ----------------------------------------------------------------------- cli
def _engine_pick(draft_id=None):
    """Ask the engine what to take, and at which pick number.

    `draft_id` overrides config.json so a mock draft can be rehearsed against
    without reading - let alone touching - the real league draft.
    """
    cfg = load_config()
    if draft_id:
        cfg["draft_id"] = str(draft_id)
    board, _, _ = build_board(cfg)
    st = D.DraftState(cfg, board)
    st.my_slot = cfg.get("draft_slot")
    picks = SY.picks_made(cfg["draft_id"]) or []
    for pk in picks:
        pid = pk.get("player_id")
        if not pid:
            continue
        st.drafted[pid] = pk.get("draft_slot")
        if pk.get("draft_slot") == st.my_slot and pid in st.by_pid:
            st.my_roster.append(st.by_pid[pid])
    n = len(picks)
    cur, _ = st.next_two_picks(n)
    cands, meta = D.recommend(st, n, top_n=1)
    if not cands:
        raise SubmitError("engine returned no legal candidate")
    return cfg, cands[0]["player"], cur, n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--open", action="store_true",
                   help="navigate the debugging Chrome to the draft room")
    g.add_argument("--check", action="store_true",
                   help="report page + draft state, take no action")
    g.add_argument("--dry-run", action="store_true",
                   help="resolve the pick and run every check, but do not click")
    g.add_argument("--submit", action="store_true",
                   help="actually make the pick")
    ap.add_argument("--port", type=int, default=cdp.DEFAULT_PORT)
    ap.add_argument("--player", help="override the engine and target this name")
    ap.add_argument("--draft-id", help="override config.json (use for mock drafts)")
    a = ap.parse_args()

    cfg = load_config()
    if a.draft_id:
        cfg["draft_id"] = a.draft_id

    if a.open:
        url = f"https://sleeper.com/draft/nfl/{cfg['draft_id']}"
        cdp.open_url(url, port=a.port,
                     ready_js="JSON.stringify(!!document.querySelector"
                              "('.player-search input'))")
        print(f"opened {url}")
        room = DraftRoom(cfg["draft_id"], port=a.port)
        try:
            st = room.assert_room()
            print(f"  draft room loaded, search present, rows {st['row_count']}")
        finally:
            room.close()
        return

    if a.check:
        room = DraftRoom(cfg["draft_id"], port=a.port)
        try:
            st = room.assert_room()
            n = picks_count(cfg["draft_id"])
            print(f"tab      : {room.tab.get('title')}")
            print(f"draft    : {cfg['draft_id']}  picks made {n}  next pick {n + 1}")
            print(f"search   : present, value={st['search_value']!r}")
            print(f"rows     : {st['row_count']}  "
                  f"enabled draft buttons: {st['enabled_draft_buttons']}")
            print(f"on clock : {'YES' if st['enabled_draft_buttons'] else 'no'}")
        finally:
            room.close()
        return

    cfg2, player, pick_no, n = _engine_pick(a.draft_id)
    if a.draft_id:
        # A mock has its own slot order; the room's enabled draft button is the
        # real on-the-clock check, so rehearse against the next pick up.
        pick_no = n + 1
    if a.player:
        board, _, _ = build_board(cfg2)
        want = [p for p in board if norm(p["name"]) == norm(a.player)]
        if len(want) != 1:
            print(f"'{a.player}' matched {len(want)} players on the board")
            sys.exit(2)
        player = want[0]

    res = submit(player, pick_no, cfg=cfg2, dry_run=not a.submit, port=a.port)
    print(json.dumps(res, indent=2))
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
