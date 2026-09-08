#!/usr/bin/env python3
"""Pull league state from Sleeper into the local store.

    python3 engine/track.py --sync     snapshot everything that changed
    python3 engine/track.py --status   what the store currently knows
    python3 engine/track.py --note "..." [--pid X] [--kind bet]

Read-only against Sleeper. Safe to run as often as you like; each run writes a
new dated snapshot rather than overwriting history.
"""
import sys, os, json, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
from sync import get, API
from value import build_board, load_config


def nfl_state():
    return get("https://api.sleeper.app/v1/state/nfl") or {}


def sync_players_and_board(con, snap, cfg):
    """Board rows carry name/pos/team/bye/proj/vorp/adp - store both facets."""
    board, _, _ = build_board(cfg)
    now = int(time.time())
    season = cfg.get("season") or nfl_state().get("season")
    con.executemany(
        "INSERT INTO player (pid,name,pos,team,age,years_exp,bye,updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(pid) DO UPDATE SET "
        "name=excluded.name, pos=excluded.pos, team=excluded.team, "
        "age=excluded.age, years_exp=excluded.years_exp, bye=excluded.bye, "
        "updated_ts=excluded.updated_ts",
        [(p["pid"], p["name"], p["pos"], p.get("team"), p.get("age"),
          p.get("years_exp"), p.get("bye"), now) for p in board])
    con.executemany(
        "INSERT OR REPLACE INTO projection "
        "(snapshot_id,pid,season,week,pts,vorp,adp) VALUES (?,?,?,NULL,?,?,?)",
        [(snap, p["pid"], season, p.get("proj"), p.get("vorp"), p.get("adp"))
         for p in board])
    con.commit()
    return len(board)


def sync_managers(con, league_id):
    users = get(f"{API}/league/{league_id}/users") or []
    rosters = get(f"{API}/league/{league_id}/rosters") or []
    roster_of = {r.get("owner_id"): r.get("roster_id") for r in rosters}
    con.executemany(
        "INSERT INTO manager (owner_id,roster_id,username,team_name) VALUES (?,?,?,?) "
        "ON CONFLICT(owner_id) DO UPDATE SET roster_id=excluded.roster_id, "
        "username=excluded.username, team_name=excluded.team_name",
        [(u["user_id"], roster_of.get(u["user_id"]), u.get("display_name"),
          (u.get("metadata") or {}).get("team_name")) for u in users])
    con.commit()
    return len(users), rosters


def sync_ownership(con, snap, rosters, season, week):
    rows = []
    for r in rosters:
        starters = set(r.get("starters") or [])
        for pid in (r.get("players") or []):
            rows.append((snap, season, week, r.get("roster_id"),
                         r.get("owner_id"), pid, 1 if pid in starters else 0))
    con.executemany(
        "INSERT OR REPLACE INTO ownership "
        "(snapshot_id,season,week,roster_id,owner_id,pid,is_starter) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def sync_transactions(con, league_id, season, upto_week):
    n = 0
    for wk in range(1, max(1, upto_week) + 1):
        for t in (get(f"{API}/league/{league_id}/transactions/{wk}") or []):
            con.execute(
                "INSERT OR REPLACE INTO txn "
                "(txn_id,season,week,type,status,ts,payload) VALUES (?,?,?,?,?,?,?)",
                (t.get("transaction_id"), season, wk, t.get("type"),
                 t.get("status"), t.get("status_updated"), json.dumps(t)))
            n += 1
    con.commit()
    return n


def sync_matchups(con, league_id, season, upto_week):
    n = 0
    for wk in range(1, max(0, upto_week) + 1):
        for m in (get(f"{API}/league/{league_id}/matchups/{wk}") or []):
            con.execute(
                "INSERT OR REPLACE INTO matchup "
                "(season,week,roster_id,points,starters,players) VALUES (?,?,?,?,?,?)",
                (season, wk, m.get("roster_id"), m.get("points"),
                 json.dumps(m.get("starters")), json.dumps(m.get("players"))))
            n += 1
    con.commit()
    return n


def sync_draft(con, draft_id):
    picks = get(f"{API}/draft/{draft_id}/picks") or []
    con.executemany(
        "INSERT OR REPLACE INTO draft_pick "
        "(draft_id,pick_no,round,slot,pid,picked_by) VALUES (?,?,?,?,?,?)",
        [(draft_id, p.get("pick_no"), p.get("round"), p.get("draft_slot"),
          p.get("player_id"), p.get("picked_by")) for p in picks])
    con.commit()
    return len(picks)


def cmd_sync():
    cfg = load_config()
    con = DB.connect()
    st = nfl_state()
    season = st.get("season") or "2026"
    week = int(st.get("week") or 0)
    snap = DB.new_snapshot(con, "sync", season, week, "track.py --sync")
    print(f"snapshot {snap}  season {season}  week {week}")

    print(f"  board/projections : {sync_players_and_board(con, snap, cfg)} players")
    nu, rosters = sync_managers(con, cfg["league_id"])
    print(f"  managers          : {nu}")
    print(f"  ownership rows    : {sync_ownership(con, snap, rosters, season, week)}")
    print(f"  draft picks       : {sync_draft(con, cfg['draft_id'])}")
    print(f"  transactions      : {sync_transactions(con, cfg['league_id'], season, week)}")
    print(f"  matchups          : {sync_matchups(con, cfg['league_id'], season, week)}")


def cmd_status():
    con = DB.connect()
    print(f"db: {DB.DB_PATH}")
    for k, v in DB.summary(con).items():
        print(f"  {k:<12}{v:>8}")
    last = con.execute("SELECT * FROM snapshot ORDER BY id DESC LIMIT 5").fetchall()
    if last:
        print("\n  recent snapshots:")
        for r in last:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            print(f"    #{r['id']:<4}{when}  {r['kind']}  season={r['season']} week={r['week']}")
    notes = con.execute(
        "SELECT * FROM note WHERE resolved=0 ORDER BY id DESC LIMIT 10").fetchall()
    if notes:
        print("\n  open notes:")
        for n in notes:
            print(f"    [{n['kind']}/{n['horizon']}] {n['pid'] or '-'}: {n['text'][:80]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--note")
    ap.add_argument("--pid")
    ap.add_argument("--kind", default="misc")
    ap.add_argument("--horizon", default="season")
    a = ap.parse_args()
    if a.note:
        con = DB.connect()
        DB.add_note(con, a.kind, a.note, a.pid, a.horizon)
        print("noted.")
    elif a.sync:
        cmd_sync()
    else:
        cmd_status()
