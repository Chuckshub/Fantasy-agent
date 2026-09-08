#!/usr/bin/env python3
"""Does any tracked-but-unapplied signal actually earn its way into a projection?

    python3 engine/signals.py --test        fit on 2024, grade on 2025

MODEL.md rejected defence-vs-position, the market's implied team total and
durability, and this module exists because that rejection deserved re-testing
rather than re-quoting. Three things have changed since it was written:

1. **Scoring was wrong.** Those tests ran on Sleeper's generic `pts_ppr`, which
   prices passing yards at 0.05 against this league's 0.04. Every quarterback
   residual in the original sample was about two points off.
2. **There was no usage data.** Target share, air-yards share, carry share and
   red-zone looks did not exist in the store until the play-by-play was
   ingested. They were never tested at all, and they are the signals with the
   strongest prior in the literature.
3. **The metric was MAE**, which answers the wrong question twice over. A lineup
   does not need an accurate number, it needs the right *ordering* of two
   players. A forecast does not need an accurate number either, it needs
   *calibration*. A signal can leave MAE untouched and still improve both.

So every candidate is measured three ways, out of sample:

- **MAE** on the point estimate, which is the original test, kept so the answer
  is comparable to the one it might overturn.
- **Rank correlation within position-week**, which is what actually decides a
  start/sit.
- **Brier skill** once the adjusted projection is run back through the
  calibration, which is what decides whether a published probability is better.

A signal has to beat the baseline out of sample to be adopted. Anything that
does not stays exactly where it is now: shown as context, applied to nothing.
"""
import sys, os, json, argparse, math, statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nflverse as NV
import calib as CB
import grade as GR

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every feature below is computed from weeks STRICTLY BEFORE the week being
# predicted. `ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING` is doing the real work
# here: including the current row would let each week see its own answer, which
# is the classic way a backtest of this shape flatters itself into production.
FEATURE_SQL = """
CREATE OR REPLACE TEMP TABLE feat AS
WITH pw AS (
  SELECT season, week, pid, fpts, target_share, carry_share, air_yards_share,
         rz_targets, rz_carries, targets, rush_att,
         AVG(target_share)    OVER w AS prior_ts,
         AVG(carry_share)     OVER w AS prior_cs,
         AVG(air_yards_share) OVER w AS prior_ays,
         AVG(rz_targets + rz_carries) OVER w AS prior_rz,
         AVG(fpts)            OVER w AS prior_fpts,
         COUNT(*)             OVER w AS prior_n
  FROM player_week
  WINDOW w AS (PARTITION BY season, pid ORDER BY week
               ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING)
),
-- Defence vs position, walk-forward INSIDE the season: what this opponent had
-- allowed to this position in the weeks before the one being predicted.
allowed AS (
  SELECT w.season, w.week, w.opp, x.pos,
         SUM(w.fpts) AS allowed_pts
  FROM player_week w
  JOIN player_xref x ON x.gsis_id = w.pid
  WHERE x.pos IN ('QB','RB','WR','TE')
  GROUP BY 1,2,3,4
),
dvp AS (
  SELECT season, week, opp, pos,
         AVG(allowed_pts) OVER (PARTITION BY season, opp, pos ORDER BY week
                                ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING) AS dvp_ppg,
         COUNT(*)         OVER (PARTITION BY season, opp, pos ORDER BY week
                                ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING) AS dvp_n
  FROM allowed
)
SELECT p.season, p.week, p.pid, p.pos, p.proj, p.team, p.opponent,
       COALESCE(w.fpts, 0.0) AS actual,
       pw.prior_ts, pw.prior_cs, pw.prior_ays, pw.prior_rz,
       pw.prior_fpts, pw.prior_n,
       d.dvp_ppg, d.dvp_n
FROM proj_week p
JOIN player_xref x ON x.sleeper_id = p.pid
LEFT JOIN player_week w
  ON w.season=p.season AND w.week=p.week AND w.pid=x.gsis_id
LEFT JOIN pw
  ON pw.season=p.season AND pw.week=p.week AND pw.pid=x.gsis_id
LEFT JOIN dvp d
  ON d.season=p.season AND d.week=p.week AND d.opp=p.opponent AND d.pos=p.pos
WHERE p.pos IN ('QB','RB','WR','TE') AND p.proj >= 3
"""


def vegas_index():
    """{(season, week, team): implied team total} from the closing line."""
    import db as DB
    s = DB.connect()
    out = {}
    for r in s.execute("SELECT season, week, home_team, away_team, spread_line, "
                       "total_line FROM game WHERE total_line IS NOT NULL "
                       "AND spread_line IS NOT NULL"):
        t, sp = r["total_line"], r["spread_line"]
        out[(int(r["season"]), int(r["week"]), r["home_team"])] = t / 2.0 + sp / 2.0
        out[(int(r["season"]), int(r["week"]), r["away_team"])] = t / 2.0 - sp / 2.0
    return out


def load(con=None):
    con = con or NV.connect(read_only=True)
    con.execute(FEATURE_SQL)
    cols = [c[0] for c in con.execute("SELECT * FROM feat LIMIT 0").description]
    rows = [dict(zip(cols, r)) for r in con.execute("SELECT * FROM feat").fetchall()]
    veg = vegas_index()
    act = CB.active_index()
    out = []
    for r in rows:
        if (int(r["season"]), int(r["week"]), str(r["pid"])) not in act:
            continue                      # same condition the live system uses
        r["vegas"] = veg.get((int(r["season"]), int(r["week"]), r["team"]))
        r["ratio"] = r["actual"] / r["proj"] if r["proj"] else None
        out.append(r)
    return out


# --------------------------------------------------------------- the signals
def _z(vals):
    v = [x for x in vals if x is not None]
    if len(v) < 20:
        return None, None
    mu = statistics.fmean(v)
    sd = statistics.pstdev(v) or 1.0
    return mu, sd


def _ratio(num, den, r):
    a, b = r.get(num), r.get(den)
    if a is None or not b:
        return None
    return a / b


CANDIDATES = {
    # name: (row -> raw value, minimum prior weeks required)
    #
    # Levels first. These are the forms the original rejection tested.
    "usage_target_share": (lambda r: r.get("prior_ts"), 2),
    "usage_carry_share": (lambda r: r.get("prior_cs"), 2),
    "usage_air_yards_share": (lambda r: r.get("prior_ays"), 2),
    "usage_redzone_looks": (lambda r: r.get("prior_rz"), 2),
    "recent_points": (lambda r: r.get("prior_fpts"), 2),
    "defence_vs_position": (lambda r: r.get("dvp_ppg"), 0),
    "vegas_implied_total": (lambda r: r.get("vegas"), 0),

    # Ratios second, and these are the ones with the better motivation. A level
    # asks "is this player heavily used?", which the projection already knows.
    # A ratio asks "has this player's recent output drifted away from what he is
    # still being projected for?" - which is a question about whether the
    # projection is stale, and is the only version of this that could contain
    # information the projection does not already have.
    "stale_vs_recent_points": (lambda r: _ratio("prior_fpts", "proj", r), 2),
    "usage_per_projected_point": (lambda r: _ratio("prior_ts", "proj", r), 2),
    "redzone_per_projected_point": (lambda r: _ratio("prior_rz", "proj", r), 2),
}


def usable(r, getter, min_prior):
    if getter(r) is None or r.get("ratio") is None:
        return False
    if min_prior and (r.get("prior_n") or 0) < min_prior:
        return False
    return True


def fit_deciles(rows, getter, n_bins=10):
    """Mean residual ratio per decile of the signal, as a multiplier.

    This exists because the linear fit was the wrong estimator and nearly hid a
    real effect. The residual ratio is heavy-tailed - a handful of 3x weeks per
    bucket - and OLS on a heavy-tailed target returns a slope dominated by those
    tails, which made the market signal look like a coefficient of 0.013 and
    therefore worthless. Binning and taking the conditional mean instead shows
    the bottom decile running at 0.75x its projection and the top at 0.98x,
    which is not nothing.

    The multiplier is each bucket's mean ratio divided by the overall mean, so
    it is a relative correction and leaves the average projection untouched.
    """
    vals = sorted(((getter(r), r["ratio"]) for r in rows), key=lambda t: t[0])
    if len(vals) < n_bins * 30:
        return None
    k = len(vals) // n_bins
    edges, means = [], []
    overall = statistics.fmean(y for _, y in vals)
    for i in range(n_bins):
        chunk = vals[i * k:(i + 1) * k] if i < n_bins - 1 else vals[(n_bins - 1) * k:]
        if not chunk:
            return None
        edges.append(chunk[0][0])
        means.append(statistics.fmean(y for _, y in chunk))
    return {"edges": edges, "means": means, "overall": overall or 1.0,
            "n": len(vals)}


def decile_multiplier(dec, value, cap=0.35):
    if dec is None or value is None:
        return 1.0
    i = 0
    for j, e in enumerate(dec["edges"]):
        if value >= e:
            i = j
    m = dec["means"][i] / max(1e-6, dec["overall"])
    return max(1 - cap, min(1 + cap, m))


def fit_linear(rows, getter):
    """OLS of the residual ratio on one standardised signal. b is the effect."""
    xs = [getter(r) for r in rows]
    mu, sd = _z(xs)
    if mu is None:
        return None
    pts = [((getter(r) - mu) / sd, r["ratio"]) for r in rows]
    n = len(pts)
    mx = statistics.fmean(x for x, _ in pts)
    my = statistics.fmean(y for _, y in pts)
    cov = sum((x - mx) * (y - my) for x, y in pts) / n
    var = sum((x - mx) ** 2 for x, _ in pts) / n or 1e-9
    b = cov / var
    return {"mu": mu, "sd": sd, "a": my - b * mx, "b": b, "n": n}


def spearman(pairs):
    """Rank correlation, computed without scipy."""
    if len(pairs) < 3:
        return None
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk
    a = ranks([p for p, _ in pairs])
    b = ranks([q for _, q in pairs])
    n = len(pairs)
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else None


def rank_score(rows, adjust=None):
    """Mean within-(week, position) rank correlation of prediction vs outcome.

    This is the test a start/sit decision actually cares about. Getting every
    number two points high costs nothing if the ordering survives; getting the
    ordering wrong costs a lineup slot however tidy the MAE looks.
    """
    groups = {}
    for r in rows:
        pred = r["proj"] * (adjust(r) if adjust else 1.0)
        groups.setdefault((r["season"], r["week"], r["pos"]), []).append(
            (pred, r["actual"]))
    cors = [spearman(g) for g in groups.values() if len(g) >= 5]
    cors = [c for c in cors if c is not None]
    return statistics.fmean(cors) if cors else None


def brier_skill(rows, model, adjust=None):
    """Brier skill of the published-style propositions under this adjustment."""
    graded = []
    for r in rows:
        pred = r["proj"] * (adjust(r) if adjust else 1.0)
        for t in CB.LADDERS.get(r["pos"], []):
            p = CB.p_at_least(pred, r["pos"], t, model)
            if p is None:
                continue
            graded.append((p, 1 if r["actual"] >= t else 0))
    if not graded:
        return None
    d = GR.decompose(graded)
    clim = GR.brier([(d["base_rate"], o) for _, o in graded])
    return {"brier": d["brier"], "skill": 1 - d["brier"] / clim if clim else None,
            "n": len(graded), "reliability": d["reliability"]}


def mae(rows, adjust=None):
    return statistics.fmean(
        abs(r["proj"] * (adjust(r) if adjust else 1.0) - r["actual"]) for r in rows)


# ------------------------------------------------------------------- the test
def run(fit_season=2024, test_season=2025, cap=0.35, verbose=True,
        shape="linear"):
    rows = load()
    fit_rows = [r for r in rows if r["season"] == fit_season]
    test_rows = [r for r in rows if r["season"] == test_season]
    model = CB.fit_on([fit_season], verbose=False)

    base = {"mae": mae(test_rows), "rank": rank_score(test_rows),
            "brier": brier_skill(test_rows, model)}
    if verbose:
        print(f"SIGNAL TEST ({shape}) - fit {fit_season}, graded on {test_season}")
        print(f"  {len(fit_rows):,} fitting rows, {len(test_rows):,} test rows\n")
        print("BASELINE (Sleeper's projection, unadjusted)")
        print(f"  MAE            {base['mae']:.4f}")
        print(f"  rank corr      {base['rank']:+.4f}")
        print(f"  Brier          {base['brier']['brier']:.4f}"
              f"   skill {base['brier']['skill']:+.4f}\n")
        print(f"{'signal':<24}{'n':>7}{'coef':>8}{'MAE':>9}{'d MAE':>9}"
              f"{'rank':>9}{'d rank':>9}{'skill':>9}{'d skill':>9}")

    results = {}
    for name, (getter, min_prior) in CANDIDATES.items():
        f_ok = [r for r in fit_rows if usable(r, getter, min_prior)]
        t_ok = [r for r in test_rows if usable(r, getter, min_prior)]
        if len(f_ok) < 200 or len(t_ok) < 200:
            continue
        coef = fit_linear(f_ok, getter)
        dec = fit_deciles(f_ok, getter) if shape == "decile" else None
        if not coef and not dec:
            continue

        if shape == "decile":
            def adjust(r, d=dec, g=getter):
                return decile_multiplier(d, g(r), cap)
        else:
            def adjust(r, c=coef, g=getter):
                v = g(r)
                if v is None:
                    return 1.0
                z = (v - c["mu"]) / c["sd"]
                # The multiplier is the fitted ratio relative to the fitted mean
                # ratio, capped: a linear fit extrapolated to the tail of a
                # standardised feature will otherwise produce a 3x projection.
                m = (c["a"] + c["b"] * z) / max(1e-6, c["a"])
                return max(1 - cap, min(1 + cap, m))

        # Compare on the SAME subset the signal is available for, otherwise a
        # signal that is only present for regulars would look better simply by
        # having dropped the hard cases.
        b_sub = {"mae": mae(t_ok), "rank": rank_score(t_ok),
                 "brier": brier_skill(t_ok, model)}
        a_sub = {"mae": mae(t_ok, adjust), "rank": rank_score(t_ok, adjust),
                 "brier": brier_skill(t_ok, model, adjust)}
        res = {
            "n_test": len(t_ok), "coef": coef["b"],
            "mae": a_sub["mae"], "d_mae": b_sub["mae"] - a_sub["mae"],
            "rank": a_sub["rank"],
            "d_rank": (a_sub["rank"] - b_sub["rank"])
                      if (a_sub["rank"] and b_sub["rank"]) else None,
            "skill": a_sub["brier"]["skill"] if a_sub["brier"] else None,
            "d_skill": (a_sub["brier"]["skill"] - b_sub["brier"]["skill"])
                       if (a_sub["brier"] and b_sub["brier"]) else None,
        }
        results[name] = res
        if verbose:
            print(f"{name:<24}{res['n_test']:>7,}{res['coef']:>8.3f}"
                  f"{res['mae']:>9.4f}{res['d_mae']:>+9.4f}"
                  f"{(res['rank'] or 0):>9.4f}{(res['d_rank'] or 0):>+9.4f}"
                  f"{(res['skill'] or 0):>9.4f}{(res['d_skill'] or 0):>+9.4f}")

    if verbose:
        print("\n  d columns are the change from the baseline ON THE SAME ROWS.")
        print("  Positive d MAE means lower error. Positive d rank means better")
        print("  ordering. Positive d skill means better-calibrated probabilities.")
        keep = [k for k, v in results.items()
                if (v["d_rank"] or 0) > 0.002 and (v["d_skill"] or 0) > 0.002]
        print(f"\n  EARNS ITS PLACE: {keep or 'nothing - leave the model alone'}")
    return {"baseline": base, "signals": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--fit-season", type=int, default=2024)
    ap.add_argument("--test-season", type=int, default=2025)
    ap.add_argument("--cap", type=float, default=0.35)
    ap.add_argument("--shape", choices=("linear","decile"), default="linear")
    a = ap.parse_args()
    run(a.fit_season, a.test_season, a.cap, shape=a.shape)


if __name__ == "__main__":
    main()
