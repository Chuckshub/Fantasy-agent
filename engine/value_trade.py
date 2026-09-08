#!/usr/bin/env python3
"""Roster and trade valuation.

A trade is worth making when it raises our *expected starting-lineup points*,
not when it wins on raw totals. Two RB2s for one RB1 usually loses, because
only one of the two ever enters the lineup.

    roster_value(roster) = points of the optimal legal lineup
                           + bench contribution at a steep discount

Three rules learned the hard way while testing this (an earlier version got all
three wrong and happily proposed trades no human would accept):

1. **An empty starter slot is not free.** If a trade leaves a side without a
   kicker, they sign one off waivers the same day. Unfilled slots are scored at
   replacement level, not zero - otherwise the model thinks it can strip a
   roster bare at no cost.
2. **Roster limits are real.** A 1-for-2 needs an open bench spot. Trades that
   overflow either side's roster are illegal, not merely expensive.
3. **A trade the other side barely wants is not a trade.** Requiring only
   `their_gain > 0` produced offers at +282 for us and +3 for them. Acceptance
   needs a meaningful share of the surplus, or the offer just burns credibility
   with that manager - which matters a great deal when offers send themselves.
4. **Bench value depends on the position.** A backup QB in a 1-QB league is
   worth almost nothing; a backup RB enters the lineup constantly through flex
   and injury. Weighting all bench points equally made a spare quarterback look
   like a 66-point asset and produced trades that shipped out a starting RB1
   for one.
"""
import sys, os, itertools
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BENCH_WEIGHT = 0.22          # a bench player contributes a fraction of its points
DEPTH_DECAY = 0.55           # each additional backup at a position is worth less
GAMES = 17                   # projections are season totals; report per week

# A counterparty must capture at least this share of the total surplus, and at
# least this many points per week, or they have no reason to click accept.
MIN_THEIR_SHARE = 0.30
MIN_THEIR_WEEKLY = 0.75


def startability(pos, cfg):
    """How readily a bench player at this position reaches the lineup.

    Driven by how many lineup slots the position can legally fill: a QB fills
    one in a 1-QB league, while an RB fills two plus both flexes.
    """
    slots = cfg["roster_slots"]
    flex = set(cfg.get("flex_eligible") or ("RB", "WR", "TE"))
    n = slots.get(pos, 0)
    if pos in flex:
        n += slots.get("FLEX", 0)
    if pos in flex or pos == "QB":
        n += slots.get("SUPERFLEX", 0)
    return min(1.0, n / 2.0)


def replacement_levels(board):
    """{pos: replacement-level season points}, straight off the scored board."""
    out = {}
    for p in board:
        r = p.get("repl")
        if r is not None and p["pos"] not in out:
            out[p["pos"]] = r
    return out


def optimal_lineup(players, slots, flex_eligible):
    """Greedy by position, then flex. Returns (lineup, bench, unfilled)."""
    pool = sorted(players, key=lambda p: -(p.get("proj") or 0))
    used, lineup, unfilled = set(), [], {}
    for pos, n in slots.items():
        if pos in ("FLEX", "SUPERFLEX"):
            continue
        taken = 0
        for p in pool:
            if taken >= n:
                break
            if id(p) in used or p["pos"] != pos:
                continue
            used.add(id(p)); lineup.append(p); taken += 1
        if taken < n:
            unfilled[pos] = n - taken
    for _ in range(slots.get("FLEX", 0)):
        pick = next((p for p in pool
                     if id(p) not in used and p["pos"] in flex_eligible), None)
        if pick is None:
            unfilled["FLEX"] = unfilled.get("FLEX", 0) + 1
        else:
            used.add(id(pick)); lineup.append(pick)
    for _ in range(slots.get("SUPERFLEX", 0)):
        elig = set(flex_eligible) | {"QB"}
        pick = next((p for p in pool if id(p) not in used and p["pos"] in elig), None)
        if pick is None:
            unfilled["SUPERFLEX"] = unfilled.get("SUPERFLEX", 0) + 1
        else:
            used.add(id(pick)); lineup.append(pick)
    bench = [p for p in pool if id(p) not in used]
    return lineup, bench, unfilled


def roster_value(players, cfg, repl=None):
    """Expected season starting points, with depth and waiver backfill priced in."""
    slots = cfg["roster_slots"]
    flex = set(cfg.get("flex_eligible") or ("RB", "WR", "TE"))
    repl = repl or {}
    lineup, bench, unfilled = optimal_lineup(players, slots, flex)
    total = sum((p.get("proj") or 0) for p in lineup)
    # rule 1: a hole gets filled from waivers at replacement level, not left at 0
    for pos, n in unfilled.items():
        base = repl.get(pos) if pos not in ("FLEX", "SUPERFLEX") else \
            min([repl[q] for q in flex if q in repl] or [0])
        total += (base or 0) * n
    seen = {}
    for p in sorted(bench, key=lambda x: -(x.get("proj") or 0)):
        pos = p["pos"]
        k = seen.get(pos, 0)
        total += ((p.get("proj") or 0) * BENCH_WEIGHT           # rule 4
                  * startability(pos, cfg) * (DEPTH_DECAY ** k))
        seen[pos] = k + 1
    return total


def roster_limit(cfg):
    return sum(cfg["roster_slots"].values()) + cfg.get("bench_slots", 0)


def evaluate_trade(our_players, their_players, give, get, cfg, repl=None):
    """Value of a proposed swap to both sides, per week."""
    gid, tid = {id(p) for p in give}, {id(p) for p in get}
    ours_after = [p for p in our_players if id(p) not in gid] + list(get)
    theirs_after = [p for p in their_players if id(p) not in tid] + list(give)
    lim = roster_limit(cfg)
    legal = len(ours_after) <= lim and len(theirs_after) <= lim       # rule 2
    ov0 = roster_value(our_players, cfg, repl)
    ov1 = roster_value(ours_after, cfg, repl)
    tv0 = roster_value(their_players, cfg, repl)
    tv1 = roster_value(theirs_after, cfg, repl)
    our_gain, their_gain = (ov1 - ov0) / GAMES, (tv1 - tv0) / GAMES
    surplus = our_gain + their_gain
    share = (their_gain / surplus) if surplus > 0 else 0.0
    return {
        "our_gain": round(our_gain, 2),
        "their_gain": round(their_gain, 2),
        "their_share": round(share, 2),
        "legal": legal,
        "acceptable": bool(                                           # rule 3
            legal and our_gain > 0 and their_gain >= MIN_THEIR_WEEKLY
            and share >= MIN_THEIR_SHARE),
        "give": [p["name"] for p in give],
        "get": [p["name"] for p in get],
    }


def find_trades(our_players, their_players, cfg, repl=None, max_per_side=2,
                top_n=5, require_acceptable=True, min_gain=0.25):
    """Search small swaps. Ranked by our gain, filtered by plausible acceptance.

    `min_gain` is points per week, so 0.25 is a real but modest edge.
    """
    out = []
    for a in range(1, max_per_side + 1):
        for b in range(1, max_per_side + 1):
            for give in itertools.combinations(our_players, a):
                for get in itertools.combinations(their_players, b):
                    ev = evaluate_trade(our_players, their_players,
                                        list(give), list(get), cfg, repl)
                    if not ev["legal"] or ev["our_gain"] < min_gain:
                        continue
                    if require_acceptable and not ev["acceptable"]:
                        continue
                    out.append(ev)
    out.sort(key=lambda e: -e["our_gain"])
    return out[:top_n]
