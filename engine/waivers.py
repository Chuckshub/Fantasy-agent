#!/usr/bin/env python3
"""Waiver-wire hunting and FAAB bidding.

    python3 engine/waivers.py --targets        who to claim this week
    python3 engine/waivers.py --budget         what we have left to spend

Why this exists, measured rather than assumed. Across 2025, the share of weekly
top-N finishes produced by players who were projected under 6 points in week 1 -
that is, nobody's starter, mostly free agents:

    RB  107 of 432 = 25%
    WR  189 of 648 = 29%
    TE   73 of 216 = 34%

Eighty-five separate 18+ point weeks came from that group, averaging 22.7. So
roughly a third of all startable production each season belongs to players who
were not drafted at all. No amount of draft-pick refinement competes with that,
and nothing in this project addressed it until now.

**What is and is not validated.** The detection signals below are computed from
real game logs and are as sound as the data. The BID SIZING is a heuristic: we
have no historical FAAB auction data for this league, so there is nothing to
backtest it against. It is calibrated on budget arithmetic - do not read it as
having the same standing as the model in MODEL.md.
"""
import sys, os, json, argparse, statistics, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import sync as SY
from value import build_board, load_config

# A claim is only worth making if it beats what we would otherwise start.
MIN_GAIN_PPG = 1.0
# Never spend the whole budget on one player, however good he looks: the wire
# refills every week and a spent budget cannot answer an injury in week 10. But
# a true league-winner is worth well over a third, so the ceiling is 60%.
MAX_SINGLE_BID_SHARE = 0.60


def rostered_pids(cfg):
    """Every player owned by anyone in the league, straight from Sleeper.

    Read uncached. A stale roster here is not a cosmetic problem: it made a
    player we had just dropped look like he was still ours, which stopped an
    attempt to take him back.
    """
    out = set()
    for r in (SY.get(f"{SY.API}/league/{cfg['league_id']}/rosters", fresh=True) or []):
        for pid in (r.get("players") or []):
            out.add(pid)
    return out


def our_budget(cfg):
    """FAAB left, from the league's own record of what we have spent."""
    total = cfg.get("waiver_budget") or 100
    spent = 0
    uid = cfg.get("user_id")
    for r in (SY.get(f"{SY.API}/league/{cfg['league_id']}/rosters") or []):
        if r.get("owner_id") == uid:
            spent = ((r.get("settings") or {}).get("waiver_budget_used") or 0)
    return total - spent, total


def usage_trend(pid, season, con, last=3):
    """Snap share direction. Opportunity moves before production does, which is
    the whole point of getting there before the market."""
    rows = con.execute(
        "SELECT week, snaps, pts FROM actual WHERE pid=? AND season=? AND played=1 "
        "ORDER BY week", (pid, str(season))).fetchall()
    snaps = [r["snaps"] for r in rows if r["snaps"] is not None]
    if len(snaps) < 2:
        return None
    recent = statistics.fmean(snaps[-last:])
    earlier = statistics.fmean(snaps[:-last]) if len(snaps) > last else snaps[0]
    return {"recent_snaps": recent, "earlier_snaps": earlier,
            "delta": recent - earlier,
            "games": len(rows),
            "recent_ppg": statistics.fmean([r["pts"] for r in rows[-last:]])}


def beating_projection(pid, season, con, last=3):
    """Producing more than the market expected, over the last few weeks."""
    rows = con.execute(
        "SELECT a.week, a.pts actual, w.pts proj FROM actual a "
        "JOIN wproj w ON w.pid=a.pid AND w.season=a.season AND w.week=a.week "
        "WHERE a.pid=? AND a.season=? AND a.played=1 ORDER BY a.week DESC LIMIT ?",
        (pid, str(season), last)).fetchall()
    if not rows:
        return None
    return statistics.fmean((r["actual"] or 0) - (r["proj"] or 0) for r in rows)


def breakout_score(pid, season, con):
    """Combine opportunity and surprise into one comparable number.

    Snap trend is weighted above raw production because usage is stickier: a big
    game on four snaps is noise, four extra snaps a week is a role change.
    """
    u = usage_trend(pid, season, con)
    if not u:
        return None
    beat = beating_projection(pid, season, con) or 0.0
    score = 0.6 * (u["delta"] or 0) + 2.0 * beat + 0.5 * (u["recent_ppg"] or 0)
    return {"score": score, **u, "beat_projection": beat}


# A season's worth of added production that would justify spending most of the
# budget. ~9 points a week across a full season is a genuine league-winner.
BID_ANCHOR_POINTS = 160.0


def bid(gain_ppg, budget_left, weeks_left, top_target=True):
    """FAAB to offer, in dollars. Heuristic - see the module docstring.

    Value what the player adds over the REMAINING season, then spend that share
    of the budget. The first version anchored at 60 points and capped at 35%,
    which meant gains of 1.5, 3.0 and 6.0 points a week all returned the same
    $35 - the cap bound every case and the model discriminated nothing. A bid
    model that prices a marginal add like a league-winner is worse than no
    model, because it spends the budget on the first thing it sees.
    """
    if budget_left <= 0 or weeks_left <= 0 or gain_ppg <= 0:
        return 0
    season_gain = gain_ppg * weeks_left
    share = min(MAX_SINGLE_BID_SHARE, season_gain / BID_ANCHOR_POINTS)
    if not top_target:
        share *= 0.4
    return max(1, int(round(budget_left * share)))


def targets(cfg=None, season=None, top_n=8):
    cfg = cfg or load_config()
    con = DB.connect()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = season or st.get("season") or "2026"
    week = int(st.get("week") or 1)
    weeks_left = max(1, 18 - week)

    board, _, _ = build_board(cfg)
    by = {p["pid"]: p for p in board}
    owned = rostered_pids(cfg)
    free = [p for p in board if p["pid"] not in owned
            and p["pos"] in ("QB", "RB", "WR", "TE", "K", "DEF")]

    scored = []
    for p in free:
        b = breakout_score(p["pid"], season, con)
        if not b:
            continue
        scored.append((b["score"], p, b))
    scored.sort(key=lambda t: -t[0])

    budget_left, budget_total = our_budget(cfg)
    out = []
    for i, (sc, p, b) in enumerate(scored[:top_n]):
        gain = max(0.0, b["recent_ppg"] - (p.get("repl") or 0) / 17.0)
        out.append({
            "player": p, "score": sc, "recent_ppg": b["recent_ppg"],
            "snap_delta": b["delta"], "beat_projection": b["beat_projection"],
            "gain_ppg": gain,
            "bid": bid(gain, budget_left, weeks_left, top_target=(i < 3)),
        })
    return {"targets": out, "budget_left": budget_left,
            "budget_total": budget_total, "week": week,
            "weeks_left": weeks_left, "free_agents_scored": len(scored)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", action="store_true")
    ap.add_argument("--budget", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    if a.budget:
        left, total = our_budget(cfg)
        print(f"  FAAB: ${left} of ${total} remaining")
        return
    r = targets(cfg)
    print(f"  week {r['week']}, {r['weeks_left']} weeks left, "
          f"FAAB ${r['budget_left']}/{r['budget_total']}, "
          f"{r['free_agents_scored']} free agents with usage data")
    if not r["targets"]:
        print("  no waiver data yet - the season has not produced game logs")
        return
    print(f"\n  {'PLAYER':<22}{'POS':<5}{'ppg':>6}{'snapΔ':>7}{'vs proj':>9}{'bid':>6}")
    for t in r["targets"]:
        p = t["player"]
        print(f"  {p['name']:<22}{p['pos']:<5}{t['recent_ppg']:>6.1f}"
              f"{t['snap_delta']:>7.1f}{t['beat_projection']:>+9.1f}"
              f"{'$'+str(t['bid']):>6}")


if __name__ == "__main__":
    main()
