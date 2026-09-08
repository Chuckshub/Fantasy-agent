#!/usr/bin/env python3
"""Historical game logs and matchup analysis.

    python3 engine/history.py --pull 2025        # fetch a season of game logs
    python3 engine/history.py --defense 2025     # points allowed by defense/position
    python3 engine/history.py --player "Nico Collins"
    python3 engine/history.py --matchup "Nico Collins" --vs KC

The honest statistics here matter more than the code. "How does this player do
against Kansas City" is usually a one- or two-game sample - a coin flip dressed
up as a read. What *is* reliable is **defense versus position**: every defense
faces 16-17 games worth of opposing running backs, and how many PPR points they
give up is a real, stable signal.

So the matchup model leans on defense-vs-position, uses the player's own recent
form as the baseline, and treats head-to-head history as a footnote that gets
shown but barely weighted. Sample sizes are printed everywhere so a two-game
"trend" is visible as exactly that.
"""
import sys, os, json, argparse, urllib.request, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
from value import build_board, load_config

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
UA = {"User-Agent": "Mozilla/5.0 (statking)"}
# Below this many head-to-head games, the split is decoration, not evidence.
MIN_H2H = 3
# How far a defensive matchup may move a weekly projection, either way.
MATCHUP_SWING = 0.18


def get(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ! {url} -> {e}", file=sys.stderr)
        return None


# The stats feed ships every ADP flavour on every row - a dozen keys per player
# per week that mean nothing in a game log and would bloat the store by an order
# of magnitude. Drop them at write time.
def _slim(st):
    return {k: v for k, v in st.items()
            if not k.startswith("adp_") and not k.startswith("pos_rank_")}


def pull_season(season, weeks=range(1, 19), scoring="ppr"):
    con = DB.connect()
    key = f"pts_{scoring}"
    total = 0
    for wk in weeks:
        rows_this_week = 0
        for pos in POSITIONS:
            rows = get(f"https://api.sleeper.com/stats/nfl/{season}/{wk}"
                       f"?season_type=regular&position[]={pos}&order_by=pts_ppr") or []
            batch = []
            for r in rows:
                st = r.get("stats") or {}
                pid = r.get("player_id")
                if not pid:
                    continue
                batch.append((
                    pid, str(season), wk, pos, r.get("team"), r.get("opponent"),
                    st.get(key), st.get("off_snp"), int(st.get("gs") or 0),
                    int((st.get("gp") or 0) > 0), json.dumps(_slim(st))))
            con.executemany(
                "INSERT OR REPLACE INTO actual (pid,season,week,pos,team,opponent,"
                "pts,snaps,started,played,stats) VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
            rows_this_week += len(batch)
        con.commit()
        total += rows_this_week
        print(f"  {season} wk {wk:>2}: {rows_this_week} rows")
    print(f"  -> {total} game-log rows for {season}")
    return total


# ---------------------------------------------------------------- analysis
def defense_vs_position(season, con=None, min_games=6):
    """PPR points a defense allows per game to each position, with a z-score.

    Only players who actually played are counted, and each (defense, position)
    cell aggregates per week so a team's total allowed - not one player's day -
    is what gets measured.
    """
    con = con or DB.connect()
    rows = con.execute(
        "SELECT opponent AS d, pos, week, SUM(pts) AS allowed FROM actual "
        "WHERE season=? AND played=1 AND opponent IS NOT NULL AND pts IS NOT NULL "
        "GROUP BY opponent, pos, week", (str(season),)).fetchall()
    cell = {}
    for r in rows:
        cell.setdefault((r["d"], r["pos"]), []).append(r["allowed"])
    out = {}
    for (d, pos), vals in cell.items():
        if len(vals) < min_games:
            continue
        out.setdefault(pos, {})[d] = {"ppg": sum(vals) / len(vals), "n": len(vals)}
    for pos, teams in out.items():
        xs = [v["ppg"] for v in teams.values()]
        mu = statistics.fmean(xs)
        sd = statistics.pstdev(xs) or 1.0
        for d, v in teams.items():
            v["z"] = (v["ppg"] - mu) / sd      # positive = generous defense
            v["mu"] = mu
    return out


def player_games(pid, con=None, season=None):
    con = con or DB.connect()
    q = ("SELECT season, week, team, opponent, pts, snaps, started FROM actual "
         "WHERE pid=? AND played=1 AND pts IS NOT NULL")
    args = [pid]
    if season:
        q += " AND season=?"
        args.append(str(season))
    return con.execute(q + " ORDER BY season, week", args).fetchall()


def player_profile(pid, con=None):
    g = player_games(pid, con)
    if not g:
        return None
    pts = [r["pts"] for r in g]
    byopp = {}
    for r in g:
        byopp.setdefault(r["opponent"], []).append(r["pts"])
    h2h = {o: {"n": len(v), "ppg": sum(v) / len(v)} for o, v in byopp.items()}
    last5 = pts[-5:]
    return {
        "games": len(pts),
        "ppg": statistics.fmean(pts),
        "sd": statistics.pstdev(pts) if len(pts) > 1 else 0.0,
        "floor": min(pts), "ceiling": max(pts),
        "last5_ppg": statistics.fmean(last5) if last5 else None,
        "boom_rate": sum(1 for x in pts if x >= statistics.fmean(pts) * 1.5) / len(pts),
        "bust_rate": sum(1 for x in pts if x <= statistics.fmean(pts) * 0.5) / len(pts),
        "h2h": h2h,
    }


def matchup_edge(pid, pos, opponent, dvp, con=None):
    """Multiplier for a weekly projection, given who the player faces.

    Driven by defense-vs-position, because that is the part with a real sample.
    Head-to-head history is reported but only nudges the number, and only when
    there are at least MIN_H2H games behind it.
    """
    cell = (dvp.get(pos) or {}).get(opponent)
    if not cell:
        return 1.0, "no matchup data"
    z = max(-2.0, min(2.0, cell["z"]))
    mult = 1.0 + MATCHUP_SWING * (z / 2.0)
    why = (f"{opponent} allows {cell['ppg']:.1f} PPR/gm to {pos} "
           f"(z {cell['z']:+.2f} over {cell['n']} gms)")
    prof = player_profile(pid, con)
    if prof:
        h = prof["h2h"].get(opponent)
        if h and h["n"] >= MIN_H2H and prof["ppg"] > 0:
            ratio = h["ppg"] / prof["ppg"]
            mult *= 1.0 + 0.25 * max(-0.4, min(0.4, ratio - 1.0))
            why += f"; h2h {h['n']} gms at {h['ppg']:.1f} vs {prof['ppg']:.1f} career"
        elif h:
            why += f"; h2h only {h['n']} gm(s) - ignored"
    return mult, why


# -------------------------------------------------------------------- cli
def _find(name, board):
    hits = [p for p in board if p["name"].lower() == name.strip().lower()]
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", metavar="SEASON")
    ap.add_argument("--weeks", default="1-18")
    ap.add_argument("--defense", metavar="SEASON")
    ap.add_argument("--player")
    ap.add_argument("--matchup")
    ap.add_argument("--vs")
    ap.add_argument("--season", default="2025")
    a = ap.parse_args()

    if a.pull:
        lo, _, hi = a.weeks.partition("-")
        pull_season(a.pull, range(int(lo), int(hi or lo) + 1))
        return

    con = DB.connect()
    if a.defense:
        dvp = defense_vs_position(a.defense, con)
        for pos in ("QB", "RB", "WR", "TE"):
            teams = dvp.get(pos) or {}
            if not teams:
                continue
            rank = sorted(teams.items(), key=lambda kv: -kv[1]["ppg"])
            print(f"\n{pos}: most generous defenses ({a.defense}, league avg "
                  f"{rank[0][1]['mu']:.1f} PPR/gm)")
            for d, v in rank[:5]:
                print(f"   {d:<4}{v['ppg']:>7.1f}  z {v['z']:+5.2f}  ({v['n']} gms)")
            print(f"{pos}: toughest")
            for d, v in rank[-5:]:
                print(f"   {d:<4}{v['ppg']:>7.1f}  z {v['z']:+5.2f}  ({v['n']} gms)")
        return

    cfg = load_config()
    board, _, _ = build_board(cfg)
    name = a.player or a.matchup
    if not name:
        ap.print_help()
        return
    p = _find(name, board)
    if not p:
        print(f"'{name}' not on the board")
        return
    prof = player_profile(p["pid"], con)
    if not prof:
        print(f"no game logs for {p['name']} - run --pull first")
        return
    print(f"{p['name']}  ({p['pos']} {p['team']})")
    print(f"  {prof['games']} games   {prof['ppg']:.1f} PPR/gm   sd {prof['sd']:.1f}"
          f"   floor {prof['floor']:.1f}   ceiling {prof['ceiling']:.1f}")
    print(f"  last 5: {prof['last5_ppg']:.1f}/gm   "
          f"boom {prof['boom_rate']:.0%}   bust {prof['bust_rate']:.0%}")
    solid = {o: h for o, h in prof["h2h"].items() if h["n"] >= MIN_H2H}
    print(f"\n  opponents faced {MIN_H2H}+ times (the only ones worth reading):")
    if solid:
        for o, h in sorted(solid.items(), key=lambda kv: -kv[1]["ppg"]):
            print(f"     vs {o:<4}{h['ppg']:>7.1f}/gm over {h['n']} games")
    else:
        print("     none - every head-to-head sample is 1-2 games and tells you nothing")
    if a.vs:
        dvp = defense_vs_position(a.season, con)
        mult, why = matchup_edge(p["pid"], p["pos"], a.vs.upper(), dvp, con)
        print(f"\n  vs {a.vs.upper()}: projection x{mult:.3f}")
        print(f"     {why}")


if __name__ == "__main__":
    main()
