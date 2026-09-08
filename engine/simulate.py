#!/usr/bin/env python3
"""Validation harness: run full mock drafts, engine vs ADP-following opponents.

Opponents draft the way real leagues drift -- best available by ADP with noise,
with light positional-need logic. We measure each team by its optimal weekly
starting lineup, which is what actually wins matchups.
"""
import sys, os, random, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from value import build_board, load_config
import draft as D
import json as _json


def load_byes():
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "schedule_2026.json")) as f:
            return _json.load(f).get("byes") or {}
    except Exception:
        return {}


# Waiver-wire replacement level per week for positions that are streamed.
STREAMABLE = {"K", "DEF", "QB"}
STREAM_VALUE = {"K": 7.5, "DEF": 6.0, "QB": 14.0}


def weekly_lineup(roster, cfg, week, byes):
    """Optimal lineup for ONE week, with players on bye unavailable."""
    slots, flex_el = cfg["roster_slots"], cfg["flex_eligible"]
    live = [p for p in roster if not (byes.get((p.get("team") or "").upper()) == week)]
    pool = {}
    for p in live:
        pool.setdefault(p["pos"], []).append(p)
    for lst in pool.values():
        lst.sort(key=lambda x: -x["value"])
    total, used, holes = 0.0, set(), 0
    for pos, n in slots.items():
        if pos in ("FLEX", "SUPERFLEX"):
            continue
        got = pool.get(pos, [])[:n]
        for p in got:
            total += p["value"]; used.add(p["pid"])
        missing = n - len(got)
        if missing:
            # K/DEF/QB byes are covered off waivers in every real league --
            # nobody burns a roster spot for 16 weeks to patch one week. Charge
            # the realistic cost of a streamed replacement, not a zero.
            if pos in STREAMABLE:
                total += missing * STREAM_VALUE[pos]
            else:
                holes += missing        # genuinely unfillable: a real zero
    flex_pool = sorted([p for p in live
                        if p["pos"] in flex_el and p["pid"] not in used],
                       key=lambda x: -x["value"])
    fn = slots.get("FLEX", 0)
    for p in flex_pool[:fn]:
        total += p["value"]; used.add(p["pid"])
    holes += fn - len(flex_pool[:fn])
    # per-week points: season projection is 17 games, so scale to one week
    return total / 17.0, holes


def evaluate(roster, cfg, byes, weeks=range(1, 18)):
    """Score a roster the way the league actually scores it: week by week.

    Season totals hide bye-week cliffs -- a roster can lead on paper and still
    lose week 11 with five starters idle. We return the mean weekly score and
    the worst week, because a championship needs both a high ceiling and a
    floor that survives the bad weeks.
    """
    wk = [weekly_lineup(roster, cfg, w, byes) for w in weeks]
    pts = [p for p, _ in wk]
    holes = sum(h for _, h in wk)
    mean = sum(pts) / len(pts)
    worst = min(pts)
    return {"mean": mean, "worst": worst, "holes": holes,
            "score": 0.8 * mean + 0.2 * worst}   # reward floor as well as ceiling


def best_lineup_points(roster, cfg, byes=None):
    """Back-compat season-total view (bye-agnostic) used by older callers."""
    if byes is None:
        byes = {}
    return evaluate(roster, cfg, byes)["mean"] * 17.0


def opponent_pick(avail, roster, cfg, rnd, rounds, rng):
    """ADP-driven with need awareness and end-of-draft K/DEF behaviour."""
    counts = {}
    for p in roster:
        counts[p["pos"]] = counts.get(p["pos"], 0) + 1
    need_kdef = rounds - rnd <= 1
    cands = []
    for p in avail[:80]:
        pos = p["pos"]
        if pos in ("K", "DEF") and not need_kdef:
            continue
        if not need_kdef and counts.get(pos, 0) >= cfg["roster_slots"].get(pos, 0) + 2:
            continue
        adp = p.get("adp") or 300
        cands.append((adp + rng.gauss(0, 6), p))
    if not cands:
        # forced: take K/DEF or anything left
        for p in avail:
            if p["pos"] in ("K", "DEF") and counts.get(p["pos"], 0) == 0:
                return p
        return avail[0]
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def run_mock(my_slot, cfg, board, rng, verbose=False):
    st = D.DraftState(cfg, board)
    st.my_slot = my_slot
    rosters = {s: [] for s in range(1, cfg["teams"] + 1)}
    taken = set()
    picks_made = 0
    order_log = []

    for rnd in range(1, st.rounds + 1):
        slots = range(1, cfg["teams"] + 1) if rnd % 2 == 1 else range(cfg["teams"], 0, -1)
        for slot in slots:
            avail = [p for p in board if p["pid"] not in taken]
            if not avail:
                break
            if slot == my_slot:
                st.drafted = {pid: 1 for pid in taken}
                st.my_roster = rosters[slot]
                cands, meta = D.recommend(st, picks_made, top_n=1)
                pick = cands[0]["player"]
                if verbose:
                    order_log.append((rnd, picks_made + 1, pick, cands[0], meta))
            else:
                pick = opponent_pick(avail, rosters[slot], cfg, rnd, st.rounds, rng)
            taken.add(pick["pid"])
            rosters[slot].append(pick)
            picks_made += 1

    byes = load_byes()
    evals = {s: evaluate(r, cfg, byes) for s, r in rosters.items()}
    scores = {s: e["score"] for s, e in evals.items()}
    return rosters, scores, order_log


def main():
    cfg = load_config()
    board, _, _ = build_board(cfg)
    n_trials = 3
    all_ranks, all_margins = [], []

    for slot in range(1, cfg["teams"] + 1):
        ranks, margins = [], []
        for t in range(n_trials):
            rng = random.Random(1000 + slot * 37 + t)
            rosters, scores, _ = run_mock(slot, cfg, board, rng)
            mine = scores[slot]
            others = [v for k, v in scores.items() if k != slot]
            rank = 1 + sum(1 for v in others if v > mine)
            ranks.append(rank)
            margins.append(mine - statistics.mean(others))
        all_ranks.extend(ranks); all_margins.extend(margins)
        print(f"  draft slot {slot:>2}: avg finish {statistics.mean(ranks):.2f} of {cfg['teams']}"
              f"   |  +{statistics.mean(margins):6.1f} proj pts vs field avg")

    print(f"\n  OVERALL: avg projected finish {statistics.mean(all_ranks):.2f} / {cfg['teams']}"
          f"  |  avg margin +{statistics.mean(all_margins):.1f} pts")
    print(f"  1st-place rate: {100*sum(1 for r in all_ranks if r==1)/len(all_ranks):.0f}%"
          f"   top-3 rate: {100*sum(1 for r in all_ranks if r<=3)/len(all_ranks):.0f}%")


if __name__ == "__main__":
    main()
