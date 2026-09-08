#!/usr/bin/env python3
"""Persistent league store for StatKing.

SQLite (stdlib - no pip on this machine) at data/statking.db. This is the
memory that lets later decisions be better than earlier ones: what we
projected vs what actually happened, who owns whom, what has been traded, and
what we believed at the time we believed it.

Design notes:
- Every fact that can change over time is written as a dated snapshot rather
  than overwritten, so "what did we think in week 3" stays answerable. That is
  the whole point - a trade engine needs to know whether its own past reads
  were any good.
- Nothing here writes to Sleeper. It is a read-and-remember store.
"""
import os, sqlite3, json, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(HERE, "data", "statking.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

-- provenance: every sync run gets a row, every fact points at one
CREATE TABLE IF NOT EXISTS snapshot (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      INTEGER NOT NULL,
  season  TEXT,
  week    INTEGER,
  kind    TEXT NOT NULL,
  note    TEXT
);

CREATE TABLE IF NOT EXISTS player (
  pid         TEXT PRIMARY KEY,
  name        TEXT,
  pos         TEXT,
  team        TEXT,
  age         REAL,
  years_exp   INTEGER,
  bye         INTEGER,
  updated_ts  INTEGER
);

-- season-long or weekly projections, kept as history so drift is visible
CREATE TABLE IF NOT EXISTS projection (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  pid         TEXT NOT NULL,
  season      TEXT,
  week        INTEGER,          -- NULL = full season
  pts         REAL,
  vorp        REAL,
  adp         REAL,
  PRIMARY KEY (snapshot_id, pid, week)
);

-- Per-game actuals: the ground truth we grade ourselves against, and the raw
-- material for every matchup question. Opponent and venue are stored alongside
-- the points because "how did this player do against THIS defense" is
-- unanswerable without them.
CREATE TABLE IF NOT EXISTS actual (
  pid       TEXT NOT NULL,
  season    TEXT NOT NULL,
  week      INTEGER NOT NULL,
  pos       TEXT,
  team      TEXT,
  opponent  TEXT,
  pts       REAL,
  snaps     REAL,
  started   INTEGER,
  played    INTEGER,
  stats     TEXT,                 -- json blob
  PRIMARY KEY (pid, season, week)
);

-- Preseason game logs, kept in their OWN table on purpose. Folding these into
-- `actual` would corrupt everything downstream: durability would count exhibition
-- games as real ones, and a season-to-date average would be polluted by snaps
-- taken against third-string defenses.
CREATE TABLE IF NOT EXISTS preseason (
  pid      TEXT NOT NULL,
  season   TEXT NOT NULL,
  week     INTEGER NOT NULL,
  pos      TEXT, team TEXT, opponent TEXT,
  pts      REAL, snaps REAL, started INTEGER,
  stats    TEXT,
  PRIMARY KEY (pid, season, week)
);
CREATE INDEX IF NOT EXISTS ix_pre_s ON preseason(season);

-- Game context: Vegas lines, venue, weather. The implied team total (derived
-- from the spread and the over/under) is the market's own point projection for
-- an offense, and the market prices in things no stat feed carries.
CREATE TABLE IF NOT EXISTS game (
  season      TEXT NOT NULL,
  week        INTEGER NOT NULL,
  home_team   TEXT NOT NULL,
  away_team   TEXT,
  home_score  REAL, away_score REAL,
  spread_line REAL,          -- positive = home favoured, nflverse convention
  total_line  REAL,
  roof        TEXT, surface TEXT, temp REAL, wind REAL,
  PRIMARY KEY (season, week, home_team)
);
CREATE INDEX IF NOT EXISTS ix_game_sw ON game(season, week);

-- Sleeper's own weekly projections, kept as history so we can measure how
-- wrong they were. A projection you cannot grade is a rumour.
CREATE TABLE IF NOT EXISTS wproj (
  pid      TEXT NOT NULL,
  season   TEXT NOT NULL,
  week     INTEGER NOT NULL,
  pos      TEXT,
  team     TEXT,
  opponent TEXT,
  pts      REAL,
  PRIMARY KEY (pid, season, week)
);
CREATE INDEX IF NOT EXISTS ix_wproj_sw ON wproj(season, week);

-- who has whom, per week
CREATE TABLE IF NOT EXISTS ownership (
  snapshot_id INTEGER NOT NULL REFERENCES snapshot(id),
  season      TEXT, week INTEGER,
  roster_id   INTEGER, owner_id TEXT, pid TEXT,
  is_starter  INTEGER DEFAULT 0,
  PRIMARY KEY (snapshot_id, roster_id, pid)
);

CREATE TABLE IF NOT EXISTS manager (
  owner_id   TEXT PRIMARY KEY,
  roster_id  INTEGER,
  username   TEXT,
  team_name  TEXT
);

CREATE TABLE IF NOT EXISTS txn (
  txn_id  TEXT PRIMARY KEY,
  season  TEXT, week INTEGER,
  type    TEXT, status TEXT, ts INTEGER,
  payload TEXT
);

CREATE TABLE IF NOT EXISTS draft_pick (
  draft_id TEXT NOT NULL, pick_no INTEGER NOT NULL,
  round INTEGER, slot INTEGER, pid TEXT, picked_by TEXT,
  PRIMARY KEY (draft_id, pick_no)
);

CREATE TABLE IF NOT EXISTS matchup (
  season TEXT, week INTEGER, roster_id INTEGER,
  points REAL, starters TEXT, players TEXT,
  PRIMARY KEY (season, week, roster_id)
);

-- Feature requests raised from Discord via !f-lenovo, and what came of them.
CREATE TABLE IF NOT EXISTS feature_request (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  requester TEXT,
  request   TEXT NOT NULL,
  status    TEXT DEFAULT 'queued',   -- queued|building|built|failed|held
  branch    TEXT,
  summary   TEXT,
  finished  INTEGER
);

-- our own reasoning: bets, trade theses, watch flags. Free-form on purpose.
CREATE TABLE IF NOT EXISTS note (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      INTEGER NOT NULL,
  pid     TEXT,
  kind    TEXT,          -- 'bet' | 'trade_target' | 'avoid' | 'injury' | 'misc'
  horizon TEXT,          -- 'week' | 'season' | 'dynasty'
  text    TEXT,
  resolved INTEGER DEFAULT 0,
  outcome TEXT
);

CREATE INDEX IF NOT EXISTS ix_act_opp  ON actual(opponent, pos);
CREATE INDEX IF NOT EXISTS ix_act_pid  ON actual(pid);
CREATE INDEX IF NOT EXISTS ix_own_pid  ON ownership(pid);
CREATE INDEX IF NOT EXISTS ix_proj_pid ON projection(pid);
CREATE INDEX IF NOT EXISTS ix_note_pid ON note(pid);
"""


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    _migrate(con)          # must precede SCHEMA: it creates indexes on `actual`
    con.executescript(SCHEMA)
    return con


def _migrate(con):
    """CREATE TABLE IF NOT EXISTS will not add columns to a table that already
    exists, so widen `actual` in place when an older shape is found. Rebuilding
    is safe while it is empty; refuse loudly rather than silently drop data."""
    try:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(actual)")}
    except Exception:
        return

    if cols and "opponent" not in cols:
        n = con.execute("SELECT COUNT(*) c FROM actual").fetchone()["c"]
        if n:
            raise RuntimeError(
                f"actual table has an old shape and {n} rows; migrate by hand")
        con.execute("DROP TABLE actual")
        con.commit()


def new_snapshot(con, kind, season=None, week=None, note=None):
    cur = con.execute(
        "INSERT INTO snapshot (ts, season, week, kind, note) VALUES (?,?,?,?,?)",
        (int(time.time()), season, week, kind, note))
    con.commit()
    return cur.lastrowid


def add_note(con, kind, text, pid=None, horizon="season"):
    con.execute("INSERT INTO note (ts,pid,kind,horizon,text) VALUES (?,?,?,?,?)",
                (int(time.time()), pid, kind, horizon, text))
    con.commit()


def summary(con):
    out = {}
    for t in ("snapshot", "player", "projection", "actual", "ownership",
              "manager", "txn", "draft_pick", "matchup", "note"):
        out[t] = con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    return out


if __name__ == "__main__":
    con = connect()
    print(f"db: {DB_PATH}")
    for k, v in summary(con).items():
        print(f"  {k:<12}{v:>8}")
