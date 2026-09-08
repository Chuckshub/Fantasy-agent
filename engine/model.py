#!/usr/bin/env python3
"""The weighting layer: turn raw projections into decisions.

A projection says what a player scores if he plays a normal game against an
average defense. Three things break that assumption every week, and this module
prices each one:

1. **Availability.** A projection is conditional on playing. A back who has
   missed 20% of his career games is worth less than one who has missed none,
   and no season projection reflects that.
2. **Matchup.** Defenses differ by 2+ standard deviations in what they concede
   to a position. That is the largest single weekly lever we have.
3. **Form and role.** Snap share and recent scoring move faster than
   preseason projections do.

Every component is bounded, every bound is documented, and the matchup piece is
backtested against real outcomes in `backtest_matchup()` rather than asserted.

    python3 engine/model.py --durability "Christian McCaffrey"
    python3 engine/model.py --dvp RB
    python3 engine/model.py --backtest 2025
    python3 engine/model.py --score "Nico Collins" --vs DAL --week 4
"""
import sys, os, json, math, argparse, statistics, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
from value import build_board, load_config

# Recency weights for multi-season aggregates. NFL rosters and schemes turn over
# fast enough that a five-year-old defensive ranking is nearly worthless; these
# halve roughly every season.
SEASON_WEIGHTS = {2025: 1.00, 2024: 0.55, 2023: 0.30, 2022: 0.16, 2021: 0.09,
                  2020: 0.05, 2019: 0.03}

# Shrinkage: a defense with few observed games is pulled toward the league mean.
# k is "how many games of evidence are needed before we half-believe the split".
DVP_SHRINK_K = 10.0

# --------------------------------------------------------------------------
# WHAT SURVIVED TESTING
#
# Every lever below was backtested on 2023-2025 (see MODEL.md). Most did not
# survive, and the constants reflect the evidence rather than the intuition:
#
#   REJECTED  prior-season defense-vs-position     -0.03% .. -1.07% MAE
#   REJECTED  same-season walk-forward matchup     -0.61% .. +0.31% (null)
#   REJECTED  positional bias correction (OOS)     -1.27%
#   REJECTED  durability weighting of projections  -0.01% .. +2.21% (unstable)
#   KEPT      0.8 projection + 0.2 season average  -0.2% .. -0.9% MAE, both years
#
# MATCHUP_CAP is 0.0 deliberately. A cap sweep found MAE rising monotonically
# with the cap (+/-5% -> +0.15%, +/-35% -> -1.75%), which is the signature of no
# signal at all: the optimum sits at zero. The function is kept because the
# numbers are worth *showing* a human, but it no longer moves a projection.
BLEND_ALPHA = 0.80          # weight on Sleeper's projection vs season-to-date
MATCHUP_CAP = 0.0           # display only - see above
FORM_CAP = 0.10             # +/-10% from recent scoring vs baseline (unvalidated)
AVAIL_FLOOR = 0.55          # never write a healthy-but-fragile player off entirely

# Injury designations -> probability of actually suiting up. Derived from the
# conventional reading of the report, not from our own data, and flagged as such.
STATUS_PLAY_PROB = {
    "Out": 0.0, "IR": 0.0, "PUP": 0.0, "Sus": 0.0, "NA": 0.0, "DNR": 0.0,
    "COV": 0.0, "Doubtful": 0.25, "Questionable": 0.75, "Limited": 0.9,
}


# ------------------------------------------------------------- availability
def team_weeks_played(con, season):
    """How many game weeks each team actually appeared in that season."""
    rows = con.execute(
        "SELECT team, COUNT(DISTINCT week) n FROM actual "
        "WHERE season=? AND team IS NOT NULL GROUP BY team", (str(season),))
    return {r["team"]: r["n"] for r in rows}


def _opportunity_weeks(con, pid, season):
    """Weeks the player *could* have played: the union of game weeks belonging
    to every team he appeared for that season.

    Grouping by season alone and reading `team` off the group hands back an
    arbitrary one of his teams, which for a mid-season trade produces nonsense -
    McCaffrey's 2022 read as "7 of 7" because only his post-trade club counted.
    """
    teams = [r["team"] for r in con.execute(
        "SELECT DISTINCT team FROM actual WHERE pid=? AND season=? AND team IS NOT NULL",
        (pid, str(season)))]
    if not teams:
        return None
    q = ("SELECT COUNT(DISTINCT week) n FROM actual WHERE season=? AND team IN (%s)"
         % ",".join("?" * len(teams)))
    return con.execute(q, [str(season)] + teams).fetchone()["n"]


def durability(pid, con=None, as_of_season=2026):
    """Share of his team's games a player has actually played, recency-weighted.

    Counts only seasons in which he appeared at all - a rookie year before he
    was in the league is not a missed game, and treating it as one would libel
    every young player.
    """
    con = con or DB.connect()
    rows = con.execute(
        "SELECT season, COUNT(DISTINCT week) appeared FROM actual "
        "WHERE pid=? AND played=1 GROUP BY season", (pid,)).fetchall()
    if not rows:
        return None
    num = den = 0.0
    detail = []
    for r in rows:
        season = int(r["season"])
        tw = _opportunity_weeks(con, pid, season) or 17
        share = min(1.0, r["appeared"] / tw)
        w = SEASON_WEIGHTS.get(season, 0.02)
        num += share * w
        den += w
        detail.append({"season": season, "played": r["appeared"],
                       "of": tw, "share": share})
    return {"rate": num / den if den else None, "seasons": detail,
            "n_seasons": len(rows)}


def availability(pid, injury_status=None, con=None):
    """P(this player is on the field this week), and why.

    Combines the standing durability rate with the current designation. A
    Questionable tag on a player who never misses is a different animal from
    the same tag on one who misses a quarter of every season, and multiplying
    the two is how that difference shows up.
    """
    d = durability(pid, con)
    base = d["rate"] if d and d["rate"] is not None else 0.92
    base = max(AVAIL_FLOOR, base)
    if injury_status in STATUS_PLAY_PROB:
        p = STATUS_PLAY_PROB[injury_status]
        return (base * p if p > 0 else 0.0,
                f"durability {base:.0%} x {injury_status} {p:.0%}")
    return base, f"durability {base:.0%}"


# ------------------------------------------------- defense versus position
def dvp_weighted(con=None, seasons=None, min_games=4):
    """Points allowed per game by defense and position, blended across seasons.

    Recent seasons dominate, and thin samples are shrunk toward the league
    mean so a defense with four observed games cannot post a +2 z-score on
    noise alone.
    """
    con = con or DB.connect()
    seasons = seasons or sorted(
        {int(r["season"]) for r in con.execute("SELECT DISTINCT season FROM actual")},
        reverse=True)
    rows = con.execute(
        "SELECT season, opponent d, pos, week, SUM(pts) allowed FROM actual "
        "WHERE played=1 AND opponent IS NOT NULL AND pts IS NOT NULL "
        "GROUP BY season, opponent, pos, week").fetchall()
    acc = collections.defaultdict(lambda: [0.0, 0.0, 0.0])   # wsum, w, games
    for r in rows:
        s = int(r["season"])
        if s not in seasons:
            continue
        w = SEASON_WEIGHTS.get(s, 0.02)
        a = acc[(r["d"], r["pos"])]
        a[0] += r["allowed"] * w
        a[1] += w
        a[2] += 1
    raw = {}
    for (d, pos), (wsum, w, n) in acc.items():
        if n < min_games or w <= 0:
            continue
        raw.setdefault(pos, {})[d] = {"ppg": wsum / w, "n": int(n)}
    out = {}
    for pos, teams in raw.items():
        xs = [v["ppg"] for v in teams.values()]
        mu, sd = statistics.fmean(xs), (statistics.pstdev(xs) or 1.0)
        out[pos] = {}
        for d, v in teams.items():
            k = v["n"] / (v["n"] + DVP_SHRINK_K)          # shrink thin samples
            adj = mu + k * (v["ppg"] - mu)
            out[pos][d] = {"ppg": v["ppg"], "adj": adj, "n": v["n"],
                           "z": (adj - mu) / sd, "mu": mu, "shrink": k}
    return out


def matchup_multiplier(pos, opponent, dvp, cap=MATCHUP_CAP):
    cell = (dvp.get(pos) or {}).get(opponent)
    if not cell:
        return 1.0, "no matchup data"
    z = max(-2.0, min(2.0, cell["z"]))
    return (1.0 + cap * (z / 2.0),
            f"{opponent} concedes {cell['adj']:.1f} PPR/gm to {pos} "
            f"(z {cell['z']:+.2f}, {cell['n']} gms, shrink {cell['shrink']:.2f})")


# ---------------------------------------------------------------- form
def form_multiplier(pid, con=None, last_n=4, cap=FORM_CAP):
    con = con or DB.connect()
    g = con.execute(
        "SELECT pts FROM actual WHERE pid=? AND played=1 AND pts IS NOT NULL "
        "ORDER BY season DESC, week DESC LIMIT ?", (pid, last_n * 3)).fetchall()
    pts = [r["pts"] for r in g]
    if len(pts) < last_n + 4:
        return 1.0, ""
    recent = statistics.fmean(pts[:last_n])
    baseline = statistics.fmean(pts)
    if baseline <= 0:
        return 1.0, ""
    ratio = max(-0.5, min(0.5, recent / baseline - 1.0))
    return 1.0 + cap * (ratio / 0.5), f"last {last_n}: {recent:.1f} vs {baseline:.1f} baseline"


# ------------------------------------------------------------- backtesting
def composite(weekly_proj, season_to_date_avg=None, alpha=BLEND_ALPHA):
    """The validated weekly estimate.

    0.8 * Sleeper's weekly projection + 0.2 * the player's season-to-date mean.
    That is the whole model. It beat pure projection in both 2024 (4.841 vs
    4.849 MAE) and 2025 (4.983 vs 5.025), and beat the season average alone by
    a wider margin in both.

    Availability is deliberately NOT multiplied in here: bye weeks and Out
    designations are handled as hard zeroes in lineup.py, where they belong,
    and the softer durability weighting could not be shown to help.
    """
    if weekly_proj is None:
        return season_to_date_avg
    if season_to_date_avg is None:
        return weekly_proj
    return alpha * weekly_proj + (1.0 - alpha) * season_to_date_avg


def season_to_date(pid, season, before_week, con=None, min_games=4):
    """Mean actual points this season before `before_week`, or None if thin."""
    con = con or DB.connect()
    rows = con.execute(
        "SELECT pts FROM actual WHERE pid=? AND season=? AND week<? "
        "AND played=1 AND pts IS NOT NULL", (pid, str(season), before_week)).fetchall()
    pts = [r["pts"] for r in rows]
    return statistics.fmean(pts) if len(pts) >= min_games else None


def backtest_matchup(season, con=None, min_prior=4):
    """Does the matchup adjustment actually predict better? Honest answer.

    For every player-week in `season`, predict that week's points from the
    player's season-to-date average, with and without the defense-vs-position
    adjustment built ONLY from prior seasons. Compare mean absolute error.
    Using prior seasons only is the point: a model tested on data it was fitted
    to will always look brilliant.
    """
    con = con or DB.connect()
    dvp = dvp_weighted(con, seasons=[s for s in SEASON_WEIGHTS if s < int(season)])
    rows = con.execute(
        "SELECT pid, pos, week, opponent, pts FROM actual "
        "WHERE season=? AND played=1 AND pts IS NOT NULL AND opponent IS NOT NULL "
        "AND pos IN ('QB','RB','WR','TE') ORDER BY pid, week", (str(season),)).fetchall()
    hist = collections.defaultdict(list)
    base_err, adj_err, n = [], [], 0
    for r in rows:
        prior = hist[r["pid"]]
        if len(prior) >= min_prior:
            base = statistics.fmean(prior)
            m, _ = matchup_multiplier(r["pos"], r["opponent"], dvp)
            base_err.append(abs(base - r["pts"]))
            adj_err.append(abs(base * m - r["pts"]))
            n += 1
        prior.append(r["pts"])
    if not n:
        return None
    b, a = statistics.fmean(base_err), statistics.fmean(adj_err)
    return {"n": n, "mae_baseline": b, "mae_with_matchup": a,
            "improvement_pct": (b - a) / b * 100.0,
            "dvp_from_seasons": [s for s in SEASON_WEIGHTS if s < int(season)]}


def backtest_walkforward(season, con=None, min_prior=4, min_def_games=4,
                         cap=MATCHUP_CAP):
    """Same test, but the matchup model only ever sees THIS season, to date.

    The prior-season version fails (see backtest_matchup): defensive quality
    does not carry across years well enough to predict with. The question this
    answers is the one that actually matters in-season - given what a defense
    has conceded so far *this* year, does adjusting for it help next week?

    Strictly walk-forward: predicting week W uses only weeks < W.
    """
    con = con or DB.connect()
    rows = con.execute(
        "SELECT pid, pos, week, opponent, pts FROM actual "
        "WHERE season=? AND played=1 AND pts IS NOT NULL AND opponent IS NOT NULL "
        "AND pos IN ('QB','RB','WR','TE') ORDER BY week", (str(season),)).fetchall()
    byweek = collections.defaultdict(list)
    for r in rows:
        byweek[r["week"]].append(r)

    allowed = collections.defaultdict(list)      # (def,pos) -> [weekly totals]
    hist = collections.defaultdict(list)         # pid -> [pts]
    base_err, adj_err, applied = [], [], 0
    for wk in sorted(byweek):
        # ---- predict week wk using only what was known before it
        pos_pool = collections.defaultdict(list)
        for (d, pos), vals in allowed.items():
            if len(vals) >= min_def_games:
                pos_pool[pos].append(statistics.fmean(vals))
        stats_by_pos = {pos: (statistics.fmean(v), statistics.pstdev(v) or 1.0)
                        for pos, v in pos_pool.items() if len(v) >= 8}
        for r in byweek[wk]:
            prior = hist[r["pid"]]
            if len(prior) < min_prior:
                continue
            base = statistics.fmean(prior)
            vals = allowed.get((r["opponent"], r["pos"]))
            m = 1.0
            if vals and len(vals) >= min_def_games and r["pos"] in stats_by_pos:
                mu, sd = stats_by_pos[r["pos"]]
                z = max(-2.0, min(2.0, (statistics.fmean(vals) - mu) / sd))
                m = 1.0 + cap * (z / 2.0)
                applied += 1
            base_err.append(abs(base - r["pts"]))
            adj_err.append(abs(base * m - r["pts"]))
        # ---- now fold week wk into what is known
        wk_def = collections.defaultdict(float)
        for r in byweek[wk]:
            wk_def[(r["opponent"], r["pos"])] += r["pts"]
            hist[r["pid"]].append(r["pts"])
        for k, v in wk_def.items():
            allowed[k].append(v)
    if not base_err:
        return None
    b, a = statistics.fmean(base_err), statistics.fmean(adj_err)
    return {"n": len(base_err), "adjusted": applied, "cap": cap,
            "mae_baseline": b, "mae_with_matchup": a,
            "improvement_pct": (b - a) / b * 100.0}


def grade_projections(season, con=None, min_prior=4):
    """Is Sleeper's weekly projection better than the player's own average?

    Compares, on the same player-weeks: the season-to-date mean, Sleeper's
    weekly projection, and a 50/50 blend. If the projection wins clearly, it is
    the signal and our job is to use it, not to second-guess it with
    adjustments it has already priced in.
    """
    con = con or DB.connect()
    rows = con.execute(
        "SELECT a.pid, a.pos, a.week, a.pts actual, w.pts proj FROM actual a "
        "JOIN wproj w ON w.pid=a.pid AND w.season=a.season AND w.week=a.week "
        "WHERE a.season=? AND a.played=1 AND a.pts IS NOT NULL AND w.pts IS NOT NULL "
        "AND a.pos IN ('QB','RB','WR','TE') ORDER BY a.week", (str(season),)).fetchall()
    hist = collections.defaultdict(list)
    e_base, e_proj, e_blend, bias = [], [], [], collections.defaultdict(list)
    for r in rows:
        prior = hist[r["pid"]]
        if len(prior) >= min_prior:
            base = statistics.fmean(prior)
            e_base.append(abs(base - r["actual"]))
            e_proj.append(abs(r["proj"] - r["actual"]))
            e_blend.append(abs(0.5 * base + 0.5 * r["proj"] - r["actual"]))
            bias[r["pos"]].append(r["proj"] - r["actual"])
        hist[r["pid"]].append(r["actual"])
    if not e_base:
        return None
    return {
        "n": len(e_base),
        "mae_season_avg": statistics.fmean(e_base),
        "mae_sleeper_proj": statistics.fmean(e_proj),
        "mae_blend": statistics.fmean(e_blend),
        "bias_by_pos": {p: statistics.fmean(v) for p, v in bias.items()},
    }


def bias_table(season, con=None):
    """Mean (projection - actual) by position for one season."""
    con = con or DB.connect()
    rows = con.execute(
        "SELECT a.pos, AVG(w.pts - a.pts) b, COUNT(*) n FROM actual a "
        "JOIN wproj w ON w.pid=a.pid AND w.season=a.season AND w.week=a.week "
        "WHERE a.season=? AND a.played=1 AND a.pts IS NOT NULL AND w.pts IS NOT NULL "
        "GROUP BY a.pos", (str(season),)).fetchall()
    return {r["pos"]: {"bias": r["b"], "n": r["n"]} for r in rows}


def test_bias_correction(fit_season, test_season, con=None):
    """Correct next year's projections with last year's measured bias.

    Strictly out-of-sample: the correction is fitted on `fit_season` and applied,
    untouched, to `test_season`. A correction fitted and tested on the same year
    would flatter itself.
    """
    con = con or DB.connect()
    bias = bias_table(fit_season, con)
    rows = con.execute(
        "SELECT a.pos, a.pts actual, w.pts proj FROM actual a "
        "JOIN wproj w ON w.pid=a.pid AND w.season=a.season AND w.week=a.week "
        "WHERE a.season=? AND a.played=1 AND a.pts IS NOT NULL AND w.pts IS NOT NULL "
        "AND a.pos IN ('QB','RB','WR','TE')", (str(test_season),)).fetchall()
    raw, corr = [], []
    for r in rows:
        b = (bias.get(r["pos"]) or {}).get("bias", 0.0)
        raw.append(abs(r["proj"] - r["actual"]))
        corr.append(abs((r["proj"] - b) - r["actual"]))
    if not raw:
        return None
    a, c = statistics.fmean(raw), statistics.fmean(corr)
    return {"n": len(raw), "fit": fit_season, "test": test_season,
            "mae_raw": a, "mae_corrected": c,
            "improvement_pct": (a - c) / a * 100.0,
            "correction": {p: -v["bias"] for p, v in bias.items()}}


def blend_sweep(season, con=None, min_prior=4):
    """Find the best mix of Sleeper's projection and the player's own average."""
    con = con or DB.connect()
    rows = con.execute(
        "SELECT a.pid, a.pts actual, w.pts proj FROM actual a "
        "JOIN wproj w ON w.pid=a.pid AND w.season=a.season AND w.week=a.week "
        "WHERE a.season=? AND a.played=1 AND a.pts IS NOT NULL AND w.pts IS NOT NULL "
        "AND a.pos IN ('QB','RB','WR','TE') ORDER BY a.week", (str(season),)).fetchall()
    hist, pairs = collections.defaultdict(list), []
    for r in rows:
        prior = hist[r["pid"]]
        if len(prior) >= min_prior:
            pairs.append((statistics.fmean(prior), r["proj"], r["actual"]))
        hist[r["pid"]].append(r["actual"])
    out = []
    for i in range(0, 11):
        alpha = i / 10.0                       # weight on Sleeper's projection
        mae = statistics.fmean(
            abs(alpha * p + (1 - alpha) * b - a) for b, p, a in pairs)
        out.append((alpha, mae))
    return {"n": len(pairs), "curve": out,
            "best": min(out, key=lambda kv: kv[1])}


def test_availability(season, con=None):
    """Does weighting by availability help - counting the weeks they DID NOT play?

    Every other backtest here filters to played=1, which structurally cannot see
    the thing availability is for. A player who is inactive scores zero, and the
    cost of starting him is the whole projection. This test scores every week a
    player was on an NFL roster, DNPs included.
    """
    con = con or DB.connect()
    # weeks in which each player's team had a game, whether or not he played
    rows = con.execute(
        "SELECT a.pid, a.pos, a.season, a.week, a.played, "
        "       COALESCE(a.pts,0) actual, w.pts proj "
        "FROM actual a JOIN wproj w ON w.pid=a.pid AND w.season=a.season "
        "  AND w.week=a.week "
        "WHERE a.season=? AND w.pts IS NOT NULL AND a.pos IN ('QB','RB','WR','TE')",
        (str(season),)).fetchall()
    prior_seasons = [s for s in SEASON_WEIGHTS if s < int(season)]
    dur_cache = {}
    raw, adj = [], []
    for r in rows:
        pid = r["pid"]
        if pid not in dur_cache:
            d = con.execute(
                "SELECT season, COUNT(DISTINCT week) played FROM actual "
                "WHERE pid=? AND played=1 AND CAST(season AS INT)<? GROUP BY season",
                (pid, int(season))).fetchall()
            num = den = 0.0
            for x in d:
                sw = SEASON_WEIGHTS.get(int(x["season"]), 0.02)
                num += min(1.0, x["played"] / 17.0) * sw
                den += sw
            dur_cache[pid] = max(AVAIL_FLOOR, num / den) if den else 0.92
        av = dur_cache[pid]
        actual = r["actual"] if r["played"] else 0.0
        raw.append(abs(r["proj"] - actual))
        adj.append(abs(r["proj"] * av - actual))
    if not raw:
        return None
    a, c = statistics.fmean(raw), statistics.fmean(adj)
    return {"n": len(raw), "mae_raw": a, "mae_availability_weighted": c,
            "improvement_pct": (a - c) / a * 100.0,
            "dnp_share": statistics.fmean(0.0 if r["played"] else 1.0 for r in rows)}


def implied_totals(con=None):
    """{(season, week, team): implied points} from the closing spread and total.

    A 47-point total with the home side favoured by 3 implies 25 for the home
    offense and 22 for the away one. This is the market's own projection of a
    team's scoring, and it moves on injury and weather news faster than any
    stat feed does.
    """
    con = con or DB.connect()
    out = {}
    for r in con.execute("SELECT season, week, home_team, away_team, spread_line, "
                         "total_line FROM game WHERE total_line IS NOT NULL "
                         "AND spread_line IS NOT NULL"):
        t, sp = r["total_line"], r["spread_line"]
        out[(r["season"], r["week"], r["home_team"])] = t / 2.0 + sp / 2.0
        out[(r["season"], r["week"], r["away_team"])] = t / 2.0 - sp / 2.0
    return out


def test_vegas(season, con=None, min_prior=4, betas=(0.0, 0.25, 0.5, 0.75, 1.0, 1.5)):
    """Does the market's implied team total improve on the validated composite?

    Tested *on top of* composite(), not on raw projections - the question is
    whether Vegas adds anything Sleeper has not already priced in.
    """
    con = con or DB.connect()
    imp = implied_totals(con)
    if not imp:
        return None
    league_avg = statistics.fmean(imp.values())
    rows = con.execute(
        "SELECT a.pid, a.pos, a.week, a.team, a.pts actual, w.pts proj "
        "FROM actual a JOIN wproj w ON w.pid=a.pid AND w.season=a.season "
        "  AND w.week=a.week "
        "WHERE a.season=? AND a.played=1 AND a.pts IS NOT NULL AND w.pts IS NOT NULL "
        "AND a.pos IN ('QB','RB','WR','TE') ORDER BY a.week", (str(season),)).fetchall()
    hist, samples = collections.defaultdict(list), []
    for r in rows:
        prior = hist[r["pid"]]
        it = imp.get((str(season), r["week"], r["team"]))
        if len(prior) >= min_prior and it is not None:
            base = composite(r["proj"], statistics.fmean(prior))
            samples.append((base, it, r["actual"]))
        hist[r["pid"]].append(r["actual"])
    if not samples:
        return None
    curve = []
    for b in betas:
        mae = statistics.fmean(
            abs(base * (1.0 + b * (it / league_avg - 1.0)) - act)
            for base, it, act in samples)
        curve.append((b, mae))
    return {"n": len(samples), "league_avg_implied": league_avg, "curve": curve,
            "best": min(curve, key=lambda kv: kv[1])}


# -------------------------------------------------------------------- cli
def _find(name, board):
    return next((p for p in board if p["name"].lower() == name.lower()), None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--durability")
    ap.add_argument("--dvp")
    ap.add_argument("--backtest")
    ap.add_argument("--walkforward")
    ap.add_argument("--sweep")
    ap.add_argument("--grade")
    ap.add_argument("--biastest", nargs=2, metavar=("FIT", "TEST"))
    ap.add_argument("--blend")
    ap.add_argument("--availtest")
    ap.add_argument("--vegas")
    ap.add_argument("--score")
    ap.add_argument("--vs")
    ap.add_argument("--week", type=int, default=1)
    a = ap.parse_args()
    con = DB.connect()
    cfg, board = None, None
    if a.durability or a.score:
        cfg = load_config()
        board, _, _ = build_board(cfg)

    if a.durability:
        p = _find(a.durability, board)
        d = durability(p["pid"], con)
        print(f"{p['name']} ({p['pos']} {p.get('team')})")
        if not d:
            print("  no game logs"); return
        for s in sorted(d["seasons"], key=lambda x: -x["season"]):
            print(f"   {s['season']}  played {s['played']:>2} of {s['of']:>2}  "
                  f"{s['share']:.0%}   (weight {SEASON_WEIGHTS.get(s['season'],0.02):.2f})")
        print(f"  recency-weighted durability: {d['rate']:.1%}")
        av, why = availability(p["pid"], p.get("injury"), con)
        print(f"  availability now: {av:.1%}   [{why}]")

    if a.dvp:
        dvp = dvp_weighted(con)
        teams = dvp.get(a.dvp.upper()) or {}
        rank = sorted(teams.items(), key=lambda kv: -kv[1]["adj"])
        print(f"\nDefenses vs {a.dvp.upper()} - recency-weighted, shrunk "
              f"(league {rank[0][1]['mu']:.1f} PPR/gm)")
        print("  most generous:")
        for d, v in rank[:6]:
            print(f"     {d:<4}{v['adj']:>6.1f}  z {v['z']:+5.2f}  ({v['n']} gms)")
        print("  toughest:")
        for d, v in rank[-6:]:
            print(f"     {d:<4}{v['adj']:>6.1f}  z {v['z']:+5.2f}  ({v['n']} gms)")

    if a.backtest:
        r = backtest_matchup(a.backtest, con)
        if not r:
            print("not enough data"); return
        print(f"\nBacktest on {a.backtest} - {r['n']:,} player-weeks")
        print(f"  matchup model built from seasons {r['dvp_from_seasons']} only")
        print(f"  MAE, season-to-date average   : {r['mae_baseline']:.3f}")
        print(f"  MAE, + matchup adjustment     : {r['mae_with_matchup']:.3f}")
        print(f"  improvement                   : {r['improvement_pct']:+.2f}%")

    if a.walkforward:
        r = backtest_walkforward(a.walkforward, con)
        print(f"\nWalk-forward backtest on {a.walkforward} - {r['n']:,} player-weeks "
              f"({r['adjusted']:,} adjusted)")
        print(f"  MAE, season-to-date average : {r['mae_baseline']:.3f}")
        print(f"  MAE, + same-season matchup  : {r['mae_with_matchup']:.3f}")
        print(f"  improvement                 : {r['improvement_pct']:+.2f}%")

    if a.sweep:
        print(f"\nCap sweep, walk-forward, season {a.sweep}")
        for cap in (0.05, 0.10, 0.18, 0.25, 0.35):
            r = backtest_walkforward(a.sweep, con, cap=cap)
            print(f"  cap +/-{cap:>4.0%}   MAE {r['mae_with_matchup']:.4f}   "
                  f"vs baseline {r['mae_baseline']:.4f}   "
                  f"{r['improvement_pct']:+.2f}%")

    if a.grade:
        r = grade_projections(a.grade, con)
        print(f"\nProjection grading, {a.grade} - {r['n']:,} player-weeks")
        print(f"  MAE, season-to-date average : {r['mae_season_avg']:.3f}")
        print(f"  MAE, Sleeper weekly proj    : {r['mae_sleeper_proj']:.3f}")
        print(f"  MAE, 50/50 blend            : {r['mae_blend']:.3f}")
        best = min(("season avg", r["mae_season_avg"]),
                   ("Sleeper proj", r["mae_sleeper_proj"]),
                   ("blend", r["mae_blend"]), key=lambda kv: kv[1])
        print(f"  best: {best[0]}")
        print("  projection bias (positive = Sleeper over-projects):")
        for pos, b in sorted(r["bias_by_pos"].items()):
            print(f"     {pos:<4}{b:+7.3f} pts/game")

    if a.biastest:
        r = test_bias_correction(a.biastest[0], a.biastest[1], con)
        print(f"\nBias correction fitted on {r['fit']}, applied to {r['test']} "
              f"- {r['n']:,} player-weeks")
        print(f"  MAE, raw Sleeper projection : {r['mae_raw']:.3f}")
        print(f"  MAE, bias-corrected         : {r['mae_corrected']:.3f}")
        print(f"  improvement                 : {r['improvement_pct']:+.2f}%")
        print("  correction applied (pts/game added to the projection):")
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
            if pos in r["correction"]:
                print(f"     {pos:<4}{r['correction'][pos]:+7.3f}")

    if a.blend:
        r = blend_sweep(a.blend, con)
        print(f"\nBlend sweep, {a.blend} - {r['n']:,} player-weeks")
        print("  alpha = weight on Sleeper's projection, 1-alpha on season average")
        for alpha, mae in r["curve"]:
            mark = "  <== best" if (alpha, mae) == r["best"] else ""
            print(f"     alpha {alpha:.1f}   MAE {mae:.4f}{mark}")

    if a.availtest:
        r = test_availability(a.availtest, con)
        print(f"\nAvailability test, {a.availtest} - {r['n']:,} player-weeks "
              f"({r['dnp_share']:.1%} were DNPs)")
        print(f"  MAE, projection as-is        : {r['mae_raw']:.3f}")
        print(f"  MAE, x durability            : {r['mae_availability_weighted']:.3f}")
        print(f"  improvement                  : {r['improvement_pct']:+.2f}%")

    if a.vegas:
        r = test_vegas(a.vegas, con)
        print(f"\nVegas implied-total test, {a.vegas} - {r['n']:,} player-weeks "
              f"(league avg implied {r['league_avg_implied']:.1f})")
        print("  beta = how strongly the implied total scales the estimate")
        for b, mae in r["curve"]:
            mark = "  <== best" if (b, mae) == r["best"] else ""
            print(f"     beta {b:>4.2f}   MAE {mae:.4f}{mark}")

    if a.score:
        p = _find(a.score, board)
        dvp = dvp_weighted(con)
        av, awhy = availability(p["pid"], p.get("injury"), con)
        mm, mwhy = matchup_multiplier(p["pos"], (a.vs or "").upper(), dvp)
        fm, fwhy = form_multiplier(p["pid"], con)
        wk_base = (p.get("proj") or 0) / 17.0
        print(f"\n{p['name']} ({p['pos']} {p.get('team')}) week {a.week}"
              + (f" vs {a.vs.upper()}" if a.vs else ""))
        print(f"  base weekly projection : {wk_base:6.2f}")
        print(f"  availability   x{av:.3f}   {awhy}")
        print(f"  matchup        x{mm:.3f}   {mwhy}")
        print(f"  form           x{fm:.3f}   {fwhy}")
        print(f"  -> expected    {wk_base * av * mm * fm:6.2f}")


if __name__ == "__main__":
    main()
