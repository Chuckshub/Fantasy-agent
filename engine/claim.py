#!/usr/bin/env python3
"""Add a player - waiver claim or free agent - by driving the Sleeper web app.

`waivers.py` works out who is worth claiming and what to bid. Until this module
existed, nothing submitted the claim: the agent posted a target list to Discord
and a human had to open the app. That is the same gap `setlineup.py` closed for
lineups, and it matters more here, because the lineup optimiser can only choose
among players we already own. A bye-week hole that needs a quarterback cannot be
fixed by rearranging the roster.

Sleeper has no write API. As with the draft room and the team page, the only way
in is the real React app over the DevTools protocol.

    python3 engine/claim.py --inspect "Jacoby Brissett"   open the dialog, read it, cancel
    python3 engine/claim.py --plan                        what waivers.py wants, with drops
    python3 engine/claim.py --dry-run                     resolve everything, stop before submit
    python3 engine/claim.py --submit                      actually claim

The player list is a ReactVirtualized grid - only the visible rows exist in the
DOM, and the container is 162,000px tall - so a player is reached by typing into
the search box and matching the single surviving row, exactly as `submit.py`
does in the draft room. Scrolling to find a row is not an option.

**What this will not do.** A claim costs a roster spot, so it names a player to
drop, and a wrong drop is the most expensive mistake available here - the player
is gone and someone else can take him. So the drop is chosen by the same lineup
engine that sets the lineup, and it refuses outright to drop anyone who starts
this week, anyone needed to field a legal lineup in the next few weeks, or
anyone the caller has not explicitly approved when `--player` is given by hand.
"""
import sys, os, re, json, time, argparse, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp
import lineup as LU
import waivers as WV
import sync as SY
import db as DB
import setlineup as SLU
from value import build_board, load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(HERE, "logs", "claim.log")

SEARCH_SETTLE_SEC = 1.6
DIALOG_SETTLE_SEC = 1.4
VERIFY_TIMEOUT_SEC = 30
VERIFY_POLL_SEC = 2.5

# How far ahead the drop guard looks for byes. A player who is the only startable
# body at his position in week 7 must not be dropped in week 3 for a marginal
# upgrade, which is exactly the trap a greedy weekly comparison walks into.
LOOKAHEAD_WEEKS = 6


class ClaimError(Exception):
    """Anything that means we must not submit, or that a submit went wrong."""


def log(line):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"[{stamp}] {line}\n")


def norm(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def last_name(s):
    parts = (s or "").replace(".", " ").split()
    return norm(parts[-1]) if parts else ""


# ------------------------------------------------------------ injected page JS
JS_SET_SEARCH = r"""
(() => {
  const inp = document.querySelector('.player-search input')
           || document.querySelector('input[placeholder*="Find player"]');
  if (!inp) return JSON.stringify({ok:false, why:'no search input on this page'});
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
  setter.call(inp, %s);
  inp.dispatchEvent(new Event('input', {bubbles:true}));
  return JSON.stringify({ok:true, value: inp.value});
})()
"""

# Read the filtered rows and optionally click the add button, in ONE evaluation.
# The list is virtualized and re-renders constantly; reading and clicking in two
# passes lets a different player slide under the index we matched.
JS_MATCH_AND_ADD = r"""
(() => {
  const want = %s;                      // {name, pos, team, click}
  const norm = s => (s || '').toLowerCase().replace(/[^a-z]/g, '');
  const lastName = s => {
    const p = (s || '').replace(/\./g, ' ').trim().split(/\s+/);
    return p.length ? norm(p[p.length - 1]) : '';
  };
  const rows = [...document.querySelectorAll('.player-list-item')];
  const seen = [], hits = [];
  for (const r of rows) {
    const nameEl = r.querySelector('.name-container .name');
    const posEl  = r.querySelector('.position');
    const injEl  = r.querySelector('.injury-status');
    const btn    = r.querySelector('.player-action-button.add');
    const name = nameEl ? nameEl.innerText.trim() : '';
    const meta = posEl ? posEl.innerText.trim() : '';    // "QB - ARI(14)"
    const m = meta.match(/^([A-Z]{1,3})\s*-\s*([A-Z]{2,3})/);
    const rec = { name, pos: m ? m[1] : null, team: m ? m[2] : null,
                  inj: injEl ? injEl.innerText.trim() : '',
                  addable: !!btn };
    seen.push(rec);
    const posOk  = !want.pos  || rec.pos === want.pos;
    const teamOk = !want.team || rec.team === want.team;
    // The list abbreviates first names ("J. Brissett"), so the surname plus
    // position plus team is the identity. Defenses render as a team code.
    const nameOk = want.pos === 'DEF' ? true
                 : lastName(name) === lastName(want.name);
    if (posOk && teamOk && nameOk) hits.push({rec, btn});
  }
  if (hits.length !== 1)
    return JSON.stringify({ok:false, why:'expected exactly 1 match, got ' + hits.length,
                           matches: hits.map(h => h.rec), rows_seen: seen.slice(0,15)});
  const hit = hits[0];
  if (!hit.btn)
    return JSON.stringify({ok:false, why:'no add button - already rostered?',
                           match: hit.rec});
  if (!want.click) return JSON.stringify({ok:true, clicked:false, match:hit.rec});
  hit.btn.click();
  return JSON.stringify({ok:true, clicked:true, match:hit.rec});
})()
"""

# Read whatever dialog the add button opened, without touching it.
JS_READ_DIALOG = r"""
(() => {
  const scopes = ['.modal', '[class*="modal"]', '[role="dialog"]',
                  '[class*="popup"]', '[class*="dialog"]', '[class*="overlay"]'];
  let root = null;
  for (const s of scopes) {
    for (const el of document.querySelectorAll(s)) {
      if (el.offsetParent !== null && el.innerText && el.innerText.trim().length > 10) {
        if (!root || el.contains(root) === false) root = root || el;
      }
    }
    if (root) break;
  }
  if (!root) return JSON.stringify({open:false});
  const cls = {};
  for (const el of root.querySelectorAll('*')) {
    let c = el.className;
    if (c && c.baseVal !== undefined) c = c.baseVal;
    if (typeof c === 'string') for (const x of c.split(/\s+/)) if (x) cls[x]=(cls[x]||0)+1;
  }
  return JSON.stringify({
    open: true,
    root_cls: String(root.className || ''),
    text: root.innerText.slice(0, 1200),
    classes: Object.entries(cls).sort((a,b)=>b[1]-a[1]).slice(0, 40),
    inputs: [...root.querySelectorAll('input')].map(i => ({
      cls: String(i.className||''), type: i.type, ph: i.placeholder, val: i.value })),
    buttons: [...root.querySelectorAll('button,[class*="button"],[class*="btn"]')]
      .filter(b => b.offsetParent !== null)
      .map(b => ({cls: String(b.className||''), text: (b.innerText||'').trim().slice(0,40)}))
      .slice(0, 20),
    html: root.outerHTML.slice(0, 3000)
  });
})()
"""

# Pick the player to drop inside the add dialog, and confirm the click took.
# The dialog lists the roster as .team-roster-item rows with a full (unabbreviated)
# name in .name-text, so the match here can be exact rather than by surname.
JS_SELECT_DROP = r"""
(() => {
  const want = %s;                       // {name, click}
  const norm = s => (s || '').toLowerCase().replace(/[^a-z]/g, '');
  const rows = [...document.querySelectorAll('.modal-item-underlay .team-roster-item')];
  if (!rows.length) return JSON.stringify({ok:false, why:'no roster rows in the dialog'});
  const seen = [], hits = [];
  for (const r of rows) {
    const nm = r.querySelector('.name-text');
    const name = nm ? nm.innerText.trim() : '';
    seen.push(name);
    if (name && norm(name) === norm(want.name)) hits.push(r);
  }
  if (hits.length !== 1)
    return JSON.stringify({ok:false, why:'expected exactly 1 roster row named ' +
                           want.name + ', got ' + hits.length, seen});
  if (!want.click) return JSON.stringify({ok:true, clicked:false});
  const row = hits[0];
  const before = String(row.className || '');
  row.click();
  return JSON.stringify({ok:true, clicked:true, before,
                         after: String(row.className || '')});
})()
"""

# Which row does the dialog consider selected? Sleeper marks it with a class we
# do not want to hardcode, so this reports every row's class and lets Python
# decide by comparing against the un-selected rows.
JS_DIALOG_ROWS = r"""
(() => {
  const rows = [...document.querySelectorAll('.modal-item-underlay .team-roster-item')];
  return JSON.stringify(rows.map(r => {
    const nm = r.querySelector('.name-text');
    return {name: nm ? nm.innerText.trim() : null, cls: String(r.className || '')};
  }));
})()
"""

# A waiver claim (as opposed to a free-agent add) carries a FAAB bid field. It
# is absent pre-season and present once waivers are live, so this sets it only
# if it exists and reports which case it saw.
JS_SET_BID = r"""
(() => {
  const amount = %s;
  const root = document.querySelector('.modal-item-underlay');
  if (!root) return JSON.stringify({ok:false, why:'dialog is gone'});
  const inputs = [...root.querySelectorAll('input')].filter(i => i.offsetParent !== null);
  const bid = inputs.find(i => i.type === 'number' ||
      /bid|faab|amount|budget/i.test((i.className||'') + ' ' + (i.placeholder||'')));
  if (!bid) return JSON.stringify({ok:true, has_bid:false});
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value').set;
  setter.call(bid, String(amount));
  bid.dispatchEvent(new Event('input', {bubbles:true}));
  bid.dispatchEvent(new Event('change', {bubbles:true}));
  return JSON.stringify({ok:true, has_bid:true, value: bid.value});
})()
"""

# The commit. Deliberately matched on the exact button, and it refuses to click
# anything whose text is not the add/claim action - a stray match on "Cancel"
# would be harmless but a stray match on some other confirm would not.
JS_SUBMIT = r"""
(() => {
  const root = document.querySelector('.modal-item-underlay');
  if (!root) return JSON.stringify({ok:false, why:'dialog is gone'});
  const btns = [...root.querySelectorAll('button,[class*="button"],[class*="btn"]')]
               .filter(b => b.offsetParent !== null);
  const want = /^(add player|claim player|place claim|submit claim|add|claim)$/i;
  const hits = btns.filter(b => want.test((b.innerText || '').trim()));
  if (!hits.length)
    return JSON.stringify({ok:false, why:'no add/claim button',
                           saw: btns.map(b => (b.innerText||'').trim()).slice(0,10)});
  const btn = hits[hits.length - 1];      // innermost matching element
  if (/disabled/i.test(String(btn.className || '')) || btn.disabled)
    return JSON.stringify({ok:false, why:'the add button is disabled'});
  btn.click();
  return JSON.stringify({ok:true, clicked:(btn.innerText||'').trim()});
})()
"""

JS_CLOSE_DIALOG = r"""
(() => {
  // Prefer an explicit cancel/close control; fall back to Escape. Never press
  // anything whose text could commit the transaction.
  const bad = /confirm|submit|claim|add|place|save|yes/i;
  const good = /cancel|close|back|nevermind|never mind|dismiss/i;
  const all = [...document.querySelectorAll('button,[class*="button"],[class*="btn"],[class*="close"]')]
              .filter(b => b.offsetParent !== null);
  for (const b of all) {
    const t = (b.innerText || '').trim();
    if (t && good.test(t) && !bad.test(t)) { b.click(); return JSON.stringify({closed:'button:'+t}); }
  }
  for (const b of all) {
    if (/close|dismiss/i.test(String(b.className||''))) { b.click(); return JSON.stringify({closed:'class'}); }
  }
  document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', keyCode:27, bubbles:true}));
  return JSON.stringify({closed:'escape'});
})()
"""


# ------------------------------------------------------------------- session
class PlayersPage:
    """A CDP session against the league's player list."""

    def __init__(self, league_id, port=cdp.DEFAULT_PORT, host="127.0.0.1",
                 navigate=True):
        self.league_id = str(league_id)
        url = f"https://sleeper.com/leagues/{self.league_id}/players"
        ready = ("JSON.stringify(!!document.querySelector('.player-search input')"
                 " || !!document.querySelector('input[placeholder*=\"Find player\"]'))")
        if navigate:
            try:
                cdp.open_url(url, port=port, host=host, ready_js=ready)
            except Exception as e:
                raise ClaimError(f"could not open the players page: {e}")
        self.page, self.tab = cdp.attach(f"/leagues/{self.league_id}", port, host)

    def search(self, query):
        r = self.page.evaluate(JS_SET_SEARCH % json.dumps(query))
        if not (isinstance(r, dict) and r.get("ok")):
            raise ClaimError(f"could not set the search box: {r}")
        time.sleep(SEARCH_SETTLE_SEC)

    def match(self, player, click=False):
        want = {"name": player.get("name"), "pos": player.get("pos"),
                "team": player.get("team") or "", "click": bool(click)}
        return self.page.evaluate(JS_MATCH_AND_ADD % json.dumps(want))

    def dialog(self):
        return self.page.evaluate(JS_READ_DIALOG)

    def dialog_rows(self):
        return self.page.evaluate(JS_DIALOG_ROWS)

    def select_drop(self, name, click=False):
        return self.page.evaluate(JS_SELECT_DROP %
                                  json.dumps({"name": name, "click": bool(click)}))

    def set_bid(self, amount):
        return self.page.evaluate(JS_SET_BID % json.dumps(int(amount)))

    def submit(self):
        return self.page.evaluate(JS_SUBMIT)

    def close_dialog(self):
        try:
            r = self.page.evaluate(JS_CLOSE_DIALOG)
            time.sleep(0.8)
            return r
        except Exception as e:
            return {"closed": f"error: {e}"}

    def close(self):
        try:
            self.page.close()
        except Exception:
            pass


# ------------------------------------------------------------------ the drop
def protected_pids(cfg, week, season="2026", lookahead=LOOKAHEAD_WEEKS):
    """Players we must not drop, and why.

    Three reasons, in descending obviousness: he starts this week; he is needed
    to field a legal lineup in some week between now and `lookahead` (the bye
    case, which a purely weekly comparison cannot see); or he is the last body
    at a position the league requires a starter at.
    """
    board, _, _ = build_board(cfg)
    by = {p["pid"]: p for p in board}
    con = DB.connect()
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        raise ClaimError("no ownership snapshot - run track.py --sync")
    pids = [r["pid"] for r in con.execute(
        "SELECT pid FROM ownership WHERE snapshot_id=? AND owner_id=?",
        (row["s"], cfg.get("user_id")))]
    roster = [by[p] for p in pids if p in by]

    why = {}
    res = LU.analyse(roster, cfg, week, season=season)
    for p in res["lineup"]:
        why[p["pid"]] = f"starts this week ({p['proj']:.1f})"

    # Anyone whose removal would leave a required slot unfillable in any week we
    # can see. Checked one player at a time, because that is the actual question.
    slots = cfg["roster_slots"]
    for p in roster:
        if p["pid"] in why:
            continue
        rest = [q for q in roster if q["pid"] != p["pid"]]
        for wk in range(week, week + lookahead):
            before = LU.forecast(roster, cfg, 1, season, start_week=wk)[0]
            after = LU.forecast(rest, cfg, 1, season, start_week=wk)[0]
            new_holes = {k: v for k, v in (after["unfilled"] or {}).items()
                         if v > (before["unfilled"] or {}).get(k, 0)}
            if new_holes:
                why[p["pid"]] = (f"dropping him leaves week {wk} short at "
                                 f"{', '.join(new_holes)}")
                break
    return why, roster, res


def score_week(roster, cfg, week, season):
    """(points, unfilled) for the best legal lineup this roster can field."""
    import value_trade as VT
    eff = LU.effective(roster, week, cfg, season)
    playable = [p for p in eff if p["mult"] > 0]
    lineup, _, unfilled = VT.optimal_lineup(
        playable, cfg["roster_slots"], set(cfg["flex_eligible"]))
    return sum(p["proj"] for p in lineup), (unfilled or {})


def best_drop_for(cfg, roster, incoming, weeks, season="2026"):
    """Which player to drop for `incoming`, decided by simulating the result.

    Every heuristic tried before this one picked a player who should obviously
    have been kept. Ranking by weekly points offered to drop a receiver who was
    merely on a bye; ranking by "does not start this week" offered to drop the
    roster's third-best receiver in a week where two starters were out, because
    a depleted week makes almost everyone a starter and leaves the valuable
    players looking spare.

    So this stops guessing and measures. For each candidate it builds the roster
    that would exist after the swap, scores every week in `weeks`, and keeps the
    drop with the best total change. A candidate whose removal opens a starter
    slot that was previously fillable is rejected outright, however good the
    points look: closing week 6 by breaking week 9 is not a trade worth making.

    Returns None when no drop improves anything, which is the correct answer
    surprisingly often and is always better than making a move for its own sake.
    """
    base = {w: score_week(roster, cfg, w, season) for w in weeks}
    best = None
    for cand in roster:
        trial = [p for p in roster if p["pid"] != cand["pid"]] + [incoming]
        gain, opens_hole = 0.0, False
        per_week = {}
        for w in weeks:
            pts, unf = score_week(trial, cfg, w, season)
            b_pts, b_unf = base[w]
            gain += pts - b_pts
            per_week[w] = round(pts - b_pts, 2)
            if sum(unf.values()) > sum(b_unf.values()):
                opens_hole = True
                break
        if opens_hole:
            continue
        if best is None or gain > best["gain"]:
            best = {**cand, "gain": round(gain, 2), "per_week": per_week}
    return best


def position_depth(cfg):
    """How many players at each position can realistically start in a week.

    The dedicated slots, plus the flex slots for the positions eligible to fill
    them. Anyone deeper than this at his position is surplus: he cannot start
    even when everyone is healthy, so he is what a roster spot should be taken
    from.
    """
    slots = cfg["roster_slots"]
    flex_el = set(cfg["flex_eligible"])
    flex_n = slots.get("FLEX", 0) + slots.get("SUPERFLEX", 0)
    out = {}
    for pos, n in slots.items():
        if pos in ("FLEX", "SUPERFLEX"):
            continue
        out[pos] = n + (flex_n if pos in flex_el else 0)
    return out


def choose_drop(cfg, week, season, protected, roster, res):
    """The most redundant droppable player, or None if there is no safe drop.

    Ranking by value alone is wrong, and dangerously so. In a week where two
    starters are on bye, most of the roster is "not starting this week", and the
    lowest-value player left unprotected can easily be the third-best receiver
    on the team - which is exactly what the first version chose, offering to
    drop a 212-point receiver to stream a kicker.

    What a roster spot should actually come from is *surplus*: with six
    receivers, two WR slots and two flex, receivers five and six can never
    start. Depth beyond what a position can field is the real currency, and
    only inside that surplus does lowest-value become the right tiebreak.
    """
    eff = {p["pid"]: p for p in res["eff"]}
    depth = position_depth(cfg)

    # Rank each player within his own position by season-long value.
    by_pos = {}
    for p in roster:
        by_pos.setdefault(p["pos"], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: -(x.get("proj") or 0))

    surplus, rest = [], []
    for pos, players in by_pos.items():
        need = depth.get(pos, 1)
        for rank, p in enumerate(players, start=1):
            if p["pid"] in protected:
                continue
            row = {**p, "pos_rank": rank,
                   "week_proj": (eff.get(p["pid"]) or {}).get("proj", 0.0),
                   "surplus": rank > need}
            (surplus if rank > need else rest).append(row)

    # Surplus first, cheapest within it. Only if no position has any surplus at
    # all does this fall back to the cheapest protected-free player, and that
    # is a situation worth seeing in the log rather than silently resolving.
    pool = surplus or rest
    if not pool:
        return None
    pool.sort(key=lambda x: (x.get("proj") or 0, x["week_proj"]))
    return pool[0]


# ----------------------------------------------------------------------- run
def inspect(name, port=cdp.DEFAULT_PORT):
    """Open the add dialog for one player, describe it, and close it again."""
    cfg = load_config()
    pp = PlayersPage(cfg["league_id"], port=port)
    try:
        pp.search(name.split()[-1])
        m = pp.match({"name": name, "pos": None, "team": None}, click=False)
        print("match:", json.dumps(m, indent=1)[:800])
        if not (isinstance(m, dict) and m.get("ok")):
            return m
        m = pp.match({"name": name, "pos": None, "team": None}, click=True)
        time.sleep(DIALOG_SETTLE_SEC)
        d = pp.dialog()
        print("\ndialog:", json.dumps(d, indent=1)[:4000])
        return d
    finally:
        pp.close_dialog()
        pp.close()


def roster_pids(cfg):
    rosters = SY.get(f"{SY.API}/league/{cfg['league_id']}/rosters") or []
    for r in rosters:
        if str(r.get("owner_id")) == str(cfg.get("user_id")):
            return {str(x) for x in (r.get("players") or [])}
    return set()


def verify_claim(cfg, add_pid, drop_pid, week, timeout=VERIFY_TIMEOUT_SEC):
    """Confirm with Sleeper. The roster endpoint is the truth, not the page.

    A waiver claim does not take effect immediately - it is queued until the
    league processes waivers - so a pending claim shows up in transactions
    rather than on the roster. Both outcomes count as "it landed"; what must
    never pass is neither.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        pids = roster_pids(cfg)
        if str(add_pid) in pids and (not drop_pid or str(drop_pid) not in pids):
            return True, "confirmed: the roster now holds the added player"
        for t in (SY.get(f"{SY.API}/league/{cfg['league_id']}/transactions/{week}") or []):
            adds = t.get("adds") or {}
            if str(add_pid) in {str(k) for k in adds}:
                return True, (f"confirmed: a {t.get('type')} transaction is "
                              f"{t.get('status')} for this player")
        time.sleep(VERIFY_POLL_SEC)
    return False, (f"Sleeper shows neither a roster change nor a transaction "
                   f"for this player after {timeout}s")


def claim_one(pp, cfg, player, drop, bid, week, dry_run=True):
    """Add `player`, dropping `drop`. Returns a result dict; never raises for
    an ordinary refusal - the caller wants to hear about it, not crash."""
    name = player.get("name")
    pp.search(search_term(player))
    m = pp.match(player, click=False)
    if not (isinstance(m, dict) and m.get("ok")):
        return {"ok": False, "stage": "match", "why": m}
    m = pp.match(player, click=True)
    if not (isinstance(m, dict) and m.get("ok")):
        return {"ok": False, "stage": "click-add", "why": m}
    time.sleep(DIALOG_SETTLE_SEC)

    d = pp.dialog()
    if not (isinstance(d, dict) and d.get("open")):
        return {"ok": False, "stage": "dialog", "why": "the add dialog did not open"}
    needs_drop = "select a player to drop" in (d.get("text") or "").lower()

    if needs_drop:
        if not drop:
            return {"ok": False, "stage": "drop",
                    "why": "roster is full and no droppable player was found"}
        want_drop = dialog_name(drop)
        sel = pp.select_drop(want_drop, click=False)
        if not (isinstance(sel, dict) and sel.get("ok")):
            return {"ok": False, "stage": "find-drop", "why": sel}
        before = pp.dialog_rows()
        pp.select_drop(want_drop, click=True)
        time.sleep(0.9)
        after = pp.dialog_rows()
        # The dialog marks the chosen row with a class it does not give the
        # others. Compare rather than hardcode, and refuse if nothing changed:
        # submitting with no drop selected is how you add nobody and learn
        # nothing, or worse, drop whoever the UI defaulted to.
        changed = [a["name"] for a, b in zip(after or [], before or [])
                   if a.get("cls") != b.get("cls")]
        if changed != [want_drop]:
            return {"ok": False, "stage": "select-drop",
                    "why": f"selecting {want_drop} changed rows {changed!r}"}

    bidres = pp.set_bid(bid) if bid is not None else {"has_bid": False}
    if dry_run:
        return {"ok": True, "dry_run": True, "add": name,
                "drop": drop["name"] if (needs_drop and drop) else None,
                "needs_drop": needs_drop, "bid": bidres}

    s = pp.submit()
    if not (isinstance(s, dict) and s.get("ok")):
        return {"ok": False, "stage": "submit", "why": s}
    ok, why = verify_claim(cfg, player["pid"],
                           drop["pid"] if (needs_drop and drop) else None, week)
    return {"ok": ok, "add": name,
            "drop": drop["name"] if (needs_drop and drop) else None,
            "verify": why, "bid": bidres}


_DEF_NAMES = None


def def_display_name(team):
    """Sleeper lists defenses as 'Kansas City Chiefs', not 'KC DEF'."""
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


def dialog_name(player):
    """The name the add dialog uses for this player.

    The dialog lists the roster with Sleeper's own names, so a defense appears
    as 'Minnesota Vikings' while our board calls it 'MIN DEF'. Matching the
    board name against the dialog found nothing and aborted an otherwise
    correct streaming move.
    """
    if player.get("pos") == "DEF":
        return def_display_name(player.get("team")) or player.get("name")
    return player.get("name")


def search_term(player):
    """What to type in the search box to bring this player up.

    For a person the surname is enough, and is more selective than the full
    name because the list abbreviates first names anyway. A defense has no
    surname: our board calls it 'KC DEF', and typing 'DEF' matches nothing but
    noise - which is exactly how the first version failed to find one. Sleeper
    names them 'Kansas City Chiefs', so the nickname is what to search.
    """
    if player.get("pos") == "DEF":
        full = def_display_name(player.get("team"))
        if full:
            return full.split()[-1]          # 'Chiefs'
        return player.get("team") or ""
    parts = (player.get("name") or "").split()
    return parts[-1] if len(parts) > 1 else (player.get("name") or "")


def run_claims(week=None, season="2026", mode="plan", limit=1,
               port=cdp.DEFAULT_PORT, verbose=True):
    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = str(st.get("season") or season)
    week = int(week or st.get("week") or 1)

    protected, roster, res = protected_pids(cfg, week, season)
    drop = choose_drop(cfg, week, season, protected, roster, res)
    wv = WV.targets(cfg)
    live = [t for t in wv["targets"] if t["gain_ppg"] >= WV.MIN_GAIN_PPG]

    if verbose:
        print(f"WEEK {week} - budget ${wv['budget_left']}/{wv['budget_total']}")
        print(f"  droppable: {drop['name'] + ' (' + drop['pos'] + ')' if drop else 'NOBODY'}")
        for t in live[:6]:
            print(f"  target {t['player']['name']:<22}{t['player']['pos']:<5}"
                  f"+{t['gain_ppg']:.1f} ppg  bid ${t['bid']}")
    if not live:
        if verbose:
            print("  nothing worth claiming.")
        return {"week": week, "claims": [], "drop": drop}
    if mode == "plan":
        return {"week": week, "claims": live[:limit], "drop": drop}

    out = []
    pp = PlayersPage(cfg["league_id"], port=port)
    try:
        for t in live[:limit]:
            r = claim_one(pp, cfg, t["player"], drop, t.get("bid"), week,
                          dry_run=(mode == "dry-run"))
            r["gain_ppg"] = t["gain_ppg"]
            log(f"{mode}: add {t['player']['name']} drop "
                f"{drop['name'] if drop else '-'} -> {r}")
            if verbose:
                print(f"  {'OK ' if r.get('ok') else 'FAIL'} {t['player']['name']}: "
                      f"{r.get('verify') or r.get('why') or 'dry run'}")
            out.append(r)
            pp.close_dialog()
            if not r.get("ok"):
                break
    finally:
        pp.close_dialog()
        pp.close()
    return {"week": week, "results": out, "drop": drop}


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--inspect", metavar="NAME",
                   help="open the add dialog for this player, read it, cancel")
    g.add_argument("--plan", action="store_true",
                   help="what waivers.py wants and who would be dropped")
    g.add_argument("--dry-run", action="store_true",
                   help="open the dialog and fill it in, stop before submitting")
    g.add_argument("--submit", action="store_true",
                   help="actually claim, then verify against the API")
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", default="2026")
    ap.add_argument("--limit", type=int, default=1,
                    help="how many claims to make in one run")
    a = ap.parse_args()

    if a.inspect:
        inspect(a.inspect)
        return
    if a.dry_run or a.submit:
        res = run_claims(a.week, a.season,
                         mode="dry-run" if a.dry_run else "submit", limit=a.limit)
        if any(not r.get("ok") for r in (res.get("results") or [])):
            sys.exit(1)
        return

    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = str(st.get("season") or a.season)
    week = int(a.week or st.get("week") or 1)
    protected, roster, res = protected_pids(cfg, week, season)
    drop = choose_drop(cfg, week, season, protected, roster, res)
    print(f"WEEK {week} - roster {len(roster)}, protected {len(protected)}")
    for pid, reason in protected.items():
        nm = next((p["name"] for p in roster if p["pid"] == pid), pid)
        print(f"  KEEP  {nm:<24}{reason}")
    print(f"\n  DROP CANDIDATE: "
          f"{drop['name'] + ' (' + drop['pos'] + ')' if drop else 'none - every player is protected'}")
    try:
        wv = WV.targets(cfg)
        live = [t for t in wv["targets"] if t["gain_ppg"] >= WV.MIN_GAIN_PPG]
        print(f"\n  budget ${wv['budget_left']}/{wv['budget_total']}, "
              f"{len(live)} target(s) above +{WV.MIN_GAIN_PPG} ppg")
        for t in live[:6]:
            print(f"    {t['player']['name']:<24}{t['player']['pos']:<5}"
                  f"+{t['gain_ppg']:.1f} ppg -> bid ${t['bid']}")
    except Exception as e:
        print(f"  ! waiver scan failed: {e}")


if __name__ == "__main__":
    main()
