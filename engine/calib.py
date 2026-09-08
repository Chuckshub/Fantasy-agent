#!/usr/bin/env python3
"""Paired projections and outcomes, league-scored, and the model fitted to them.

    python3 engine/calib.py --pull 2024,2025     projections WITH stat lines
    python3 engine/calib.py --fit                fit the predictive distribution
    python3 engine/calib.py --backtest           does usage beat the baseline?

The point of this module is that a projection alone cannot be scored. "Nico
Collins projects 13.2" is unfalsifiable; "Nico Collins has a 46% chance of
clearing 12 points" is not. Turning the first into the second needs a
distribution around the projection, and the only honest source for that is what
actually happened the last two seasons at each projection level.

**Everything here is league-scored.** The existing `wproj` table is not usable
for this: it stored Sleeper's generic `pts_ppr`, which prices passing yards at
0.05 against this league's 0.04, and the stat line was thrown away so it cannot
be corrected after the fact. So projections are re-pulled with their stat lines
and scored with `scoring.league_points`, and outcomes come from the play-by-play
in DuckDB, scored the same way. Both sides of every pair use one set of rules.
"""
import sys, os, json, argparse, statistics, math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scoring as SC
import nflverse as NV
from history import get, POSITIONS
from value import load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(HERE, "data", "calibration.json")

# Positions whose stat line the feed breaks out finely enough to re-score.
# Kickers and defenses are not among them: field-goal distance bands and
# points-allowed brackets are not in the projection payload, so their numbers
# stay as the feed reports them and are modelled separately, from outcomes only.
SCORABLE = ("QB", "RB", "WR", "TE")


# --------------------------------------------------------------------- pull
def pull_projections(seasons, weeks=range(1, 19), verbose=True):
    """Weekly projections with stat lines, league-scored, into DuckDB."""
    cfg = load_config()
    ss = cfg.get("scoring_settings") or {}
    con = NV.connect()
    con.execute("""CREATE TABLE IF NOT EXISTS proj_week (
        season INTEGER, week INTEGER, pid VARCHAR, pos VARCHAR,
        team VARCHAR, opponent VARCHAR, proj DOUBLE, proj_generic DOUBLE,
        PRIMARY KEY (season, week, pid))""")
    for season in seasons:
        for wk in weeks:
            rows_out = []
            for pos in POSITIONS:
                rows = get(f"https://api.sleeper.com/projections/nfl/{season}/{wk}"
                           f"?season_type=regular&position[]={pos}"
                           f"&order_by=pts_ppr") or []
                for r in rows:
                    pid = r.get("player_id")
                    st = r.get("stats") or {}
                    if not pid or not st:
                        continue
                    generic = st.get("pts_ppr")
                    lg = SC.league_points(st, ss, pos)
                    val = lg if lg is not None else generic
                    if val is None:
                        continue
                    rows_out.append((season, wk, pid, pos, r.get("team"),
                                     r.get("opponent"), val, generic))
            if rows_out:
                con.execute("DELETE FROM proj_week WHERE season=? AND week=?",
                            (season, wk))
                con.executemany(
                    "INSERT OR REPLACE INTO proj_week VALUES (?,?,?,?,?,?,?,?)",
                    rows_out)
            if verbose:
                print(f"  {season} wk {wk:>2}: {len(rows_out)} projections",
                      flush=True)
    n = con.execute("SELECT count(*) FROM proj_week").fetchone()[0]
    print(f"  -> proj_week holds {n:,} rows")
    return con


# ---------------------------------------------------------------- the pairs
PAIRS_SQL = """
SELECT p.season, p.week, p.pid, p.pos, p.proj,
       COALESCE(w.fpts, 0.0) AS actual
FROM proj_week p
JOIN player_xref x ON x.sleeper_id = p.pid
LEFT JOIN player_week w
  ON w.season = p.season AND w.week = p.week AND w.pid = x.gsis_id
WHERE p.pos IN ('QB','RB','WR','TE')
  AND p.proj IS NOT NULL
  AND p.proj >= {min_proj}
"""


def active_index():
    """{(season, week, pid)} for players who were actually active that week.

    This matters more than it looks. `lineup.effective` zeroes out byes and Out
    designations *before* anything is forecast, so the live system only ever
    publishes probabilities for players it believes will play. If the model were
    fitted on a sample that also contained inactive players - all of them true
    zeros - it would learn a far fatter zero spike than the live question has,
    and every probability would come out systematically too low. Fit and use
    have to be conditioned on the same thing.
    """
    import db as DB
    s = DB.connect()
    return {(int(r["season"]), int(r["week"]), str(r["pid"]))
            for r in s.execute("SELECT season, week, pid FROM actual "
                               "WHERE played=1")}


def pairs(con=None, min_proj=1.0, active_only=True):
    """(projection, actual) for every scorable player-week we can join.

    A missing play-by-play row for an *active* player is a real zero - he
    played and never touched the ball, or touched it and lost yardage. Those
    weeks stay in. Dropping them would quietly delete the bad half of the
    sample and produce a model that looks beautifully calibrated and is not.

    Players who were not active at all are excluded instead, because the live
    forecast never asks about them. See `active_index`.
    """
    con = con or NV.connect(read_only=True)
    rows = con.execute(PAIRS_SQL.format(min_proj=min_proj)).fetchall()
    if not active_only:
        return rows
    act = active_index()
    if not act:
        return rows
    return [r for r in rows
            if (int(r[0]), int(r[1]), str(r[2])) in act]


# ----------------------------------------------------------------- the model
# Fantasy scoring is right-skewed and heteroscedastic: a 20-point projection is
# not merely a shifted 5-point projection, it is a wider one, and both have a
# floor at zero that a normal distribution happily ignores. So rather than
# assume a shape, the residual distribution is stored empirically as quantiles
# of the RATIO actual/projection, in bins of projection size. Ratios rather than
# differences because the spread scales with the projection.
BINS = [(1, 4), (4, 7), (7, 10), (10, 13), (13, 17), (17, 22), (22, 99)]
QUANTS = [0.02, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
          0.60, 0.70, 0.80, 0.90, 0.95, 0.98]


def _bin_of(proj):
    for lo, hi in BINS:
        if lo <= proj < hi:
            return f"{lo}-{hi}"
    return f"{BINS[-1][0]}-{BINS[-1][1]}"


def fit(con=None, verbose=True):
    """Empirical ratio quantiles per position and projection bin."""
    rows = pairs(con)
    buckets = {}
    for season, week, pid, pos, proj, actual in rows:
        if not proj or proj <= 0:
            continue
        buckets.setdefault((pos, _bin_of(proj)), []).append(actual / proj)
    model = {"bins": [f"{a}-{b}" for a, b in BINS], "quantiles": QUANTS,
             "cells": {}, "n_pairs": len(rows)}
    for (pos, b), ratios in sorted(buckets.items()):
        if len(ratios) < 40:
            continue
        ratios.sort()
        qs = [_quantile(ratios, q) for q in QUANTS]
        model["cells"][f"{pos}|{b}"] = {
            "n": len(ratios), "q": [round(x, 4) for x in qs],
            "mean": round(statistics.fmean(ratios), 4),
            "p_zero": round(sum(1 for r in ratios if r <= 0.001) / len(ratios), 4),
        }
    # A position-level fallback for bins too thin to fit on their own.
    for pos in SCORABLE:
        allr = sorted(r for (p, _), v in buckets.items() if p == pos for r in v)
        if len(allr) >= 100:
            model["cells"][f"{pos}|*"] = {
                "n": len(allr), "q": [round(_quantile(allr, q), 4) for q in QUANTS],
                "mean": round(statistics.fmean(allr), 4),
                "p_zero": round(sum(1 for r in allr if r <= 0.001) / len(allr), 4)}
    json.dump(model, open(MODEL_PATH, "w"), indent=1)
    if verbose:
        print(f"  fitted on {len(rows):,} projection/outcome pairs")
        print(f"  {'cell':<12}{'n':>7}{'p(zero)':>9}{'q10':>7}{'q50':>7}{'q90':>7}")
        for k, c in sorted(model["cells"].items()):
            i10, i50, i90 = QUANTS.index(0.10), QUANTS.index(0.50), QUANTS.index(0.90)
            print(f"  {k:<12}{c['n']:>7,}{c['p_zero']:>9.3f}"
                  f"{c['q'][i10]:>7.2f}{c['q'][i50]:>7.2f}{c['q'][i90]:>7.2f}")
        print(f"  -> {MODEL_PATH}")
    return model


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    i = q * (len(sorted_vals) - 1)
    lo, hi = int(math.floor(i)), int(math.ceil(i))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def load_model():
    try:
        return json.load(open(MODEL_PATH))
    except Exception:
        return None


def cell_for(model, pos, proj):
    if not model:
        return None
    return (model["cells"].get(f"{pos}|{_bin_of(proj)}")
            or model["cells"].get(f"{pos}|*"))


def p_at_least(proj, pos, threshold, model=None):
    """P(actual >= threshold) for a player projected `proj`.

    Read straight off the empirical ratio quantiles: the threshold is converted
    to the ratio it represents, and the share of history above that ratio is
    the probability. Linear interpolation between quantiles, clipped away from
    0 and 1 because a sample of a few hundred weeks cannot justify certainty.
    """
    model = model or load_model()
    cell = cell_for(model, pos, proj)
    if not cell or not proj or proj <= 0:
        return None
    want = threshold / proj
    qs, quants = cell["q"], model["quantiles"]
    if want <= qs[0]:
        p_below = quants[0] * (want / qs[0] if qs[0] > 0 else 1.0)
    elif want >= qs[-1]:
        p_below = 1.0 - (1.0 - quants[-1]) * min(1.0, qs[-1] / want if want else 1)
    else:
        p_below = quants[-1]
        for i in range(len(qs) - 1):
            if qs[i] <= want <= qs[i + 1]:
                span = qs[i + 1] - qs[i]
                frac = 0.0 if span <= 0 else (want - qs[i]) / span
                p_below = quants[i] + frac * (quants[i + 1] - quants[i])
                break
    return max(0.005, min(0.995, 1.0 - p_below))


def sample(proj, pos, rng, model=None, n=1):
    """Draw plausible outcomes for a projection, by inverting the quantiles."""
    model = model or load_model()
    cell = cell_for(model, pos, proj)
    if not cell or not proj or proj <= 0:
        return [max(0.0, proj or 0.0)] * n
    qs, quants = cell["q"], model["quantiles"]
    out = []
    for _ in range(n):
        u = rng.random()
        if u <= quants[0]:
            r = qs[0] * (u / quants[0] if quants[0] > 0 else 1)
        elif u >= quants[-1]:
            r = qs[-1] * (1 + (u - quants[-1]) / max(1e-9, 1 - quants[-1]) * 0.35)
        else:
            r = qs[-1]
            for i in range(len(quants) - 1):
                if quants[i] <= u <= quants[i + 1]:
                    span = quants[i + 1] - quants[i]
                    frac = 0.0 if span <= 0 else (u - quants[i]) / span
                    r = qs[i] + frac * (qs[i + 1] - qs[i])
                    break
        out.append(max(0.0, proj * r))
    return out


def fit_on(seasons, con=None, verbose=False):
    """Fit using only the given seasons. Returns the model without saving it."""
    con = con or NV.connect(read_only=True)
    rows = [r for r in pairs(con) if r[0] in set(seasons)]
    buckets = {}
    for season, week, pid, pos, proj, actual in rows:
        if not proj or proj <= 0:
            continue
        buckets.setdefault((pos, _bin_of(proj)), []).append(actual / proj)
    model = {"bins": [f"{a}-{b}" for a, b in BINS], "quantiles": QUANTS,
             "cells": {}, "n_pairs": len(rows), "fit_seasons": sorted(seasons)}
    for (pos, b), ratios in buckets.items():
        if len(ratios) < 40:
            continue
        ratios.sort()
        model["cells"][f"{pos}|{b}"] = {
            "n": len(ratios), "q": [_quantile(ratios, q) for q in QUANTS],
            "p_zero": sum(1 for r in ratios if r <= 0.001) / len(ratios)}
    for pos in SCORABLE:
        allr = sorted(r for (p, _), v in buckets.items() if p == pos for r in v)
        if len(allr) >= 100:
            model["cells"][f"{pos}|*"] = {
                "n": len(allr), "q": [_quantile(allr, q) for q in QUANTS],
                "p_zero": sum(1 for r in allr if r <= 0.001) / len(allr)}
    if verbose:
        print(f"  fitted on {seasons}: {len(rows):,} pairs, "
              f"{len(model['cells'])} cells")
    return model


# The same ladders forecast.py publishes, so the backtest grades the questions
# the live system will actually be asked rather than easier ones.
LADDERS = {"QB": [10, 15, 18, 22, 26], "RB": [5, 10, 14, 18, 22],
           "WR": [5, 10, 14, 18, 22], "TE": [4, 8, 12, 16]}


def backtest(fit_seasons, test_season, con=None, verbose=True, plot_path=None):
    """Fit on one set of seasons, forecast another, and grade it honestly.

    Fitting and testing on the same years would flatter the model - the
    quantiles would already know the answers. So the model is fitted on
    `fit_seasons` and every proposition is asked of `test_season`, which it has
    never seen. This is the only number here worth believing before the season
    starts.
    """
    import grade as GR
    con = con or NV.connect(read_only=True)
    model = fit_on(fit_seasons, con, verbose=verbose)
    rows = [r for r in pairs(con) if r[0] == test_season]
    graded, by_pos = [], {}
    for season, week, pid, pos, proj, actual in rows:
        for t in LADDERS.get(pos, []):
            p = p_at_least(proj, pos, t, model)
            if p is None:
                continue
            o = 1 if actual >= t else 0
            graded.append((p, o))
            by_pos.setdefault(pos, []).append((p, o))
    if not graded:
        print("no propositions to grade")
        return None
    d = GR.decompose(graded)
    clim = GR.brier([(d["base_rate"], o) for _, o in graded])
    d["climatology"] = clim
    d["skill"] = 1 - d["brier"] / clim if clim else None
    out = {"overall": d, "by_kind": {}, "by_pos": {}}
    for pos, sub in sorted(by_pos.items()):
        dd = GR.decompose(sub)
        c = GR.brier([(dd["base_rate"], o) for _, o in sub])
        dd["climatology"] = c
        dd["skill"] = 1 - dd["brier"] / c if c else None
        out["by_pos"][pos] = dd
    if verbose:
        print(f"\nOUT-OF-SAMPLE: fitted on {fit_seasons}, tested on {test_season}")
        print(GR.render(out))
    if plot_path:
        _plot_pairs(graded, plot_path,
                    f"Out-of-sample calibration - fit {fit_seasons}, "
                    f"test {test_season}")
        print(f"\n  reliability plot -> {plot_path}")
    return out


def _plot_pairs(graded, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import grade as GR
    d = GR.decompose(graded)
    pts = [b for b in d["bins"] if b]
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(7, 8),
                                  height_ratios=[3, 1], sharex=True)
    ax.plot([0, 1], [0, 1], "--", color="#888", lw=1, label="perfect calibration")
    ax.plot([b["mean_p"] for b in pts], [b["obs"] for b in pts], "o-",
            color="#2E86DE", lw=2, ms=7, label="our forecasts")
    for b in pts:
        ax.annotate(f"{b['n']:,}", (b["mean_p"], b["obs"]),
                    textcoords="offset points", xytext=(6, -10),
                    fontsize=7, color="#555")
    ax.set_ylabel("observed frequency"); ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.grid(alpha=.25); ax.legend(loc="upper left", fontsize=9)
    ax.set_title(f"{title}\n{d['n']:,} propositions, Brier {d['brier']:.4f}")
    ax2.bar([b["mean_p"] for b in pts], [b["n"] for b in pts],
            width=0.085, color="#95A5A6")
    ax2.set_xlabel("forecast probability"); ax2.set_ylabel("count")
    ax2.grid(alpha=.25)
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", metavar="SEASONS")
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--backtest", action="store_true",
                    help="fit on 2024, grade 2025 out of sample")
    ap.add_argument("--plot", metavar="PATH")
    a = ap.parse_args()
    if a.backtest:
        backtest([2024], 2025, plot_path=a.plot)
        return
    if a.pull:
        pull_projections(NV.parse_seasons(a.pull))
        fit()
    elif a.fit:
        fit()
    else:
        m = load_model()
        print(json.dumps(m, indent=1)[:2000] if m else "no model fitted yet")


if __name__ == "__main__":
    main()
