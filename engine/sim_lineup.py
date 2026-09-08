#!/usr/bin/env python3
"""Does variance-aware lineup setting win more matchups than accuracy does?

    python3 engine/sim_lineup.py --season 2025 --leagues 200

Every other backtest in this project optimises mean absolute error. But MAE is
minimised by predicting the median, and fantasy is not scored on accuracy - it
is scored on beating one specific opponent, once a week. Those are different
objectives, and the difference has a direction: when you are projected to lose,
the safe lineup loses; you need variance. When you are projected to win, the
volatile lineup is what throws the game away.

So this simulates whole seasons on real 2025 outcomes. Managers are dealt real
rosters by snake draft, play a real schedule, and set lineups by one of two
rules:

  BASELINE   maximise expected points (the MAE-optimal choice)
  ADAPTIVE   same, but when projected to lose by more than a threshold, swap in
             higher-variance players; when projected to win comfortably, prefer
             lower-variance ones

Half the league uses each rule, so they play each other directly, and the whole
thing is repeated over many random leagues to drown out draft luck.
"""
import sys, os, json, random, argparse, statistics, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
FLEX = 2
FLEX_OK = ("RB", "WR", "TE")
ROSTER_SIZE = 15
TEAMS = 12
WEEKS = range(1, 18)


def load_pool(season, con, top_n=260):
    """Players with enough 2025 usage to be draftable, plus their week-by-week
    actuals, projections and realised volatility."""
    rows = con.execute(
        "SELECT a.pid, a.pos, a.week, a.pts actual, w.pts proj FROM actual a "
        "LEFT JOIN wproj w ON w.pid=a.pid AND w.season=a.season AND w.week=a.week "
        "WHERE a.season=? AND a.pos IN ('QB','RB','WR','TE','K','DEF') "
        "AND a.pts IS NOT NULL", (str(season),)).fetchall()
    by = collections.defaultdict(lambda: {"weeks": {}, "pos": None})
    for r in rows:
        p = by[r["pid"]]
        p["pos"] = r["pos"]
        p["weeks"][r["week"]] = (r["actual"], r["proj"])
    players = []
    for pid, p in by.items():
        pts = [v[0] for v in p["weeks"].values()]
        if len(pts) < 8:
            continue
        players.append({"pid": pid, "pos": p["pos"], "weeks": p["weeks"],
                        "total": sum(pts), "sd": statistics.pstdev(pts) or 0.1})
    players.sort(key=lambda x: -x["total"])
    return players[:top_n]


def snake_draft(pool, teams=TEAMS, rounds=ROSTER_SIZE, rng=random):
    """Rosters that are legal and roughly sane, drafted with noise."""
    avail = list(pool)
    rosters = [[] for _ in range(teams)]
    order = list(range(teams))
    for rnd in range(rounds):
        seq = order if rnd % 2 == 0 else order[::-1]
        for t in seq:
            need = _needs(rosters[t])
            cands = [p for p in avail
                     if _legal(rosters[t], p)][:18]
            if need:
                pref = [p for p in cands if p["pos"] in need]
                cands = pref or cands
            if not cands:
                continue
            pick = rng.choice(cands[:6])       # noise, so leagues differ
            rosters[t].append(pick)
            avail.remove(pick)
    return rosters


def _counts(roster):
    c = collections.Counter(p["pos"] for p in roster)
    return c


def _needs(roster):
    c = _counts(roster)
    return {pos for pos, n in SLOTS.items() if c[pos] < n}


def _legal(roster, p):
    c = _counts(roster)
    caps = {"QB": 3, "K": 1, "DEF": 1, "RB": 7, "WR": 8, "TE": 3}
    return c[p["pos"]] < caps.get(p["pos"], 8)


def expected(p, week):
    """Projection if Sleeper had one that week, else the player's own mean."""
    v = p["weeks"].get(week)
    if v is None:
        return None, None
    actual, proj = v
    if proj is None:
        others = [a for w, (a, _) in p["weeks"].items() if w != week]
        proj = statistics.fmean(others) if others else actual
    return proj, actual


def build_lineup(roster, week, variance_pref=0.0):
    """Pick a legal lineup. variance_pref > 0 favours volatility, < 0 avoids it.

    The tilt is applied as a bonus proportional to a player's realised standard
    deviation, so it only ever breaks ties between comparable options rather
    than starting a bad player because he is erratic.
    """
    avail = []
    for p in roster:
        proj, actual = expected(p, week)
        if proj is None:
            continue
        score = proj + variance_pref * p["sd"]
        avail.append((score, proj, actual, p))
    avail.sort(key=lambda x: -x[0])
    used, lineup = set(), []
    for pos, n in SLOTS.items():
        taken = 0
        for row in avail:
            if taken >= n:
                break
            if row[3]["pid"] in used or row[3]["pos"] != pos:
                continue
            used.add(row[3]["pid"]); lineup.append(row); taken += 1
    for _ in range(FLEX):
        for row in avail:
            if row[3]["pid"] not in used and row[3]["pos"] in FLEX_OK:
                used.add(row[3]["pid"]); lineup.append(row); break
    return lineup


def season(rosters, adaptive_ids, rng, deficit_trigger=8.0, tilt=0.35):
    """Play one season. Returns wins per team."""
    n = len(rosters)
    wins = [0] * n
    for week in WEEKS:
        order = list(range(n))
        rng.shuffle(order)
        for i in range(0, n - 1, 2):
            a, b = order[i], order[i + 1]
            # first pass: everyone's neutral projection, so each side can see
            # roughly what it is up against - exactly what Sleeper shows you
            proj = {}
            for t in (a, b):
                proj[t] = sum(r[1] for r in build_lineup(rosters[t], week))
            scores = {}
            for t, opp in ((a, b), (b, a)):
                pref = 0.0
                if t in adaptive_ids:
                    margin = proj[t] - proj[opp]
                    if margin < -deficit_trigger:
                        pref = tilt            # behind: buy variance
                    elif margin > deficit_trigger:
                        pref = -tilt           # ahead: sell variance
                scores[t] = sum(r[2] for r in build_lineup(rosters[t], week, pref))
            if scores[a] > scores[b]:
                wins[a] += 1
            elif scores[b] > scores[a]:
                wins[b] += 1
    return wins


def run(season_year, leagues, seed=11, **kw):
    con = DB.connect()
    pool = load_pool(season_year, con)
    rng = random.Random(seed)
    adaptive_w = baseline_w = 0
    adaptive_g = baseline_g = 0
    for _ in range(leagues):
        rosters = snake_draft(pool, rng=rng)
        adaptive_ids = set(rng.sample(range(TEAMS), TEAMS // 2))
        wins = season(rosters, adaptive_ids, rng, **kw)
        for t, w in enumerate(wins):
            if t in adaptive_ids:
                adaptive_w += w; adaptive_g += len(WEEKS)
            else:
                baseline_w += w; baseline_g += len(WEEKS)
    return {"pool": len(pool), "leagues": leagues,
            "adaptive_winrate": adaptive_w / adaptive_g,
            "baseline_winrate": baseline_w / baseline_g,
            "adaptive_wins": adaptive_w, "baseline_wins": baseline_w}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2025")
    ap.add_argument("--leagues", type=int, default=100)
    ap.add_argument("--tilt", type=float, default=0.35)
    ap.add_argument("--trigger", type=float, default=8.0)
    a = ap.parse_args()
    r = run(a.season, a.leagues, tilt=a.tilt, deficit_trigger=a.trigger)
    print(f"\n{a.leagues} simulated leagues on {a.season} actuals "
          f"(pool {r['pool']}, {TEAMS} teams, {len(list(WEEKS))} weeks)")
    print(f"  tilt {a.tilt}, trigger {a.trigger} pts")
    print(f"  BASELINE (maximise expected points) : {r['baseline_winrate']:.4f}")
    print(f"  ADAPTIVE (variance when behind)     : {r['adaptive_winrate']:.4f}")
    d = r["adaptive_winrate"] - r["baseline_winrate"]
    print(f"  difference                          : {d:+.4f} "
          f"({d*len(list(WEEKS)):+.2f} wins per 17-week season)")


def sweep(season_year, leagues, seed=11):
    """Grid over how hard to tilt and when to trigger it."""
    out = []
    for trigger in (4.0, 8.0, 15.0):
        for tilt in (0.15, 0.35, 0.7):
            r = run(season_year, leagues, seed=seed,
                    deficit_trigger=trigger, tilt=tilt)
            out.append((trigger, tilt, r["baseline_winrate"], r["adaptive_winrate"]))
    return out


def build_lineup_naive(roster, week):
    """A manager who does not check availability.

    He starts his best players by projection and never notices that one is on
    bye or inactive. Those players score zero. This is not a strawman - it is
    what happens to anyone who does not set a lineup, and what Sleeper's
    autodraft-and-forget path produces every single bye week.
    """
    avail = []
    for p in roster:
        v = p["weeks"].get(week)
        if v is None:
            others = [a for w, (a, _) in p["weeks"].items()]
            proj = statistics.fmean(others) if others else 0.0
            avail.append((proj, proj, 0.0, p))       # believes, then scores zero
        else:
            actual, proj = v
            if proj is None:
                others = [a for w, (a, _) in p["weeks"].items() if w != week]
                proj = statistics.fmean(others) if others else actual
            avail.append((proj, proj, actual, p))
    avail.sort(key=lambda x: -x[0])
    used, lineup = set(), []
    for pos, n in SLOTS.items():
        taken = 0
        for row in avail:
            if taken >= n:
                break
            if row[3]["pid"] in used or row[3]["pos"] != pos:
                continue
            used.add(row[3]["pid"]); lineup.append(row); taken += 1
    for _ in range(FLEX):
        for row in avail:
            if row[3]["pid"] not in used and row[3]["pos"] in FLEX_OK:
                used.add(row[3]["pid"]); lineup.append(row); break
    return lineup


def season_hygiene(rosters, careful_ids, rng):
    """Careful managers bench anyone without a game; naive ones do not."""
    n = len(rosters)
    wins = [0] * n
    pts = [0.0] * n
    for week in WEEKS:
        order = list(range(n))
        rng.shuffle(order)
        for i in range(0, n - 1, 2):
            a, b = order[i], order[i + 1]
            s = {}
            for t in (a, b):
                lu = (build_lineup(rosters[t], week) if t in careful_ids
                      else build_lineup_naive(rosters[t], week))
                s[t] = sum(r[2] for r in lu)
                pts[t] += s[t]
            if s[a] > s[b]:
                wins[a] += 1
            elif s[b] > s[a]:
                wins[b] += 1
    return wins, pts


def run_hygiene(season_year, leagues, seed=11):
    con = DB.connect()
    pool = load_pool(season_year, con)
    rng = random.Random(seed)
    cw = nw = 0
    cg = ng = 0
    cp = np_ = 0.0
    for _ in range(leagues):
        rosters = snake_draft(pool, rng=rng)
        careful = set(rng.sample(range(TEAMS), TEAMS // 2))
        wins, pts = season_hygiene(rosters, careful, rng)
        for t in range(TEAMS):
            if t in careful:
                cw += wins[t]; cg += len(WEEKS); cp += pts[t]
            else:
                nw += wins[t]; ng += len(WEEKS); np_ += pts[t]
    return {"careful_winrate": cw / cg, "naive_winrate": nw / ng,
            "careful_ppg": cp / cg, "naive_ppg": np_ / ng, "leagues": leagues}
