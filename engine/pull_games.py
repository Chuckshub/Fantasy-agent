#!/usr/bin/env python3
"""Load historical game context - Vegas lines, venue, weather - from nflverse.

    python3 engine/pull_games.py

The market's implied team total is the single strongest public predictor of
team scoring, and it prices in injuries, weather and game script hours before
any stat feed reflects them. nflverse publishes closing spreads and totals back
to 1999 as a plain CSV, which is what makes it possible to *backtest* rather
than merely believe.
"""
import sys, os, csv, io, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB

URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

# nflverse carries historical franchise codes; Sleeper uses current ones.
TEAM_FIX = {"OAK": "LV", "SD": "LAC", "STL": "LAR", "LA": "LAR",
            "WSH": "WAS", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE",
            "HST": "HOU", "SL": "LAR", "JAC": "JAX"}


def norm(t):
    t = (t or "").strip().upper()
    return TEAM_FIX.get(t, t)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pull(min_season=2019):
    with urllib.request.urlopen(URL, timeout=90) as r:
        text = r.read().decode("utf-8", "replace")
    rows, skipped = [], 0
    for g in csv.DictReader(io.StringIO(text)):
        try:
            season = int(g["season"])
        except (TypeError, ValueError):
            continue
        if season < min_season or g.get("game_type") != "REG":
            continue
        if g.get("total_line") in (None, "") or g.get("spread_line") in (None, ""):
            skipped += 1
        rows.append((str(season), int(g["week"]), norm(g["home_team"]),
                     norm(g["away_team"]), num(g.get("home_score")),
                     num(g.get("away_score")), num(g.get("spread_line")),
                     num(g.get("total_line")), g.get("roof"), g.get("surface"),
                     num(g.get("temp")), num(g.get("wind"))))
    con = DB.connect()
    con.executemany(
        "INSERT OR REPLACE INTO game (season,week,home_team,away_team,home_score,"
        "away_score,spread_line,total_line,roof,surface,temp,wind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    print(f"  loaded {len(rows)} regular-season games from {min_season}")
    print(f"  {skipped} without a line (older/odd fixtures)")
    have = con.execute("SELECT season, COUNT(*) n, "
                       "SUM(CASE WHEN total_line IS NOT NULL THEN 1 ELSE 0 END) withline "
                       "FROM game GROUP BY season ORDER BY season").fetchall()
    for r in have:
        print(f"    {r['season']}  {r['n']:>3} games, {r['withline']:>3} with a line")


if __name__ == "__main__":
    pull()
