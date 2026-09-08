#!/usr/bin/env python3
"""Resolve published forecasts and score them. Brier, skill, reliability.

    python3 engine/grade.py --resolve --week 1    settle last week's forecasts
    python3 engine/grade.py --report              scores over everything settled
    python3 engine/grade.py --plot out.png        reliability diagram

A forecast that is never graded is decoration. This closes the loop: it looks up
what actually happened, marks every proposition hit or miss, and reports the
Brier score against two references that make the number mean something.

**Brier score** is the mean squared error of a probability: `mean((p - o)^2)`,
zero being perfect and 0.25 being what you get by saying 50% to everything.
Alone it is close to meaningless, because a set of easy questions scores well
however lazily you answer. So it is reported next to:

- **Climatology** - the Brier you would get by ignoring every player and
  predicting the base rate of the whole category. Beating this is the minimum
  bar for the model having learned anything.
- **Brier skill score** - `1 - BS/BS_climatology`. Positive means we add
  information; zero or negative means the model is decoration and should be
  said so plainly.

The **Murphy decomposition** splits the score into reliability (are our 70%s
right 70% of the time?), resolution (do we separate likely from unlikely at
all?) and uncertainty (how hard were the questions?). A model can be perfectly
calibrated and useless - always predicting the base rate is perfectly reliable
with zero resolution - so both halves are reported, never just the flattering
one.
"""
import sys, os, json, argparse, math, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import nflverse as NV
import sync as SY
from value import load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N_BINS = 10


# ------------------------------------------------------------------ resolve
def actual_points(con, season, week):
    """{pid: league-scored points} for a week, play-by-play first.

    Play-by-play is the authority for anyone who touches the ball, because it
    is scored with this league's rules. Kickers and defenses are not in it -
    their scoring needs field-goal distances and points-allowed brackets that
    the derived table does not carry - so those fall back to what Sleeper
    recorded, and that difference is stated rather than hidden.
    """
    out = {}
    try:
        for pid, f in con.execute(
                "SELECT pid, fpts FROM player_week WHERE season=? AND week=?",
                (season, week)).fetchall():
            out[str(pid)] = float(f or 0.0)
    except Exception:
        pass
    s = DB.connect()
    for r in s.execute("SELECT pid, pts FROM actual WHERE season=? AND week=? "
                       "AND played=1 AND pts IS NOT NULL",
                       (str(season), week)):
        out.setdefault(str(r["pid"]), float(r["pts"]))
    return out


def league_results(cfg, week):
    """{roster_id: points} and {roster_id: won} from Sleeper's own matchups."""
    ms = SY.get(f"{SY.API}/league/{cfg['league_id']}/matchups/{week}") or []
    pts = {m["roster_id"]: (m.get("points") or 0.0) for m in ms}
    won, pairs = {}, {}
    for m in ms:
        if m.get("matchup_id") is not None:
            pairs.setdefault(m["matchup_id"], []).append(m["roster_id"])
    for rids in pairs.values():
        if len(rids) == 2:
            a, b = rids
            won[a] = 1 if pts.get(a, 0) > pts.get(b, 0) else 0
            won[b] = 1 if pts.get(b, 0) > pts.get(a, 0) else 0
    return pts, won


def resolve(season=None, week=None, verbose=True):
    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = int(season or st.get("season") or 2026)
    week = int(week if week is not None else (st.get("week") or 1))
    con = NV.connect()
    rows = con.execute(
        "SELECT id, kind, subject, threshold, prob FROM forecast "
        "WHERE season=? AND week=? AND resolved=FALSE", (season, week)).fetchall()
    if not rows:
        if verbose:
            print(f"nothing unresolved for {season} week {week}")
        return 0
    pts = actual_points(con, season, week)
    tpts, won = league_results(cfg, week)

    updates, unresolvable = [], 0
    for fid, kind, subject, thr, prob in rows:
        if kind == "player_over":
            a = pts.get(str(subject))
            if a is None:
                unresolvable += 1
                continue
            updates.append((1 if a >= thr else 0, a, fid))
        elif kind == "team_over":
            a = tpts.get(int(subject)) if str(subject).isdigit() else None
            if a is None:
                unresolvable += 1
                continue
            updates.append((1 if a >= thr else 0, a, fid))
        elif kind == "matchup_win":
            w = won.get(int(subject)) if str(subject).isdigit() else None
            if w is None:
                unresolvable += 1
                continue
            updates.append((int(w), tpts.get(int(subject)), fid))
    con.executemany("UPDATE forecast SET resolved=TRUE, outcome=?, actual=? "
                    "WHERE id=?", updates)
    if verbose:
        print(f"resolved {len(updates)} of {len(rows)} forecasts for "
              f"{season} week {week}"
              + (f" ({unresolvable} had no outcome data)" if unresolvable else ""))
    return len(updates)


# ------------------------------------------------------------------- scoring
def brier(pairs):
    """pairs = [(prob, outcome)] -> mean squared error."""
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def decompose(pairs, n_bins=N_BINS):
    """Murphy: Brier = reliability - resolution + uncertainty."""
    if not pairs:
        return None
    n = len(pairs)
    base = sum(o for _, o in pairs) / n
    bins = [[] for _ in range(n_bins)]
    for p, o in pairs:
        k = min(n_bins - 1, int(p * n_bins))
        bins[k].append((p, o))
    rel = res = 0.0
    table = []
    for k, b in enumerate(bins):
        if not b:
            table.append(None)
            continue
        nk = len(b)
        pk = sum(p for p, _ in b) / nk
        ok = sum(o for _, o in b) / nk
        rel += nk * (pk - ok) ** 2
        res += nk * (ok - base) ** 2
        table.append({"bin": k, "n": nk, "mean_p": pk, "obs": ok,
                      "lo": k / n_bins, "hi": (k + 1) / n_bins})
    return {"brier": brier(pairs), "reliability": rel / n,
            "resolution": res / n, "uncertainty": base * (1 - base),
            "base_rate": base, "n": n, "bins": table}


def fetch(con, season=None, week=None, kind=None, pos=None):
    q = ("SELECT kind, pos, prob, outcome FROM forecast "
         "WHERE resolved=TRUE AND outcome IS NOT NULL")
    args = []
    for col, val in (("season", season), ("week", week), ("kind", kind), ("pos", pos)):
        if val is not None:
            q += f" AND {col}=?"
            args.append(val)
    return con.execute(q, args).fetchall()


def report(season=None, week=None, verbose=True):
    con = NV.connect(read_only=False)
    rows = fetch(con, season, week)
    if not rows:
        print("nothing resolved yet - run --resolve after a week completes")
        return None
    out = {"overall": None, "by_kind": {}, "by_pos": {}}
    allp = [(r[2], r[3]) for r in rows]
    d = decompose(allp)
    clim = brier([(d["base_rate"], o) for _, o in allp])
    d["climatology"] = clim
    d["skill"] = (1 - d["brier"] / clim) if clim else None
    out["overall"] = d

    for kind in sorted({r[0] for r in rows}):
        sub = [(r[2], r[3]) for r in rows if r[0] == kind]
        dd = decompose(sub)
        c = brier([(dd["base_rate"], o) for _, o in sub])
        dd["climatology"] = c
        dd["skill"] = (1 - dd["brier"] / c) if c else None
        out["by_kind"][kind] = dd
    for p in sorted({r[1] for r in rows if r[1]}):
        sub = [(r[2], r[3]) for r in rows if r[1] == p]
        if len(sub) < 20:
            continue
        dd = decompose(sub)
        c = brier([(dd["base_rate"], o) for _, o in sub])
        dd["climatology"] = c
        dd["skill"] = (1 - dd["brier"] / c) if c else None
        out["by_pos"][p] = dd

    if verbose:
        print(render(out))
    return out


def render(out):
    L = []
    d = out["overall"]
    L.append(f"FORECAST SCORECARD - {d['n']:,} resolved propositions\n")
    L.append(f"  Brier score        {d['brier']:.4f}   (0 perfect, 0.25 = coin flip)")
    L.append(f"  Climatology        {d['climatology']:.4f}   "
             f"(always predict the {d['base_rate']:.1%} base rate)")
    skill = d["skill"]
    verdict = ("we add real information" if skill and skill > 0.05 else
               "barely better than the base rate" if skill and skill > 0 else
               "NO BETTER THAN GUESSING THE BASE RATE")
    L.append(f"  Brier skill score  {skill:+.4f}   <- {verdict}")
    L.append("")
    L.append(f"  reliability {d['reliability']:.4f} (lower better - are our 70%s "
             f"right 70% of the time?)")
    L.append(f"  resolution  {d['resolution']:.4f} (higher better - do we separate "
             f"likely from unlikely?)")
    L.append(f"  uncertainty {d['uncertainty']:.4f} (how hard the questions were)")
    if out["by_kind"]:
        L.append("\n  BY QUESTION TYPE")
        L.append(f"  {'kind':<14}{'n':>7}{'brier':>9}{'clim':>9}{'skill':>9}")
        for k, v in out["by_kind"].items():
            L.append(f"  {k:<14}{v['n']:>7,}{v['brier']:>9.4f}"
                     f"{v['climatology']:>9.4f}{v['skill']:>+9.4f}")
    if out["by_pos"]:
        L.append("\n  BY POSITION")
        L.append(f"  {'pos':<14}{'n':>7}{'brier':>9}{'clim':>9}{'skill':>9}")
        for k, v in out["by_pos"].items():
            L.append(f"  {k:<14}{v['n']:>7,}{v['brier']:>9.4f}"
                     f"{v['climatology']:>9.4f}{v['skill']:>+9.4f}")
    L.append("\n  RELIABILITY TABLE (predicted vs observed)")
    L.append(f"  {'band':<12}{'n':>7}{'predicted':>11}{'observed':>10}{'gap':>8}")
    for b in d["bins"]:
        if not b:
            continue
        L.append(f"  {b['lo']:.0%}-{b['hi']:.0%}".ljust(14)
                 + f"{b['n']:>5,}{b['mean_p']:>11.1%}{b['obs']:>10.1%}"
                   f"{b['obs']-b['mean_p']:>+8.1%}")
    return "\n".join(L)


# ---------------------------------------------------------------- the plot
def plot(path, season=None, week=None, title=None):
    """Reliability diagram: predicted probability against observed frequency."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    con = NV.connect(read_only=False)
    rows = fetch(con, season, week)
    if not rows:
        return None
    d = decompose([(r[2], r[3]) for r in rows])
    pts = [b for b in d["bins"] if b]

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(7, 8), height_ratios=[3, 1], sharex=True)
    ax.plot([0, 1], [0, 1], "--", color="#888", lw=1, label="perfect calibration")
    ax.plot([b["mean_p"] for b in pts], [b["obs"] for b in pts],
            "o-", color="#2E86DE", lw=2, ms=7, label="our forecasts")
    for b in pts:
        ax.annotate(f"{b['n']:,}", (b["mean_p"], b["obs"]),
                    textcoords="offset points", xytext=(6, -10),
                    fontsize=7, color="#555")
    ax.set_ylabel("observed frequency")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.grid(alpha=.25)
    ax.legend(loc="upper left", fontsize=9)
    ax.set_title(title or
                 f"Reliability - {d['n']:,} propositions, "
                 f"Brier {d['brier']:.4f}, skill "
                 f"{1 - d['brier']/brier([(d['base_rate'], o) for _, o in [(r[2], r[3]) for r in rows]]):+.3f}")
    ax2.bar([b["mean_p"] for b in pts], [b["n"] for b in pts],
            width=1.0 / N_BINS * 0.85, color="#95A5A6")
    ax2.set_xlabel("forecast probability")
    ax2.set_ylabel("count")
    ax2.grid(alpha=.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--plot", metavar="PATH")
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int)
    a = ap.parse_args()
    if a.resolve:
        resolve(a.season, a.week)
    if a.report or (not a.resolve and not a.plot):
        report(a.season, a.week)
    if a.plot:
        p = plot(a.plot, a.season, a.week)
        print(f"wrote {p}" if p else "nothing resolved to plot")


if __name__ == "__main__":
    main()
