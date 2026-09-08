#!/usr/bin/env python3
"""Rank every team by its simulated points distribution, not a point estimate.

    python3 engine/power.py            this week
    python3 engine/power.py --week 3
    python3 engine/power.py --json

A projected total is one number pretending to be a forecast. Two teams both
projecting 128 are not equivalent if one is four steady veterans and the other
is a boom-or-bust receiver room: over a season the second wins more coin-flips
and loses more blowouts, and a ranking that cannot see the difference is
ranking the wrong thing.

So each team's starters are drawn ten thousand times from the calibrated
outcome distributions in `calib.py` and the ranking is built from what comes
out: the median week, the tenth and ninetieth percentile, the chance of topping
the whole league, and the chance of winning the actual head-to-head this week.

Ranking is by **median**, deliberately, not by mean. Fantasy weeks are
right-skewed and the mean is dragged around by ceiling outcomes a team will see
three times a season. The median is the week a team should expect.
"""
import sys, os, json, argparse, random, statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calib as CB
import forecast as FC
import sync as SY
from value import build_board, load_config

SIMS = 10000


def rank(week=None, season=None, sims=SIMS, seed=11):
    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = int(season or st.get("season") or 2026)
    week = int(week or st.get("week") or 1)
    model = CB.load_model()
    if not model:
        raise SystemExit("no calibration model - run engine/calib.py --fit")

    board, _, _ = build_board(cfg)
    rosters, names = FC.league_rosters(cfg, board)
    rng = random.Random(seed)

    sim, lineups = {}, {}
    for rid, (players, starters) in rosters.items():
        eff, lineup = FC.best_lineup(players, cfg, week, season)
        sim[rid] = FC.simulate_team(lineup, model, rng, sims)
        lineups[rid] = lineup

    # chance of being the week's highest scorer, from the same draws
    rids = list(sim)
    top_counts = {r: 0 for r in rids}
    for i in range(sims):
        best = max(rids, key=lambda r: sim[r][i])
        top_counts[best] += 1

    # head-to-head win probability for the real schedule
    ms = SY.get(f"{SY.API}/league/{cfg['league_id']}/matchups/{week}") or []
    pairs, winp, opp_of = {}, {}, {}
    for m in ms:
        if m.get("matchup_id") is not None:
            pairs.setdefault(m["matchup_id"], []).append(m["roster_id"])
    for rl in pairs.values():
        if len(rl) == 2 and rl[0] in sim and rl[1] in sim:
            a, b = rl
            w = sum(1 for i in range(sims) if sim[a][i] > sim[b][i]) / sims
            winp[a], winp[b] = w, 1 - w
            opp_of[a], opp_of[b] = b, a

    rows = []
    myrid = None
    import db as DB
    s = DB.connect()
    for r in s.execute("SELECT roster_id, owner_id FROM manager"):
        if str(r["owner_id"]) == str(cfg.get("user_id")):
            myrid = r["roster_id"]
    for rid in rids:
        v = sorted(sim[rid])
        rows.append({
            "roster_id": rid, "name": names.get(rid, str(rid)),
            "median": v[len(v) // 2],
            "mean": statistics.fmean(v),
            "p10": v[int(.10 * len(v))], "p90": v[int(.90 * len(v))],
            "spread": v[int(.90 * len(v))] - v[int(.10 * len(v))],
            "p_top": top_counts[rid] / sims,
            "p_win": winp.get(rid),
            "opponent": names.get(opp_of.get(rid), None),
            "us": rid == myrid,
        })
    rows.sort(key=lambda r: -r["median"])
    return {"season": season, "week": week, "sims": sims, "teams": rows}


def render(res):
    L = [f"WEEK {res['week']} POWER RANKING - {res['sims']:,} simulations per team",
         "ranked by median week, not by projection\n"]
    L.append(f"{'':<3}{'team':<18}{'median':>8}{'floor':>8}{'ceil':>8}"
             f"{'swing':>8}{'P(top)':>8}{'P(win)':>8}  opponent")
    for i, t in enumerate(res["teams"], 1):
        mark = " <-- US" if t["us"] else ""
        pw = f"{t['p_win']:.0%}" if t["p_win"] is not None else "-"
        L.append(f"{i:<3}{t['name'][:17]:<18}{t['median']:>8.1f}{t['p10']:>8.1f}"
                 f"{t['p90']:>8.1f}{t['spread']:>8.1f}{t['p_top']:>8.1%}"
                 f"{pw:>8}  {t['opponent'] or '-'}{mark}")
    meds = [t["median"] for t in res["teams"]]
    L.append(f"\n  league median {statistics.median(meds):.1f}, "
             f"spread {max(meds)-min(meds):.1f}")
    L.append("  floor/ceil are the 10th and 90th percentile weeks; swing is the gap")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int)
    ap.add_argument("--sims", type=int, default=SIMS)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = rank(a.week, a.season, a.sims)
    print(json.dumps(res, indent=1) if a.json else render(res))


if __name__ == "__main__":
    main()
