#!/usr/bin/env python3
"""Keep the current season's data current. Run by the autonomous agent.

    python3 engine/refresh.py            # everything that is stale
    python3 engine/refresh.py --week 5   # force a specific week

`track.py --sync` refreshes league state - rosters, transactions, matchups - but
it never touched the three tables the model actually consumes:

  actual  weekly game logs, the ground truth and the season-to-date average
  wproj   weekly projections, 0.8 of every composite estimate
  game    betting lines, which are only posted a few days before kickoff

All three were populated once, by hand, during development. Left that way the
store would have frozen at the 2025 season while quietly appearing healthy, and
`composite()` would have silently degraded to projection-only for the whole
year. This closes that hole.
"""
import sys, os, argparse, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import history as HI
import pull_wproj as WP
import pull_games as PG
import pull_preseason as PP
import fetch as FETCH
from sync import get


def nfl_state():
    return get("https://api.sleeper.app/v1/state/nfl") or {}


def refresh(season=None, week=None, lookback=2, verbose=True):
    st = nfl_state()
    season = season or st.get("season") or str(datetime.date.today().year)
    week = week or int(st.get("week") or 1)
    con = DB.connect()
    out = {}

    def say(m):
        if verbose:
            print(m)

    # Completed weeks: pull actuals for anything recent, since stat corrections
    # land for days after a game and a week pulled live is not yet final.
    lo = max(1, week - lookback)
    say(f"season {season}, current week {week}")
    say(f"  actuals   weeks {lo}-{week}")
    HI.pull_season(season, range(lo, week + 1))

    # Projections for the week being played and the one after it - the lineup
    # for next week is set before this week's results are final.
    say(f"  wproj     weeks {week}-{week + 1}")
    WP.pull(season, range(week, min(week + 2, 19)))

    # While it is still preseason, those are the only 2026 games that exist -
    # and for a rookie they are the only evidence of any kind. Stored in their
    # own table so nothing mistakes an exhibition snap for a real one.
    if str(st.get("season_type") or "").startswith("pre") or week <= 4:
        say("  preseason logs")
        PP.pull(season)

    # The season-long board: projections, ADP and injury designations. This was
    # the gap that let the board go 20 hours stale before a live draft - the
    # weekly tables refreshed, the file the board is actually built from did not.
    say("  season projections + players master")
    try:
        FETCH.main()
    except Exception as e:
        say(f"  ! projection refresh failed (keeping existing): {e}")

    # Lines are posted a few days out and move until kickoff.
    say("  game lines (nflverse)")
    PG.pull(min_season=int(season))

    for t in ("actual", "wproj", "game", "preseason"):
        out[t] = con.execute(
            f"SELECT COUNT(*) c FROM {t} WHERE season=?", (str(season),)
        ).fetchone()["c"]
    say(f"  -> {season}: actual {out['actual']:,}  wproj {out['wproj']:,}  "
        f"game {out['game']:,}  preseason {out['preseason']:,}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season")
    ap.add_argument("--week", type=int)
    ap.add_argument("--lookback", type=int, default=2)
    a = ap.parse_args()
    refresh(a.season, a.week, a.lookback)
