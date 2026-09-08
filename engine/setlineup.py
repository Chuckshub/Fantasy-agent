#!/usr/bin/env python3
"""Set the weekly lineup on Sleeper, rather than merely recommending one.

`lineup.py` works out which players should start. Until this module existed,
that was the end of it: the agent posted the swap to Discord and a human had to
open the app and make it. That gap is where the whole measured edge leaks away.
Benching players who cannot play is worth +0.128 win rate and +2.18 wins a
season (MODEL.md), and every point of it is contingent on the change actually
being made before kickoff. A recommendation nobody applies is worth zero.

Sleeper has no write API, so - exactly as with `submit.py` and the draft room -
the only way to move a player is to drive the real React app over the DevTools
protocol. The team page swaps two players when you click one position square
and then another, which is what this automates.

    python3 engine/setlineup.py --check      read the page and our target, no action
    python3 engine/setlineup.py --dry-run    resolve the exact swaps, stop before clicking
    python3 engine/setlineup.py --apply      make the swaps, then verify against the API

Three guards, because this writes to a real roster:

1. **Locked players are never moved.** A player whose game has kicked off is
   frozen by Sleeper anyway, but a click that silently no-ops would leave us
   believing a swap landed. The schedule feed carries no kickoff time - every
   `start_time` is null - so the guard keys off the live per-game `status` and
   moves a player only while his game is explicitly `pre_game`. Any other value
   locks him, including one we have never seen.
2. **Every swap is verified in the DOM before the next one is attempted.** React
   re-renders after each move and the row order changes; re-reading between
   clicks is the only way a second swap can trust its own indices.
3. **The result is confirmed against the Sleeper API**, not the page. The
   rosters endpoint is authoritative and cannot be fooled by a stale render.
"""
import sys, os, re, json, time, argparse, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp
import lineup as LU
import sync as SY
import db as DB
from value import build_board, load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(HERE, "logs", "setlineup.log")

SETTLE_SEC = 1.4          # React needs a beat to re-render after a swap
VERIFY_TIMEOUT_SEC = 25
VERIFY_POLL_SEC = 2.0
BENCH_SLOTS = {"BN", "IR"}

# Weekly projections wobble by a few tenths every time the feed updates, and an
# agent that rewrites the lineup for every wobble makes a dozen pointless writes
# a week and gives itself a dozen chances to leave the roster half-swapped. A
# swap has to be worth making. This threshold does NOT apply when the player
# being sat cannot play at all - a bye or an Out is a hard zero and is always
# corrected, whatever the replacement projects, because that case is the entire
# measured edge (MODEL.md: +2.18 wins a season).
MIN_APPLY_GAIN = 0.75


class LineupError(Exception):
    """Anything that means we must not click, or that a click went wrong."""


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
# Read every roster row in one evaluation. Reading rows and clicking them in
# separate passes let React re-render in between and shift the row we matched
# onto a different player - the same trap submit.py documents for the draft room.
JS_ROWS = r"""
(() => {
  const items = [...document.querySelectorAll('.team-roster-item')];
  const rows = items.map((el, i) => {
    const sq   = el.querySelector('.league-slot-position-square');
    const nm   = el.querySelector('.player-name');
    const meta = el.querySelector('.cell-player-meta');
    const lines = meta ? meta.innerText.split('\n').map(s => s.trim()).filter(Boolean) : [];
    // lines: [name, "WR - GB", "(11)", "Sun 2:25 PM", "@ MIN", ...]
    let pos = null, team = null;
    for (const ln of lines) {
      const m = ln.match(/^([A-Z]{1,3})\s*-\s*([A-Z]{2,3})$/);
      if (m) { pos = m[1]; team = m[2]; break; }
    }
    return {
      i,
      slot: sq ? sq.innerText.replace(/\s+/g, '') : null,
      slot_cls: sq ? String(sq.className || '') : null,
      name: nm ? nm.innerText.trim() : null,
      pos, team,
      empty: /^\s*Empty\s*$/i.test(meta ? meta.innerText : '')
    };
  });
  return JSON.stringify({
    url: location.href,
    is_team_page: /\/leagues\/\d+\/team/.test(location.pathname),
    league_id: (location.pathname.match(/leagues\/(\d+)/) || [])[1] || null,
    row_count: rows.length,
    rows
  });
})()
""".replace("\\n", "\\n")

# Click one position square. Sleeper listens for a real pointer sequence on the
# square, not a bare .click(), so the full sequence is dispatched at the
# element's own centre.
JS_CLICK = r"""
(() => {
  const want = %s;                       // {i, expect_name, expect_slot}
  const items = [...document.querySelectorAll('.team-roster-item')];
  const el = items[want.i];
  if (!el) return JSON.stringify({ok:false, why:'row ' + want.i + ' is gone'});
  const sq = el.querySelector('.league-slot-position-square');
  const nm = el.querySelector('.player-name');
  const name = nm ? nm.innerText.trim() : null;
  const slot = sq ? sq.innerText.replace(/\s+/g, '') : null;
  if (!sq) return JSON.stringify({ok:false, why:'no position square on row ' + want.i});
  // Re-confirm the row still holds who we matched. If React reordered under us,
  // clicking this index would move the wrong player.
  if (want.expect_name !== null && name !== want.expect_name)
    return JSON.stringify({ok:false, why:'row ' + want.i + ' now holds ' + name +
                           ', expected ' + want.expect_name});
  if (want.expect_slot !== null && slot !== want.expect_slot)
    return JSON.stringify({ok:false, why:'row ' + want.i + ' is slot ' + slot +
                           ', expected ' + want.expect_slot});
  const r = sq.getBoundingClientRect();
  const at = {bubbles:true, cancelable:true, view:window,
              clientX: r.left + r.width/2, clientY: r.top + r.height/2};
  for (const t of ['pointerdown','mousedown','pointerup','mouseup','click'])
    sq.dispatchEvent(new MouseEvent(t, at));
  return JSON.stringify({ok:true, clicked:{i:want.i, name, slot}});
})()
"""


# ------------------------------------------------------------------- session
class TeamPage:
    """A CDP session against the Sleeper team page for one league."""

    def __init__(self, league_id, port=cdp.DEFAULT_PORT, host="127.0.0.1",
                 navigate=True):
        self.league_id = str(league_id)
        url = f"https://sleeper.com/leagues/{self.league_id}/team"
        ready = ("JSON.stringify(document.querySelectorAll"
                 "('.team-roster-item').length > 0)")
        if navigate:
            try:
                cdp.open_url(url, port=port, host=host, ready_js=ready)
            except Exception as e:
                raise LineupError(f"could not open the team page: {e}")
        self.page, self.tab = cdp.attach(f"/leagues/{self.league_id}", port, host)

    def rows(self):
        st = self.page.evaluate(JS_ROWS)
        if not isinstance(st, dict):
            raise LineupError(f"unreadable team page: {st!r}")
        if not st.get("is_team_page"):
            raise LineupError(f"attached tab is not a team page: {st.get('url')}")
        if st.get("league_id") != self.league_id:
            raise LineupError(f"tab is on league {st.get('league_id')}, "
                              f"expected {self.league_id}")
        if not st.get("row_count"):
            raise LineupError("no roster rows rendered - page not loaded")
        return st["rows"]

    def click(self, row, expect_name=None, expect_slot=None):
        want = {"i": row, "expect_name": expect_name, "expect_slot": expect_slot}
        res = self.page.evaluate(JS_CLICK % json.dumps(want))
        if not (isinstance(res, dict) and res.get("ok")):
            raise LineupError(f"click on row {row} refused: "
                              f"{res.get('why') if isinstance(res, dict) else res}")
        return res

    def close(self):
        try:
            self.page.close()
        except Exception:
            pass


# ------------------------------------------------------------------ matching
def match_rows_to_players(rows, roster):
    """Map each rendered row onto one of our board players.

    The page abbreviates first names ("J Reed"), so the name alone cannot
    identify anyone - two Reeds at the same position would collide. Last name
    plus position plus team is unique on a 13-man roster, and defenses are
    identified by team alone because Sleeper renders them as a bare team code.
    """
    by_row, unmatched = {}, []
    for r in rows:
        if r.get("empty") or not r.get("name"):
            continue
        pos, team = r.get("pos"), r.get("team")
        cands = []
        for p in roster:
            if pos and p.get("pos") != pos:
                continue
            if team and (p.get("team") or "") != team:
                continue
            if pos == "DEF":
                cands.append(p)
            elif last_name(p.get("name")) == last_name(r["name"]):
                cands.append(p)
        if len(cands) == 1:
            by_row[r["i"]] = cands[0]
        else:
            unmatched.append({"row": r["i"], "name": r["name"], "pos": pos,
                              "team": team, "candidates": len(cands)})
    return by_row, unmatched


# --------------------------------------------------------------- lock guard
# Sleeper's schedule feed carries no kickoff time - `start_time` is null for
# every game and the only clock in it is a date. Locking on the date alone means
# nothing is ever locked *on game day*, which is the one day it matters: a
# rebalance run at 15:14 on a Sunday would happily try to move a player whose
# game kicked off at 11:00.
#
# What the feed does carry is a live per-game `status`. Observed values are
# 'pre_game', 'complete' and 'canceled'. So the rule is inverted: a player is
# movable only while his game is explicitly `pre_game`, and any other value -
# including one Sleeper adds later that we have never seen - locks him. An
# unknown status must fail towards refusing to touch the roster, not towards
# clicking blind.
MOVABLE_STATUS = "pre_game"


def game_status_index(season, week):
    """{team: status} for one week, from Sleeper's schedule feed."""
    out = {}
    try:
        rows = SY.get(f"https://api.sleeper.com/schedule/nfl/regular/{season}") or []
    except Exception:
        return out
    for g in rows:
        if int(g.get("week") or 0) != int(week):
            continue
        for side in ("home", "away"):
            t = g.get(side)
            if t:
                out[t] = {"status": g.get("status"), "date": g.get("date")}
    return out


def is_locked(player, kicks):
    """(locked, why). A player is movable only while his game is pre_game."""
    info = kicks.get(player.get("team") or "")
    if not info:
        # No schedule row for this team this week. That is a bye or a feed gap;
        # either way there is no game to be locked by.
        return False, ""
    st = info.get("status")
    if st == MOVABLE_STATUS:
        return False, ""
    return True, f"game status is {st!r}, not {MOVABLE_STATUS!r}"


# ------------------------------------------------------------------- planning
def target_lineup(cfg, week, season="2026"):
    """(roster, current_starters, analysis) for this week."""
    board, _, _ = build_board(cfg)
    con = DB.connect()
    by = {p["pid"]: p for p in board}
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        raise LineupError("no ownership snapshot in the store - run track.py --sync")
    pids, starters = [], set()
    for r in con.execute("SELECT pid, is_starter FROM ownership "
                         "WHERE snapshot_id=? AND owner_id=?",
                         (row["s"], cfg.get("user_id"))):
        pids.append(r["pid"])
        if r["is_starter"]:
            starters.add(r["pid"])
    roster = [by[p] for p in pids if p in by]
    if not roster:
        raise LineupError("ownership snapshot holds no players for us")
    res = LU.analyse(roster, cfg, week, current_starters=starters or None,
                     season=season)
    return roster, starters, res


def slot_token(row):
    """The canonical slot a rendered row occupies: QB/RB/WR/TE/FLEX/K/DEF/BN/IR.

    The visible text is the league's own label ('WRT' for this league's flex),
    which varies by league. The element's class does not - it is always one of
    the position keywords - so the class is the reliable source.
    """
    cls = (row.get("slot_cls") or "").lower()
    for token in ("flex", "superflex", "qb", "rb", "wr", "te", "k", "def",
                  "bn", "ir"):
        if re.search(rf"\b{token}\b", cls):
            return "FLEX" if token == "flex" else token.upper()
    return (row.get("slot") or "").upper()


def can_fill(pos, slot, cfg):
    """Could a player at `pos` legally occupy `slot`?"""
    if slot == pos:
        return True
    if slot == "FLEX":
        return pos in set(cfg["flex_eligible"])
    if slot == "SUPERFLEX":
        return pos in set(cfg["flex_eligible"]) | {"QB"}
    return False


def plan_swaps(rows, by_row, res, kicks, cfg, min_gain=MIN_APPLY_GAIN):
    """Pair each player to promote with one to sit, as clickable row indices.

    Sleeper swaps the two players when their squares are clicked in turn, so a
    plan is a list of (bench row, starter row) pairs. Pairing them by projection
    alone is wrong and would have failed on the first bye week: in week 7 it
    paired a receiver against the tight end holding the dedicated TE slot, which
    Sleeper will simply refuse, and the refusal would have aborted the rest of
    the plan with it. A promotion is therefore matched only to a starter whose
    *slot he could legally occupy*, read from the page rather than guessed.

    The most constrained slots are filled first - a dedicated TE slot can only
    take a tight end, whereas a flex can take anyone - so a scarce eligible
    player is not spent on a slot that had other options.
    """
    want_ids = {p["pid"] for p in res["lineup"]}
    row_of = {p["pid"]: i for i, p in by_row.items()}
    by_i = {r["i"]: r for r in rows}

    # who is currently started on the page, and who is benched
    started_now, benched_now = set(), set()
    slot_of = {}
    for i, p in by_row.items():
        tok = slot_token(by_i.get(i) or {})
        slot_of[p["pid"]] = tok
        (benched_now if tok in BENCH_SLOTS else started_now).add(p["pid"])

    promote = sorted([p for p in res["lineup"] if p["pid"] in benched_now],
                     key=lambda x: -x["proj"])
    demote = [p for p in res["eff"]
              if p["pid"] in started_now and p["pid"] not in want_ids]
    # Dedicated slots before flex: a flex can be filled by anyone eligible, so
    # resolving it last leaves the most room. Within each group, worst first.
    demote.sort(key=lambda x: (slot_of.get(x["pid"]) in ("FLEX", "SUPERFLEX"),
                               x["proj"]))

    plan, skipped, used = [], [], set()
    for down in demote:
        slot = slot_of.get(down["pid"])
        eligible = [p for p in promote
                    if p["pid"] not in used and can_fill(p["pos"], slot, cfg)]
        if not eligible:
            skipped.append({"start": None, "sit": down,
                            "why": f"nobody on the bench can fill the {slot} slot"})
            continue
        up = eligible[0]
        pair_bad = None
        for who in (up, down):
            locked, why = is_locked(who, kicks)
            if locked:
                pair_bad = f"{who['name']} is locked ({why})"
                break
        if pair_bad:
            skipped.append({"start": up, "sit": down, "why": pair_bad})
            continue
        if up["pid"] not in row_of or down["pid"] not in row_of:
            skipped.append({"start": up, "sit": down,
                            "why": "could not find both players on the page"})
            continue
        gain = round(up["proj"] - down["proj"], 2)
        # A player who cannot play is a hard zero, not a close call. Correct it
        # regardless of how little the replacement projects.
        forced = down.get("mult", 1.0) == 0
        if not forced and gain < min_gain:
            skipped.append({"start": up, "sit": down,
                            "why": f"gain {gain:+.2f} below the {min_gain:.2f} "
                                   f"threshold - not worth a write"})
            continue
        used.add(up["pid"])
        plan.append({
            "start": up, "sit": down, "slot": slot,
            "start_row": row_of[up["pid"]], "sit_row": row_of[down["pid"]],
            "gain": gain, "forced": forced,
        })
    # A promotion with nobody to demote means an empty slot, which is a waiver
    # problem, not a swap. res["problems"] already carries it.
    for extra in promote:
        if extra["pid"] not in used and not any(
                s.get("start") and s["start"]["pid"] == extra["pid"]
                for s in skipped):
            skipped.append({"start": extra, "sit": None,
                            "why": "no starter he can legally replace"})
    return plan, skipped


# ------------------------------------------------------------------ applying
def apply_swap(tp, step):
    """Click the two squares and confirm in the DOM that they actually swapped."""
    rows = tp.rows()
    by_i = {r["i"]: r for r in rows}
    a, b = step["start_row"], step["sit_row"]
    a_name = (by_i.get(a) or {}).get("name")
    b_name = (by_i.get(b) or {}).get("name")
    a_slot = (by_i.get(a) or {}).get("slot")
    b_slot = (by_i.get(b) or {}).get("slot")

    tp.click(a, expect_name=a_name, expect_slot=a_slot)
    time.sleep(SETTLE_SEC)
    tp.click(b, expect_name=b_name, expect_slot=b_slot)
    time.sleep(SETTLE_SEC)

    after = {r["i"]: r for r in tp.rows()}
    now_a = (after.get(a) or {}).get("slot")
    now_b = (after.get(b) or {}).get("slot")
    # The bench row should now hold a starting slot and vice versa. Sleeper may
    # also reorder rows entirely; falling back to "where did each name land"
    # keeps the check honest either way.
    where = {}
    for r in after.values():
        if r.get("name"):
            where[r["name"]] = r.get("slot")
    started = where.get(a_name)
    sat = where.get(b_name)
    ok = (started is not None and started not in BENCH_SLOTS and
          sat is not None and sat in BENCH_SLOTS)
    return ok, {"start": a_name, "sit": b_name, "start_slot_now": started,
                "sit_slot_now": sat, "row_slots": [now_a, now_b]}


def verify_against_api(cfg, expect_start_pids, expect_sit_pids,
                       timeout=VERIFY_TIMEOUT_SEC):
    """Confirm with Sleeper, not the page. The rosters endpoint is the truth."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rosters = SY.get(f"{SY.API}/league/{cfg['league_id']}/rosters") or []
        for r in rosters:
            if str(r.get("owner_id")) != str(cfg.get("user_id")):
                continue
            starters = {str(x) for x in (r.get("starters") or [])}
            last = starters
            missing = {str(p) for p in expect_start_pids} - starters
            lingering = {str(p) for p in expect_sit_pids} & starters
            if not missing and not lingering:
                return True, f"confirmed by Sleeper: {len(starters)} starters set"
        time.sleep(VERIFY_POLL_SEC)
    return False, (f"Sleeper still does not show the expected starters after "
                   f"{timeout}s (it reports: {sorted(last) if last else 'nothing'})")


# ----------------------------------------------------------------------- run
def run(week=None, season="2026", mode="check", port=cdp.DEFAULT_PORT,
        verbose=True):
    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = str(st.get("season") or season)
    week = int(week or st.get("week") or 1)

    roster, starters, res = target_lineup(cfg, week, season)
    kicks = game_status_index(season, week)

    tp = TeamPage(cfg["league_id"], port=port, navigate=True)
    try:
        rows = tp.rows()
        by_row, unmatched = match_rows_to_players(rows, roster)
        if unmatched and verbose:
            print(f"  ! {len(unmatched)} page row(s) could not be matched: "
                  f"{unmatched}")
        plan, skipped = plan_swaps(rows, by_row, res, kicks, cfg)

        if verbose:
            total = sum(p["proj"] for p in res["lineup"])
            print(f"WEEK {week} - target lineup projects {total:.1f}")
            for p in sorted(res["lineup"], key=lambda x: -x["proj"]):
                print(f"  START  {p['name']:<24}{p['pos']:<5}{p['proj']:>6.1f}")
            print()
            if not plan and not skipped:
                print("  lineup already matches the target - nothing to change.")
            for s in plan:
                print(f"  SWAP   start {s['start']['name']:<22}"
                      f"sit {s['sit']['name']:<22}{s['gain']:+.1f}")
            for s in skipped:
                # Either side can be absent: a starter nobody can legally
                # replace has no `start`, and a bench player with no starter to
                # displace has no `sit`.
                up = s["start"]["name"] if s.get("start") else "-"
                who = s["sit"]["name"] if s.get("sit") else "-"
                print(f"  SKIP   start {up:<22}sit {who:<22}({s['why']})")
            for prob in res.get("problems") or []:
                print(f"  !! {prob}")

        if mode in ("check", "dry-run") or not plan:
            return {"week": week, "plan": plan, "skipped": skipped,
                    "problems": res.get("problems") or [], "applied": [],
                    "verified": None}

        applied, failed = [], []
        for step in plan:
            ok, detail = apply_swap(tp, step)
            line = (f"start {step['start']['name']} / sit {step['sit']['name']} "
                    f"({step['gain']:+.1f}) -> {'ok' if ok else 'FAILED'} {detail}")
            log(line)
            if verbose:
                print(f"  {'APPLIED' if ok else 'FAILED '} {line}")
            (applied if ok else failed).append(step)
            if not ok:
                # Stop rather than compound a bad state onto a page we no longer
                # understand. Whatever landed is verified below regardless.
                break

        ok, why = verify_against_api(
            cfg,
            [s["start"]["pid"] for s in applied],
            [s["sit"]["pid"] for s in applied]) if applied else (True, "nothing to verify")
        log(f"verify: {ok} - {why}")
        if verbose:
            print(f"\n  VERIFY {'ok' if ok else 'FAILED'}: {why}")
        return {"week": week, "plan": plan, "skipped": skipped,
                "problems": res.get("problems") or [],
                "applied": applied, "failed": failed,
                "verified": ok, "verify_why": why}
    finally:
        tp.close()


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true",
                   help="read the page and the target lineup, change nothing")
    g.add_argument("--dry-run", action="store_true",
                   help="resolve the exact swaps, stop before clicking")
    g.add_argument("--apply", action="store_true",
                   help="make the swaps and verify them against the API")
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", default="2026")
    a = ap.parse_args()
    mode = "check" if a.check else "dry-run" if a.dry_run else "apply"
    try:
        res = run(week=a.week, season=a.season, mode=mode)
    except LineupError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        log(f"REFUSED: {e}")
        sys.exit(2)
    if res.get("verified") is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
