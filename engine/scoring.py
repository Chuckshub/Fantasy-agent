#!/usr/bin/env python3
"""Verify that the board is scored with THIS league's rules.

The board's projected points come from Sleeper's precomputed `pts_ppr`. That is
only correct if the league's scoring settings actually match standard PPR. This
module recomputes points from the raw stat projections using the league's own
`scoring_settings` and reports the difference, so the assumption is checked
rather than trusted - and so a mid-season scoring change by the commissioner
gets caught instead of silently poisoning every projection.

    python3 engine/scoring.py            # alignment report

Two honest limits, both discovered by getting them wrong first:

- **Kickers and defenses cannot be recomputed from this feed.** Sleeper projects
  `fgm_40_49` and a combined `fgm_50p`, but not the 0-19/20-29/30-39 buckets the
  league scores separately, and for defenses it exposes only `pts_allow_0` rather
  than a projected count in each points-allowed band. Scoring what is there
  double-counts against Sleeper's own number. For these two positions we keep
  Sleeper's figure and say so.
- **IDP stats must be ignored.** Travis Hunter carries `idp_int` and
  `idp_fum_rec` in the feed. Applying the league's `int` and `fum_rec` rules -
  which exist for defensive *units* - to a wide receiver's defensive snaps
  invented four points out of nothing. This league has no IDP slots, so those
  stats score zero.
"""
import json, os, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from value import load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Scoring rules that apply to an offensive skill player. Anything else in the
# league's settings (sack, int, ff, pts_allow_*, fg*) belongs to K or DEF.
OFFENSE_KEYS = {
    "pass_yd", "pass_td", "pass_int", "pass_2pt",
    "rush_yd", "rush_td", "rush_2pt",
    "rec", "rec_yd", "rec_td", "rec_2pt",
    "fum_lost", "fum",
    "bonus_rec_te", "bonus_rec_rb", "bonus_rec_wr",
    "bonus_rush_yd_100", "bonus_rec_yd_100", "bonus_pass_yd_300",
    "rec_fd", "rush_fd", "pass_fd",
}
RECOMPUTABLE = {"QB", "RB", "WR", "TE"}
# What counts as a real rule change, as opposed to a per-player quirk. A
# commissioner switching to half-PPR or adding a TE premium moves a whole
# position by tens of points; one odd player does not. Thresholds are in season
# points and are deliberately loose - an earlier 1.0-point trigger fired on a
# single two-way player and cried wolf.
DRIFT_MEAN = 2.0        # average shift across a position
DRIFT_SHARE = 0.02      # or >2% of the position off by DRIFT_PLAYER or more
DRIFT_PLAYER = 5.0


def league_points(stats, scoring_settings, pos):
    """Points under the league's own rules, or None if the feed can't support it."""
    if pos not in RECOMPUTABLE:
        return None
    return sum(v * scoring_settings[k]
               for k, v in stats.items()
               if k in scoring_settings and k in OFFENSE_KEYS
               and isinstance(v, (int, float)))


def verify_alignment(cfg=None, projections=None):
    cfg = cfg or load_config()
    ss = cfg.get("scoring_settings") or {}
    if not ss:
        return {"ok": False, "reason": "no scoring_settings in config - run engine/sync.py"}
    proj = projections or json.load(
        open(os.path.join(HERE, "data", "projections_2026.json")))
    key = f"pts_{cfg.get('scoring', 'ppr')}"

    bypos, worst, notable = collections.defaultdict(list), [], []
    for pid, rec in proj.items():
        st = rec.get("stats") or {}
        pos = rec.get("position")
        base = st.get(key)
        mine = league_points(st, ss, pos)
        if base is None or mine is None:
            continue
        d = mine - base
        bypos[pos].append(d)
        nm = rec.get("player") or {}
        name = f"{nm.get('first_name','')} {nm.get('last_name','')}".strip()
        worst.append((abs(d), name, pos, base, mine, d))
        if abs(d) >= DRIFT_PLAYER:
            notable.append((name, pos, base, mine, d))
    worst.sort(reverse=True)
    summary = {}
    for pos, v in bypos.items():
        if not v:
            continue
        off = sum(1 for x in v if abs(x) >= DRIFT_PLAYER)
        summary[pos] = {"n": len(v), "mean": sum(v) / len(v),
                        "max_abs": max(abs(x) for x in v),
                        "n_off": off, "share_off": off / len(v)}
    drift = {p: s for p, s in summary.items()
             if abs(s["mean"]) >= DRIFT_MEAN or s["share_off"] >= DRIFT_SHARE}
    return {"ok": not drift, "by_pos": summary, "drift": drift,
            "notable": notable, "worst": worst[:8], "checked_key": key,
            "not_recomputable": sorted(set(
                r.get("position") for r in proj.values()) - RECOMPUTABLE - {None})}


def main():
    r = verify_alignment()
    if not r.get("by_pos"):
        print(r.get("reason", "nothing to check")); return
    print(f"Scoring alignment - league rules vs Sleeper's {r['checked_key']}\n")
    for pos in ("QB", "RB", "WR", "TE"):
        s = r["by_pos"].get(pos)
        if not s:
            continue
        print(f"  {pos:<4} n={s['n']:<5} mean {s['mean']:+7.3f}   "
              f"worst {s['max_abs']:7.3f}   players off by >={DRIFT_PLAYER:.0f}: {s['n_off']}")
    print(f"\n  not recomputable from this feed (Sleeper's figure kept): "
          f"{', '.join(r['not_recomputable'])}")
    if r.get("notable"):
        print("\n  individual players where the league's rules differ from Sleeper's total:")
        for nm, pos, base, mine, d in r["notable"]:
            print(f"     {nm:<24}{pos:<5}{base:>8.1f} -> {mine:>8.1f}  ({d:+.1f})")
    if r["ok"]:
        print("\n  ALIGNED - the league scores skill positions as standard PPR, so the")
        print("  board's projections are correct as-is. No engine change warranted.")
    else:
        print("\n  !! DRIFT DETECTED - the league no longer matches standard PPR:")
        for pos, s in r["drift"].items():
            print(f"     {pos}: mean {s['mean']:+.2f}, worst {s['max_abs']:.2f}")
        print("     Biggest movers:")
        for _, nm, pos, base, mine, d in r["worst"]:
            print(f"       {nm:<24}{pos:<5}{base:>8.1f} -> {mine:>8.1f}  ({d:+.1f})")


if __name__ == "__main__":
    main()
