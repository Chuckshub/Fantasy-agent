#!/usr/bin/env python3
"""Crawl Sleeper's historical weekly projections so they can be graded.

    python3 engine/pull_wproj.py 2025 [2024 ...]

Knowing a projection was 14.2 is only useful next to what actually happened.
This stores the former; `actual` already holds the latter.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
from history import get, POSITIONS


def pull(season, weeks=range(1, 19), scoring="ppr"):
    con = DB.connect()
    key = f"pts_{scoring}"
    total = 0
    for wk in weeks:
        batch = []
        for pos in POSITIONS:
            rows = get(f"https://api.sleeper.com/projections/nfl/{season}/{wk}"
                       f"?season_type=regular&position[]={pos}&order_by=pts_ppr") or []
            for r in rows:
                pid = r.get("player_id")
                pts = (r.get("stats") or {}).get(key)
                if pid and pts is not None:
                    batch.append((pid, str(season), wk, pos, r.get("team"),
                                  r.get("opponent"), pts))
        con.executemany("INSERT OR REPLACE INTO wproj "
                        "(pid,season,week,pos,team,opponent,pts) VALUES (?,?,?,?,?,?,?)",
                        batch)
        con.commit()
        total += len(batch)
        print(f"  {season} wk {wk:>2}: {len(batch)} projections")
    print(f"  -> {total} weekly projections for {season}")


if __name__ == "__main__":
    for s in (sys.argv[1:] or ["2025"]):
        print(f"===== {s} =====")
        pull(s)
    print("WPROJ COMPLETE")
