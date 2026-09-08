#!/usr/bin/env python3
"""Crawl preseason game logs. Stored separately from regular-season `actual`.

    python3 engine/pull_preseason.py 2026 2025 2024

Preseason snaps are taken by different players against different opposition than
the games that count. They are kept in their own table so nothing downstream can
mistake them for real production - `durability()` and the season-to-date average
would both be wrong if these landed in `actual`.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
from history import get, POSITIONS, _slim


def pull(season, weeks=range(1, 5), scoring="ppr"):
    con = DB.connect()
    key = f"pts_{scoring}"
    total = 0
    for wk in weeks:
        batch = []
        for pos in POSITIONS:
            rows = get(f"https://api.sleeper.com/stats/nfl/{season}/{wk}"
                       f"?season_type=pre&position[]={pos}&order_by=pts_ppr") or []
            for r in rows:
                st = r.get("stats") or {}
                pid = r.get("player_id")
                if pid:
                    batch.append((pid, str(season), wk, pos, r.get("team"),
                                  r.get("opponent"), st.get(key), st.get("off_snp"),
                                  int(st.get("gs") or 0), __import__("json").dumps(_slim(st))))
        con.executemany("INSERT OR REPLACE INTO preseason "
                        "(pid,season,week,pos,team,opponent,pts,snaps,started,stats) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
        con.commit()
        total += len(batch)
        if batch:
            print(f"  {season} pre wk {wk}: {len(batch)} rows")
    print(f"  -> {total} preseason rows for {season}")
    return total


if __name__ == "__main__":
    for s in (sys.argv[1:] or ["2026"]):
        print(f"===== {s} preseason =====")
        pull(s)
