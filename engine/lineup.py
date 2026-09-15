#!/usr/bin/env python3
"""Weekly lineup optimisation and rebalance checks.

The draft engine reasons in season-long points. A lineup does not: a player on
bye scores exactly zero, a player ruled Out scores exactly zero, and a season
projection says nothing about either. This module works in **effective weekly
points** - the week's projection scaled by whether the player can actually play
- and reports the swaps that raise the total.

    python3 engine/lineup.py --week 5
    python3 engine/lineup.py --week 5 --roster "Jahmyr Gibbs,Nico Collins,..."
    python3 engine/lineup.py --forecast 6      # bye/health trouble ahead

Run it after games and before lock. What it will not do is pretend to know
kickoff times: Sleeper's projection feed carries a game *date*, not a time, so
a player whose game is today is reported as "may already be locked" rather
than silently assumed movable.
"""
import sys, os, json, argparse, datetime, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import value_trade as VT
import history as HI
import model as MO
import scoring as SC
from value import build_board, load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (statking)"}

# effective() is called once per week by forecast() and once per week again by
# anything estimating a season total, and each call was re-running a full
# aggregation over 85k game logs plus six HTTP fetches. Memoised per process.
_WK_CACHE, _DVP_CACHE = {}, {}

# Cannot play. Sleeper uses short codes; NA is "not active" (roster exempt).
OUT_STATUSES = {"Out", "IR", "PUP", "NA", "Sus", "DNR", "COV", "DNP"}
# Can play, but discounted. These multipliers are deliberately blunt - the point
# is to break ties toward the healthy player, not to model probability of play.
STATUS_MULT = {"Doubtful": 0.25, "Questionable": 0.90, "Limited": 0.95}

SCORING_KEY = {"ppr": "pts_ppr", "half_ppr": "pts_half_ppr", "std": "pts_std"}

# How many weeks ahead a current injury designation is allowed to apply.
# One: this week and next. Beyond that a designation is not evidence about
# the week being asked about, and treating it as such re-opens bye holes that
# were already paid to close.
INJURY_HORIZON = 1


def get(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ! {url} -> {e}", file=sys.stderr)
        return None


def week_projections(season, week, positions=("QB", "RB", "WR", "TE", "K", "DEF")):
    """{pid: row} for one week. A bye shows up as opponent None / pts None."""
    out = {}
    for pos in positions:
        rows = get(f"https://api.sleeper.com/projections/nfl/{season}/{week}"
                   f"?season_type=regular&position[]={pos}&order_by=pts_ppr") or []
        for r in rows:
            pid = r.get("player_id")
            if pid and pid not in out:
                out[pid] = r
    return out


def load_byes():
    p = os.path.join(HERE, "data", "schedule_2026.json")
    s = json.load(open(p))
    return s.get("byes") or {}, s.get("opponents") or {}


def injury_status(pid):
    """Live injury designation straight from the player file."""
    global _PLAYERS
    try:
        _PLAYERS
    except NameError:
        _PLAYERS = json.load(open(os.path.join(HERE, "data", "players_nfl.json")))
    return (_PLAYERS.get(pid) or {}).get("injury_status")


def playability(p, week, wkrow, byes, opponents, current_week=None):
    """(multiplier, reason). 0.0 means this player cannot be started this week.

    Byes are facts about a specific week and apply whenever they are asked
    about. **Injury designations are not.** A player listed Out today is a fact
    about today; carrying that into week 13 asserts he is injured for three
    months, which no designation means.

    That distinction was missing and it mattered immediately. A running back
    acquired to cover the week 6 and week 8 byes was listed Out with a knee in
    week 2, and the forward-looking planner promptly reported him unavailable in
    weeks 5, 6, 7, 8, 11 and 13 - re-opening the very holes he had been bought
    to close, and inviting another round of waiver moves to fix a problem that
    did not exist.

    So the designation is applied only inside `INJURY_HORIZON` weeks of the
    current one. Past that the season projection already prices expected missed
    games, and a designation adds nothing but false precision.
    """
    team = p.get("team")
    if team and byes.get(team) == week:
        return 0.0, f"BYE (week {week})"
    if team and opponents.get(team) is not None and str(week) not in opponents[team]:
        return 0.0, f"BYE (no game week {week})"
    if wkrow is not None and wkrow.get("opponent") is None:
        return 0.0, "no opponent listed (bye or not on a roster)"
    if current_week is not None and week - current_week > INJURY_HORIZON:
        return 1.0, ""
    st = injury_status(p["pid"])
    if st in OUT_STATUSES:
        return 0.0, f"OUT ({st})"
    if st in STATUS_MULT:
        return STATUS_MULT[st], st
    return 1.0, ""


# The matchup adjustment is shown, never applied. Backtesting on 2023-2025 found
# it does not improve prediction at any cap - see MODEL.md. What replaced it is
# the blend in model.composite(): 0.8 * Sleeper's weekly projection + 0.2 * the
# player's season-to-date average, which beat pure projection in both graded
# seasons. Before week 5 of a season there is no season-to-date mean to blend,
# and composite() correctly falls back to the projection alone.


def week_points(stats, cfg, pos, key):
    """This week's points under THIS league's scoring, not Sleeper's default.

    The weekly projections endpoint returns a `pts_ppr` computed with Sleeper's
    generic PPR rules, and our league does not use them: passing yards score
    0.04 here and 0.05 there. On Trevor Lawrence's week 1 line that is 228
    passing yards x 0.01 = 2.3 points, and the engine was reading 20.1 for a
    quarterback Sleeper's own app showed at 17.84 - every quarterback overstated
    by roughly a point per 25 passing yards, every week.

    The season-long projections file does not have this problem, which is why
    `scoring.verify_alignment` - which checks that file - reported no drift and
    the discrepancy went unnoticed. Two feeds, two scoring rules.

    So the pts_* field is not trusted at all for the positions whose stat lines
    can be re-scored. Kickers and defenses cannot be (their scoring depends on
    field-goal distances and points-allowed brackets the feed does not break
    out), so for those the feed's own number is still the best available.
    """
    if not stats:
        return None
    mine = SC.league_points(stats, cfg.get("scoring_settings") or {}, pos)
    return mine if mine is not None else stats.get(key)


def effective(roster, week, cfg, season="2026", apply_matchup=False,
              dvp_season="2025", current_week=None):
    """Roster copies whose `proj` is this week's effective points."""
    byes, opponents = load_byes()
    ck = (season, week)
    if ck not in _WK_CACHE:
        _WK_CACHE[ck] = week_projections(season, week)
    wk = _WK_CACHE[ck]
    key = SCORING_KEY.get(cfg.get("scoring", "ppr"), "pts_ppr")
    if dvp_season not in _DVP_CACHE:
        try:
            _DVP_CACHE[dvp_season] = HI.defense_vs_position(dvp_season)
        except Exception:
            _DVP_CACHE[dvp_season] = {}
    dvp = _DVP_CACHE[dvp_season]
    out = []
    for p in roster:
        row = wk.get(p["pid"])
        stats = (row or {}).get("stats") or {}
        raw = week_points(stats, cfg, p["pos"], key)
        mult, reason = playability(p, week, row, byes, opponents, current_week)
        q = dict(p)
        q["week_raw"] = raw
        q["mult"] = mult
        q["reason"] = reason
        q["game_date"] = (row or {}).get("date")
        # No weekly row (rare, e.g. a DEF the feed omits): fall back to a
        # per-game share of the season projection rather than dropping them.
        base = raw if raw is not None else (p.get("proj") or 0) / 17.0

        # validated blend: projection, tempered by what he has actually done
        std = MO.season_to_date(p["pid"], season, week)
        q["season_to_date"] = std
        base = MO.composite(base, std)

        opp = (row or {}).get("opponent")
        q["opponent"] = opp
        q["matchup_mult"], q["matchup_why"] = (1.0, "")
        if opp and mult > 0 and dvp:
            m, why = HI.matchup_edge(p["pid"], p["pos"], opp, dvp)
            q["matchup_mult"], q["matchup_why"] = m, why   # shown, not applied
        q["proj"] = round((base or 0) * mult, 2)
        out.append(q)
    return out


def today_iso():
    return datetime.date.today().isoformat()


def analyse(roster, cfg, week, current_starters=None, season="2026",
            apply_matchup=False):
    eff = effective(roster, week, cfg, season, apply_matchup=apply_matchup)
    # A player who cannot play is not a lineup option at all. Leaving them in
    # the pool made the optimiser "fill" the DEF slot with a defense on bye
    # for 0.0 points and call the lineup complete - exactly the failure this
    # module exists to prevent. An empty slot is a waiver problem, and it has
    # to surface as one.
    playable = [p for p in eff if p["mult"] > 0]
    unavailable = [p for p in eff if p["mult"] == 0]
    lineup, bench, unfilled = VT.optimal_lineup(
        playable, cfg["roster_slots"], set(cfg["flex_eligible"]))
    lineup_ids = {p["pid"] for p in lineup}

    problems = []
    for pos, n in unfilled.items():
        problems.append(
            f"NO STARTABLE {pos} this week ({n} slot(s) empty) - pick one up")
    for p in unavailable:
        if current_starters is not None and p["pid"] in current_starters:
            problems.append(f"{p['name']} ({p['pos']}) is STARTING but {p['reason']}")
    swaps = []
    if current_starters is not None:
        cur = {p["pid"]: p for p in eff if p["pid"] in current_starters}
        bench_in = [p for p in lineup if p["pid"] not in current_starters]
        start_out = [p for p in eff if p["pid"] in current_starters
                     and p["pid"] not in lineup_ids]
        # zip() truncates to the shorter list, which silently dropped starters
        # who had no replacement available - precisely the bye-week pileup this
        # module exists to catch. Pair what can be paired, then report the rest
        # as benchings with nobody to promote.
        ins = sorted(bench_in, key=lambda x: -x["proj"])
        outs = sorted(start_out, key=lambda x: x["proj"])
        for i, b in enumerate(outs):
            if i < len(ins):
                swaps.append({"start": ins[i], "sit": b,
                              "gain": round(ins[i]["proj"] - b["proj"], 2)})
            else:
                swaps.append({"start": None, "sit": b,
                              "gain": round(-b["proj"], 2)})
    return {"eff": eff, "lineup": lineup, "bench": bench, "unfilled": unfilled,
            "unavailable": unavailable, "problems": problems, "swaps": swaps}


def forecast(roster, cfg, weeks, season="2026", start_week=1, current_week=None):
    """Weeks where byes or injuries leave us unable to field a full lineup."""
    rows = []
    for wk in range(start_week, start_week + weeks):
        eff = effective(roster, wk, cfg, season, current_week=current_week)
        playable = [p for p in eff if p["mult"] > 0]
        lineup, bench, unfilled = VT.optimal_lineup(
            playable, cfg["roster_slots"], set(cfg["flex_eligible"]))
        out_names = [p["name"] for p in eff if p["mult"] == 0]
        rows.append({"week": wk, "playable": len(playable),
                     "unfilled": unfilled,
                     "points": round(sum(p["proj"] for p in lineup), 1),
                     "unavailable": out_names})
    return rows


def _roster_from_names(names, board):
    by = {p["name"].lower(): p for p in board}
    out, missing = [], []
    for n in names:
        p = by.get(n.strip().lower())
        (out if p else missing).append(p if p else n)
    if missing:
        print(f"  ! not on the board: {missing}", file=sys.stderr)
    return out


def _roster_from_db(cfg, board):
    con = DB.connect()
    by = {p["pid"]: p for p in board}
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        return [], set()
    pids, starters = [], set()
    for r in con.execute("SELECT pid, is_starter FROM ownership "
                         "WHERE snapshot_id=? AND owner_id=?",
                         (row["s"], cfg.get("user_id"))):
        pids.append(r["pid"])
        if r["is_starter"]:
            starters.add(r["pid"])
    return [by[p] for p in pids if p in by], starters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--forecast", type=int, metavar="N",
                    help="check the next N weeks for bye/health trouble")
    ap.add_argument("--roster", help="comma-separated names (testing)")
    ap.add_argument("--season", default="2026")
    ap.add_argument("--matchup", action="store_true",
                    help="apply the defence-vs-position adjustment, not just show it")
    a = ap.parse_args()

    cfg = load_config()
    board, _, _ = build_board(cfg)
    if a.roster:
        roster, starters = _roster_from_names(a.roster.split(","), board), None
    else:
        roster, starters = _roster_from_db(cfg, board)
        if not roster:
            print("No roster in the store yet (draft has not happened). "
                  "Use --roster \"Name,Name,...\" to test.")
            return

    if a.forecast:
        start = a.week or 1
        print(f"BYE / AVAILABILITY FORECAST  weeks {start}-{start + a.forecast - 1}\n")
        for r in forecast(roster, cfg, a.forecast, a.season, start):
            flag = "  <-- CANNOT FIELD FULL LINEUP" if r["unfilled"] else ""
            print(f"  week {r['week']:>2}  playable {r['playable']:>2}  "
                  f"proj {r['points']:>6.1f}{flag}")
            if r["unfilled"]:
                print(f"            short: {r['unfilled']}")
            if r["unavailable"]:
                print(f"            out/bye: {', '.join(r['unavailable'])}")
        return

    wk = a.week or 1
    res = analyse(roster, cfg, wk, starters, a.season, apply_matchup=a.matchup)
    print(f"WEEK {wk} LINEUP  ({cfg.get('scoring')})\n")
    for p in sorted(res["lineup"], key=lambda x: -x["proj"]):
        note = f"  [{p['reason']}]" if p["reason"] else ""
        mm = p.get("matchup_mult", 1.0)
        tag = f"  vs {p.get('opponent')} x{mm:.2f}" if p.get("opponent") else ""
        print(f"  START  {p['name']:<24}{p['pos']:<5}{p['proj']:>6.1f}{tag}{note}")
    print()
    for p in sorted(res["bench"], key=lambda x: -x["proj"]):
        note = f"  [{p['reason']}]" if p["reason"] else ""
        print(f"  bench  {p['name']:<24}{p['pos']:<5}{p['proj']:>6.1f}{note}")
    for p in sorted(res["unavailable"], key=lambda x: x["name"]):
        print(f"  OUT    {p['name']:<24}{p['pos']:<5}{'-':>6}  [{p['reason']}]")
    if res["unfilled"]:
        print(f"\n  !! unfilled starter slots: {res['unfilled']}")
    if res["problems"]:
        print("\n  !! PROBLEMS")
        for x in res["problems"]:
            print(f"     - {x}")
    if res["swaps"]:
        print("\n  REBALANCE")
        today = today_iso()
        for s in res["swaps"]:
            lock = ""
            for p in (s["start"], s["sit"]):
                if p.get("game_date") and p["game_date"] <= today:
                    lock = "  (may already be locked - game date has arrived)"
            if s["start"] is None:
                print(f"     SIT   {s['sit']['name']:<22} "
                      f"no replacement available - pick one up{lock}")
            else:
                print(f"     start {s['start']['name']:<22} sit {s['sit']['name']:<22}"
                      f" {s['gain']:+.1f}{lock}")
    elif starters is not None:
        print("\n  lineup is already optimal.")


if __name__ == "__main__":
    main()
