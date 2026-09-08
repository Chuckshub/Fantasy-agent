#!/usr/bin/env python3
"""Pull all draft-relevant data from Sleeper. Zero third-party deps."""
import json, os, sys, urllib.request, urllib.error, time

MAX_PLAYERS_AGE_H = 6      # injury tags move daily; never trust one older

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]
UA = {"User-Agent": "Mozilla/5.0 (statking draft assistant)"}


def get(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ! FAILED {url}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))


def save(obj, name):
    path = os.path.join(DATA, name)
    with open(path, "w") as f:
        json.dump(obj, f)
    return path


def pull_by_position(kind, season, season_type="regular"):
    """kind: 'projections' or 'stats'. Returns {player_id: stats_dict}."""
    merged = {}
    for pos in POSITIONS:
        url = (f"https://api.sleeper.com/{kind}/nfl/{season}"
               f"?season_type={season_type}&position[]={pos}&order_by=pts_half_ppr")
        rows = get(url)
        if not rows:
            print(f"  {kind} {season} {pos}: none")
            continue
        n = 0
        for row in rows:
            pid = row.get("player_id")
            st = row.get("stats") or {}
            if not pid or not st:
                continue
            # a player can appear under multiple position queries; keep richest
            if pid not in merged or len(st) > len(merged[pid]["stats"]):
                merged[pid] = {"stats": st, "position": pos,
                               "team": row.get("team"),
                               "opponent": row.get("opponent"),
                               "player": row.get("player") or {}}
            n += 1
        print(f"  {kind} {season} {pos}: {n} rows")
    return merged


def main():
    os.makedirs(DATA, exist_ok=True)
    season = 2026
    print("== Sleeper NFL state ==")
    state = get("https://api.sleeper.app/v1/state/nfl")
    if state:
        save(state, "state.json")
        print(f"  season={state.get('season')} type={state.get('season_type')} week={state.get('week')}")

    print(f"== {season} projections ==")
    proj = pull_by_position("projections", season)
    save(proj, "projections_2026.json")
    print(f"  -> {len(proj)} players with projections")

    print(f"== {season-1} actual stats ==")
    stats = pull_by_position("stats", season - 1)
    save(stats, "stats_2025.json")
    print(f"  -> {len(stats)} players with 2025 stats")

    print("== players master ==")
    # This file carries injury_status, which drives both the draft board's INJ
    # flags and lineup.py's hard zeroes. It used to download only when ABSENT,
    # so once created it never refreshed again - injury designations silently
    # froze on the day of the first fetch. Refresh whenever it is older than
    # MAX_PLAYERS_AGE_H, and swap atomically so a partial write can never
    # replace a working file.
    dst = os.path.join(DATA, "players_nfl.json")
    age_h = ((time.time() - os.path.getmtime(dst)) / 3600.0
             if os.path.exists(dst) else 1e9)
    if age_h > MAX_PLAYERS_AGE_H:
        players = get("https://api.sleeper.app/v1/players/nfl")
        if players and len(players) > 5000:
            tmp = dst + ".tmp"
            with open(tmp, "w") as f:
                json.dump(players, f)
            json.load(open(tmp))          # it must parse before it is trusted
            os.replace(tmp, dst)
            print(f"  -> refreshed ({len(players):,} players, was {age_h:.1f}h old)")
        else:
            print(f"  ! refusing to overwrite with {len(players or [])} records")
    else:
        print(f"  -> current ({age_h:.1f}h old)")

    print("\nDone.")


if __name__ == "__main__":
    main()
