#!/usr/bin/env python3
"""Publish calibrated probabilities before each week, so they can be graded.

    python3 engine/forecast.py --publish          this week's forecasts
    python3 engine/forecast.py --publish --week 3
    python3 engine/forecast.py --show             what is on record, unresolved

A projection is unfalsifiable. "Nico Collins projects 13.2" is never right or
wrong - whatever he scores, the number was "close" or "unlucky". A probability
is not: publish "62% to clear 10 points" often enough and the record either
holds up or it does not, and no amount of narrative rescues it. That is the
entire reason this module exists, and the reason every forecast is written down
with a timestamp *before* the games rather than reconstructed afterwards.

Three families of proposition, chosen because all three resolve unambiguously
from data we already collect:

- `player_over`  - a player clears a points threshold. A ladder of thresholds
  per player, not one, because a reliability plot needs forecasts spread across
  the whole probability range. One threshold per player would pile everything
  near 50% and tell us nothing about whether our 90% claims are any good.
- `matchup_win`  - a team wins its head-to-head. Simulated, not assumed.
- `team_over`    - a team's starting lineup clears a points total.

Forecasts cover **every team in the league**, not just ours. That is roughly
four hundred resolvable propositions a week rather than thirty, which is the
difference between a reliability plot you can read after a month and one you
cannot read after a season.
"""
import sys, os, json, argparse, datetime, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import lineup as LU
import value_trade as VT
import nflverse as NV
import calib as CB
import sync as SY
from value import build_board, load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIMS = 20000
# Thresholds are absolute points, laddered by position so that each player gets
# forecasts at several probability levels rather than one near the middle.
LADDERS = {
    "QB":  [10, 15, 18, 22, 26],
    "RB":  [5, 10, 14, 18, 22],
    "WR":  [5, 10, 14, 18, 22],
    "TE":  [4, 8, 12, 16],
    "K":   [4, 7, 10, 13],
    "DEF": [2, 5, 8, 12],
}

DDL = """
CREATE TABLE IF NOT EXISTS forecast (
  id VARCHAR PRIMARY KEY,
  season INTEGER, week INTEGER, made_at TIMESTAMP,
  kind VARCHAR,            -- player_over | matchup_win | team_over
  subject VARCHAR,         -- pid, or roster_id
  label VARCHAR,           -- human readable
  pos VARCHAR,
  threshold DOUBLE,
  prob DOUBLE,
  proj DOUBLE,
  resolved BOOLEAN DEFAULT FALSE,
  outcome INTEGER,         -- 1 / 0 once known
  actual DOUBLE
)
"""


def con_rw():
    con = NV.connect()
    con.execute(DDL)
    return con


def league_rosters(cfg, board):
    """{roster_id: (players, starters)} from the latest ownership snapshot."""
    by = {p["pid"]: p for p in board}
    s = DB.connect()
    row = s.execute("SELECT MAX(snapshot_id) x FROM ownership").fetchone()
    if not row or not row["x"]:
        raise SystemExit("no ownership snapshot - run track.py --sync")
    snap = row["x"]
    out, names = {}, {}
    for r in s.execute("SELECT roster_id, team_name, username FROM manager"):
        names[r["roster_id"]] = r["team_name"] or r["username"]
    for r in s.execute("SELECT DISTINCT roster_id FROM ownership WHERE snapshot_id=?",
                       (snap,)):
        rid = r["roster_id"]
        players, starters = [], set()
        for x in s.execute("SELECT pid,is_starter FROM ownership "
                           "WHERE snapshot_id=? AND roster_id=?", (snap, rid)):
            if x["pid"] in by:
                players.append(by[x["pid"]])
            if x["is_starter"]:
                starters.add(x["pid"])
        if players:
            out[rid] = (players, starters)
    return out, names


def best_lineup(players, cfg, week, season):
    eff = LU.effective(players, week, cfg, season)
    playable = [p for p in eff if p["mult"] > 0]
    lineup, _, _ = VT.optimal_lineup(playable, cfg["roster_slots"],
                                     set(cfg["flex_eligible"]))
    return eff, lineup


def simulate_team(lineup, model, rng, sims=SIMS):
    """Total points distribution for one lineup.

    Each starter is drawn independently. That is an approximation and worth
    naming: a quarterback and his own receiver are correlated, so a real lineup
    has slightly fatter tails than this produces. It biases win probabilities
    toward 50% rather than in a self-flattering direction, which is the safer
    way for a calibration system to be wrong.
    """
    totals = [0.0] * sims
    for p in lineup:
        draws = CB.sample(p["proj"], p["pos"], rng, model, n=sims)
        for i, d in enumerate(draws):
            totals[i] += d
    return totals


def publish(week=None, season=None, sims=SIMS, verbose=True, seed=7):
    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = int(season or st.get("season") or 2026)
    week = int(week or st.get("week") or 1)
    model = CB.load_model()
    if not model:
        raise SystemExit("no calibration model - run: engine/calib.py --fit")

    board, _, _ = build_board(cfg)
    rosters, names = league_rosters(cfg, board)
    rng = random.Random(seed)
    now = datetime.datetime.now()
    con = con_rw()

    # A forecast is a claim made BEFORE the event. Nothing else about this
    # module means anything if that slips.
    #
    # The scheduled run is Thursday lunchtime, which is comfortably before the
    # Sunday slate and was assumed to be before everything. It is not: week 1
    # opens on a Wednesday or Thursday, so by the time the job fires there are
    # already completed games. The original code opened by deleting every
    # unresolved forecast for the week and rewriting it, which would have
    # re-forecast finished games with their outcomes already known and inflated
    # the Brier skill score - silently, and in the flattering direction.
    #
    # So: a proposition is only published while its game is still `pre_game`,
    # and an existing forecast is only replaced if it is still pre_game too.
    # Anything already under way stands exactly as it was written.
    import setlineup as SL
    status = SL.game_status_index(str(season), week)

    def still_open(team):
        info = status.get(team or "")
        # No schedule row means a bye - there is nothing to forecast anyway.
        return bool(info) and info.get("status") == SL.MOVABLE_STATUS

    locked_teams = sorted(t for t, i in status.items()
                          if i.get("status") != SL.MOVABLE_STATUS)
    if locked_teams and verbose:
        print(f"  {len(locked_teams)} team(s) already playing or finished - "
              f"their propositions are left untouched: {', '.join(locked_teams)}")

    existing = {r[0] for r in con.execute(
        "SELECT id FROM forecast WHERE season=? AND week=?",
        (season, week)).fetchall()}

    rows, team_totals, skipped_locked, open_team = [], {}, 0, {}
    for rid, (players, starters) in rosters.items():
        eff, lineup = best_lineup(players, cfg, week, season)
        totals = simulate_team(lineup, model, rng, sims)
        team_totals[rid] = totals
        open_team[rid] = all(still_open(p.get("team")) for p in lineup)
        # ---- player propositions, for every starter in the league
        for p in lineup:
            if not still_open(p.get("team")):
                skipped_locked += 1
                continue
            for t in LADDERS.get(p["pos"], []):
                pr = CB.p_at_least(p["proj"], p["pos"], t, model)
                if pr is None:
                    continue
                rows.append((
                    f"{season}-{week}-po-{p['pid']}-{t}", season, week, now,
                    "player_over", p["pid"],
                    f"{p['name']} ({p['pos']}, {names.get(rid,'?')}) over {t}",
                    p["pos"], float(t), float(pr), float(p["proj"]),
                    False, None, None))
        # ---- team total proposition, at its own median (a genuine coin-flip
        #      by construction, which is the sharpest test of the simulator)
        med = sorted(totals)[len(totals) // 2]
        line = round(med / 5.0) * 5.0
        over = sum(1 for t in totals if t >= line) / len(totals)
        # A team total is partly decided the moment one of its starters kicks
        # off, so the whole proposition is only honest while all of them wait.
        if not all(still_open(p.get("team")) for p in lineup):
            skipped_locked += 1
            continue
        rows.append((f"{season}-{week}-to-{rid}", season, week, now,
                     "team_over", str(rid),
                     f"{names.get(rid,'?')} lineup over {line:.0f}", None,
                     float(line), float(over), float(med), False, None, None))

    # ---- head-to-head win probabilities
    ms = SY.get(f"{SY.API}/league/{cfg['league_id']}/matchups/{week}") or []
    pairs = {}
    for m in ms:
        if m.get("matchup_id") is None:
            continue
        pairs.setdefault(m["matchup_id"], []).append(m["roster_id"])
    for mid, rids in pairs.items():
        if len(rids) != 2:
            continue
        a, b = rids
        if a not in team_totals or b not in team_totals:
            continue
        if not (open_team.get(a) and open_team.get(b)):
            skipped_locked += 1
            continue
        ta, tb = team_totals[a], team_totals[b]
        wins = sum(1 for i in range(len(ta)) if ta[i] > tb[i]) / len(ta)
        for rid, p in ((a, wins), (b, 1.0 - wins)):
            other = b if rid == a else a
            rows.append((f"{season}-{week}-mw-{rid}", season, week, now,
                         "matchup_win", str(rid),
                         f"{names.get(rid,'?')} beats {names.get(other,'?')}",
                         None, None, float(p),
                         float(sorted(team_totals[rid])[len(ta)//2]),
                         False, None, None))

    fresh = [r for r in rows if r[0] not in existing]
    replaced = [r for r in rows if r[0] in existing]
    con.executemany(
        "INSERT OR REPLACE INTO forecast VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    if verbose:
        print(f"published {len(rows)} forecasts for {season} week {week} "
              f"({len(fresh)} new, {len(replaced)} refreshed before kickoff)")
        if skipped_locked:
            print(f"  {skipped_locked} proposition(s) NOT written - their game "
                  f"has already started. A forecast made after the event is "
                  f"not a forecast.")
        byk = {}
        for r in rows:
            byk[r[4]] = byk.get(r[4], 0) + 1
        for k, n in sorted(byk.items()):
            print(f"  {k:<14}{n:>5}")
    return {"season": season, "week": week, "n": len(rows),
            "team_totals": {k: sorted(v) for k, v in team_totals.items()},
            "names": names}


def show(season=None, week=None):
    con = con_rw()
    q = "SELECT kind, label, prob, threshold, proj, resolved, outcome FROM forecast"
    w = []
    if season:
        w.append(f"season={int(season)}")
    if week:
        w.append(f"week={int(week)}")
    if w:
        q += " WHERE " + " AND ".join(w)
    q += " ORDER BY kind, prob DESC"
    rows = con.execute(q).fetchall()
    print(f"{len(rows)} forecasts on record")
    for kind in ("matchup_win", "team_over", "player_over"):
        sub = [r for r in rows if r[0] == kind]
        if not sub:
            continue
        print(f"\n== {kind} ({len(sub)})")
        for r in sub[:14]:
            mark = "" if not r[5] else ("  -> HIT" if r[6] else "  -> miss")
            print(f"  {r[2]:>6.1%}  {r[1]}{mark}")
        if len(sub) > 14:
            print(f"  ... and {len(sub)-14} more")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int)
    ap.add_argument("--sims", type=int, default=SIMS)
    a = ap.parse_args()
    if a.publish:
        publish(a.week, a.season, a.sims)
    else:
        show(a.season, a.week)


if __name__ == "__main__":
    main()
