#!/usr/bin/env python3
"""nflverse play-by-play into DuckDB, and player-weeks derived from it.

    python3 engine/nflverse.py --ingest 2016-2025    pull play-by-play
    python3 engine/nflverse.py --build               derive player-week rows
    python3 engine/nflverse.py --status              what the store holds

Why this exists alongside `data/statking.db`. The SQLite store holds what
Sleeper reports: a fantasy points total per player-week. That is an *outcome*
and nothing else. Play-by-play holds the process that produced it - targets,
air yards, carries inside the ten, whether a team was ahead and stopped
throwing - and process is what a projection can actually be built on. A
receiver who saw eleven targets and caught three had a bad week; a receiver who
saw two targets and caught two had a bad role, and only one of those predicts
next week.

DuckDB rather than SQLite because this is columnar analytics over millions of
plays - group-bys across ten seasons that SQLite would take minutes over run in
under a second - and because it reads the parquet releases directly over HTTPS,
so ingest is a SQL statement rather than a download-and-parse script.

Scoring is this league's, not a generic PPR. Fantasy points are recomputed from
the stat line using `scoring.league_points`, for the same reason the weekly
projections had to be: Sleeper's generic pts_ppr scores passing yards at 0.05
and this league scores them at 0.04, which is worth about two points a game to
every quarterback.
"""
import sys, os, json, argparse, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scoring as SC
from value import load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(HERE, "data", "nfl.duckdb")
PBP_URL = ("https://github.com/nflverse/nflverse-data/releases/download/pbp/"
           "play_by_play_{season}.parquet")
ROSTER_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
              "rosters/roster_{season}.parquet")
PLAYERS_URL = ("https://github.com/nflverse/nflverse-data/releases/download/"
               "players/players.parquet")


def connect(read_only=False):
    import duckdb
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = duckdb.connect(DB_PATH, read_only=read_only)
    con.execute("INSTALL httpfs; LOAD httpfs;")
    return con


def parse_seasons(spec):
    """'2016-2025' or '2024,2025' or '2025' -> [ints]"""
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


# ------------------------------------------------------------------- ingest
def ingest(seasons, con=None, verbose=True):
    """Pull play-by-play for each season into the `pbp` table.

    One season at a time and replace-per-season rather than one giant append,
    so a re-run repairs a season without duplicating the other nine, and so a
    mid-season refresh of the current year is a normal operation rather than a
    rebuild.
    """
    con = con or connect()
    con.execute("""CREATE TABLE IF NOT EXISTS pbp_seasons
                   (season INTEGER PRIMARY KEY, rows BIGINT, loaded_at TIMESTAMP)""")
    first = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name='pbp'").fetchone()[0] == 0
    for s in seasons:
        url = PBP_URL.format(season=s)
        if verbose:
            print(f"  {s}: reading {url.rsplit('/', 1)[-1]} ...", flush=True)
        try:
            if first:
                con.execute(f"CREATE TABLE pbp AS SELECT * FROM read_parquet('{url}')")
                first = False
            else:
                con.execute(f"DELETE FROM pbp WHERE season = {s}")
                con.execute(f"INSERT INTO pbp SELECT * FROM read_parquet('{url}')")
        except Exception as e:
            print(f"  ! {s} failed: {e}", file=sys.stderr)
            continue
        n = con.execute(f"SELECT count(*) FROM pbp WHERE season={s}").fetchone()[0]
        con.execute("INSERT OR REPLACE INTO pbp_seasons VALUES (?,?,?)",
                    (s, n, datetime.datetime.now()))
        if verbose:
            print(f"      {n:,} plays")
    return con


# ------------------------------------------------------- derived player-weeks
# One row per player per week, with the stat line a fantasy score needs and the
# usage that predicts next week's stat line. Built by unioning the three roles a
# play can credit - passer, rusher, receiver - and summing per player-week,
# because nflverse stores a play once with a column per role rather than a row
# per participant.
BUILD_SQL = """
CREATE OR REPLACE TABLE player_week AS
WITH plays AS (
  SELECT * FROM pbp WHERE season_type = 'REG' AND week IS NOT NULL
),
pass AS (
  SELECT season, week, passer_player_id AS pid, passer_player_name AS name,
         posteam AS team, defteam AS opp,
         SUM(COALESCE(passing_yards,0))                       AS pass_yd,
         SUM(CASE WHEN pass_touchdown=1 THEN 1 ELSE 0 END)    AS pass_td,
         SUM(CASE WHEN interception=1 THEN 1 ELSE 0 END)      AS pass_int,
         SUM(CASE WHEN complete_pass=1 THEN 1 ELSE 0 END)     AS pass_cmp,
         SUM(CASE WHEN pass_attempt=1 THEN 1 ELSE 0 END)      AS pass_att,
         SUM(CASE WHEN sack=1 THEN 1 ELSE 0 END)              AS sacks_taken
  FROM plays WHERE passer_player_id IS NOT NULL
  GROUP BY 1,2,3,4,5,6
),
rush AS (
  SELECT season, week, rusher_player_id AS pid, rusher_player_name AS name,
         posteam AS team, defteam AS opp,
         SUM(COALESCE(rushing_yards,0))                       AS rush_yd,
         SUM(CASE WHEN rush_touchdown=1 THEN 1 ELSE 0 END)    AS rush_td,
         SUM(CASE WHEN rush_attempt=1 THEN 1 ELSE 0 END)      AS rush_att,
         SUM(CASE WHEN rush_attempt=1 AND yardline_100<=10 THEN 1 ELSE 0 END) AS rz_carries
  FROM plays WHERE rusher_player_id IS NOT NULL
  GROUP BY 1,2,3,4,5,6
),
recv AS (
  SELECT season, week, receiver_player_id AS pid, receiver_player_name AS name,
         posteam AS team, defteam AS opp,
         SUM(COALESCE(receiving_yards,0))                     AS rec_yd,
         SUM(CASE WHEN pass_touchdown=1 AND complete_pass=1 THEN 1 ELSE 0 END) AS rec_td,
         SUM(CASE WHEN complete_pass=1 THEN 1 ELSE 0 END)     AS rec,
         SUM(CASE WHEN pass_attempt=1 THEN 1 ELSE 0 END)      AS targets,
         SUM(COALESCE(air_yards,0))                           AS air_yards,
         SUM(COALESCE(yards_after_catch,0))                   AS yac,
         SUM(CASE WHEN pass_attempt=1 AND yardline_100<=20 THEN 1 ELSE 0 END) AS rz_targets
  FROM plays WHERE receiver_player_id IS NOT NULL
  GROUP BY 1,2,3,4,5,6
),
fumbles AS (
  SELECT season, week, fumbled_1_player_id AS pid,
         SUM(CASE WHEN fumble_lost=1 THEN 1 ELSE 0 END) AS fum_lost
  FROM plays WHERE fumbled_1_player_id IS NOT NULL
  GROUP BY 1,2,3
),
team_pass AS (
  SELECT season, week, posteam AS team,
         SUM(CASE WHEN pass_attempt=1 THEN 1 ELSE 0 END) AS team_targets,
         SUM(COALESCE(air_yards,0))                      AS team_air_yards,
         SUM(CASE WHEN rush_attempt=1 THEN 1 ELSE 0 END) AS team_carries
  FROM plays WHERE posteam IS NOT NULL GROUP BY 1,2,3
),
ids AS (
  SELECT season, week, pid, name, team, opp FROM pass
  UNION SELECT season, week, pid, name, team, opp FROM rush
  UNION SELECT season, week, pid, name, team, opp FROM recv
)
SELECT
  i.season, i.week, i.pid, i.name, i.team, i.opp,
  COALESCE(p.pass_yd,0) AS pass_yd, COALESCE(p.pass_td,0) AS pass_td,
  COALESCE(p.pass_int,0) AS pass_int, COALESCE(p.pass_cmp,0) AS pass_cmp,
  COALESCE(p.pass_att,0) AS pass_att, COALESCE(p.sacks_taken,0) AS sacks_taken,
  COALESCE(r.rush_yd,0) AS rush_yd, COALESCE(r.rush_td,0) AS rush_td,
  COALESCE(r.rush_att,0) AS rush_att, COALESCE(r.rz_carries,0) AS rz_carries,
  COALESCE(c.rec_yd,0) AS rec_yd, COALESCE(c.rec_td,0) AS rec_td,
  COALESCE(c.rec,0) AS rec, COALESCE(c.targets,0) AS targets,
  COALESCE(c.air_yards,0) AS air_yards, COALESCE(c.yac,0) AS yac,
  COALESCE(c.rz_targets,0) AS rz_targets,
  COALESCE(f.fum_lost,0) AS fum_lost,
  COALESCE(t.team_targets,0) AS team_targets,
  COALESCE(t.team_carries,0) AS team_carries,
  CASE WHEN COALESCE(t.team_targets,0)>0
       THEN COALESCE(c.targets,0)::DOUBLE / t.team_targets END AS target_share,
  CASE WHEN COALESCE(t.team_air_yards,0)>0
       THEN COALESCE(c.air_yards,0)::DOUBLE / t.team_air_yards END AS air_yards_share,
  CASE WHEN COALESCE(t.team_carries,0)>0
       THEN COALESCE(r.rush_att,0)::DOUBLE / t.team_carries END AS carry_share
FROM ids i
LEFT JOIN pass p USING (season, week, pid, name, team, opp)
LEFT JOIN rush r USING (season, week, pid, name, team, opp)
LEFT JOIN recv c USING (season, week, pid, name, team, opp)
LEFT JOIN fumbles f ON f.season=i.season AND f.week=i.week AND f.pid=i.pid
LEFT JOIN team_pass t ON t.season=i.season AND t.week=i.week AND t.team=i.team
WHERE i.pid IS NOT NULL
"""


def build(con=None, verbose=True):
    con = con or connect()
    if verbose:
        print("  deriving player_week from pbp ...", flush=True)
    con.execute(BUILD_SQL)
    n = con.execute("SELECT count(*) FROM player_week").fetchone()[0]
    if verbose:
        print(f"      {n:,} player-weeks")
    add_fantasy_points(con, verbose=verbose)
    build_xref(con, verbose=verbose)
    return con


def add_fantasy_points(con, cfg=None, verbose=True):
    """Score every player-week under THIS league's rules.

    Done in SQL from the league's own weights rather than with a hardcoded PPR
    formula, so that a scoring change in the league is a config change here and
    every historical week is rescored consistently with every future one.
    """
    cfg = cfg or load_config()
    ss = cfg.get("scoring_settings") or {}

    def w(k, default=0.0):
        return float(ss.get(k, default))

    expr = (
        f"{w('pass_yd')}*pass_yd + {w('pass_td')}*pass_td + {w('pass_int')}*pass_int "
        f"+ {w('rush_yd')}*rush_yd + {w('rush_td')}*rush_td "
        f"+ {w('rec_yd')}*rec_yd + {w('rec_td')}*rec_td + {w('rec')}*rec "
        f"+ {w('fum_lost')}*fum_lost"
    )
    con.execute("ALTER TABLE player_week DROP COLUMN IF EXISTS fpts")
    con.execute(f"ALTER TABLE player_week ADD COLUMN fpts DOUBLE")
    con.execute(f"UPDATE player_week SET fpts = {expr}")
    if verbose:
        row = con.execute(
            "SELECT count(*), round(avg(fpts),2), round(max(fpts),1) "
            "FROM player_week WHERE fpts > 0").fetchone()
        print(f"      scored {row[0]:,} scoring weeks, mean {row[1]}, max {row[2]}")


def build_xref(con=None, verbose=True):
    """Map Sleeper player ids to nflverse GSIS ids.

    These two worlds do not share an identifier. Sleeper calls Trevor Lawrence
    `4984`; nflverse calls him `00-0036971`, and joining the two tables on `pid`
    silently produces zero matches - which is exactly what happened here first
    time, and it did not look like an error. It looked like a fitted model in
    which every single player had scored zero, and the only tell was that every
    quantile came back 0.00. A join that can fail this quietly deserves its own
    table and its own row count.

    Sleeper's own player file carries `gsis_id` for the players who have ever
    appeared in an NFL game, which is every player a fantasy projection is
    written about.
    """
    con = con or connect()
    path = os.path.join(HERE, "data", "players_nfl.json")
    players = json.load(open(path))

    # nflverse's own player table, for the second and third routes below.
    con.execute(f"""CREATE OR REPLACE TABLE nfl_players AS
                    SELECT * FROM read_parquet('{PLAYERS_URL}')""")

    # Sleeper carries gsis_id for only about a third of the players a
    # projection is written about, so one route is not enough. Three are used,
    # in descending order of trust, and a later route never overwrites an
    # earlier one:
    #   1. Sleeper's own gsis_id, where it exists.
    #   2. espn_id, which both sides carry for most active players.
    #   3. name + position, which is the weakest and is why it is last - two
    #      players can share a name, so ambiguous matches are dropped entirely
    #      rather than guessed at.
    rows = {}
    for pid, v in players.items():
        g = (v or {}).get("gsis_id")
        if g:
            rows[pid] = (pid, g, (v or {}).get("full_name") or "",
                         (v or {}).get("position") or "", "gsis")

    by_espn, by_name = {}, {}
    for gid, espn, disp, pos in con.execute(
            "SELECT gsis_id, espn_id, display_name, position FROM nfl_players "
            "WHERE gsis_id IS NOT NULL").fetchall():
        if espn:
            by_espn[str(espn)] = gid
        key = (_norm(disp), (pos or "").upper())
        by_name.setdefault(key, []).append(gid)

    for pid, v in players.items():
        if pid in rows:
            continue
        v = v or {}
        espn = v.get("espn_id")
        g = by_espn.get(str(espn)) if espn else None
        route = "espn"
        if not g:
            cands = by_name.get((_norm(v.get("full_name")),
                                 (v.get("position") or "").upper()))
            # Exactly one, or it is not a match worth making.
            g = cands[0] if cands and len(cands) == 1 else None
            route = "name"
        if g:
            rows[pid] = (pid, g, v.get("full_name") or "",
                         v.get("position") or "", route)

    con.execute("""CREATE OR REPLACE TABLE player_xref (
        sleeper_id VARCHAR PRIMARY KEY, gsis_id VARCHAR, name VARCHAR,
        pos VARCHAR, route VARCHAR)""")
    con.executemany("INSERT OR REPLACE INTO player_xref VALUES (?,?,?,?,?)",
                    list(rows.values()))
    if verbose:
        n = con.execute("SELECT count(*) FROM player_xref").fetchone()[0]
        byroute = con.execute("SELECT route, count(*) FROM player_xref "
                              "GROUP BY route ORDER BY 2 DESC").fetchall()
        hit = con.execute(
            "SELECT count(DISTINCT x.sleeper_id) FROM player_xref x "
            "JOIN player_week w ON w.pid = x.gsis_id").fetchone()[0]
        print(f"      crosswalk: {n:,} sleeper->gsis ids "
              f"({', '.join(f'{r}={c:,}' for r, c in byroute)}), "
              f"{hit:,} appear in play-by-play")
    return con


def _norm(s):
    import re
    return re.sub(r"[^a-z]", "", (s or "").lower())


def status(con=None):
    con = con or connect(read_only=True)
    have = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name='pbp'").fetchone()[0]
    if not have:
        print("no pbp table yet - run --ingest")
        return
    print(f"duckdb: {DB_PATH}  ({os.path.getsize(DB_PATH)/1e6:.0f} MB)")
    for r in con.execute("SELECT season, rows, loaded_at FROM pbp_seasons "
                         "ORDER BY season").fetchall():
        print(f"  {r[0]}  {r[1]:>8,} plays   loaded {r[2]:%Y-%m-%d %H:%M}")
    pw = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name='player_week'").fetchone()[0]
    if pw:
        n, lo, hi = con.execute(
            "SELECT count(*), min(season), max(season) FROM player_week").fetchone()
        print(f"  player_week: {n:,} rows, {lo}-{hi}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", metavar="SEASONS",
                    help="e.g. 2016-2025 or 2024,2025")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.ingest:
        con = connect()
        ingest(parse_seasons(a.ingest), con)
        build(con)
        status(con)
    elif a.build:
        con = connect()
        build(con)
        status(con)
    else:
        status()


if __name__ == "__main__":
    main()
