#!/usr/bin/env python3
"""Valuation board: projections -> VORP -> tiers. Zero third-party deps."""
import json, os, math

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")

# Injury designations that should suppress or zero a player's draft value.
INJ_PENALTY = {"IR": 0.0, "PUP": 0.35, "NFI": 0.35, "Sus": 0.55,
               "Out": 0.6, "Doubtful": 0.75, "Questionable": 0.95, None: 1.0}


def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


def load_config():
    with open(os.path.join(HERE, "config.json")) as f:
        return json.load(f)


def display_name(pid, players):
    p = players.get(pid) or {}
    if p.get("position") == "DEF" or p.get("fantasy_positions") == ["DEF"]:
        return f"{p.get('team') or pid} DEF"
    fn = p.get("full_name")
    if fn:
        return fn
    return f"{p.get('first_name','?')} {p.get('last_name','?')}".strip()


def build_pool(cfg):
    """Return list of player dicts with projection, adp, injury-adjusted value."""
    proj = load("projections_2026.json")
    players = load("players_nfl.json")
    prior = load("stats_2025.json")
    # Sleeper ships no bye_week field, so byes come from the derived schedule.
    try:
        sched = load("schedule_2026.json")
    except Exception:
        sched = {}
    byes = sched.get("byes") or {}
    sos = sched.get("sos") or {}
    scoring = cfg["scoring"]
    pts_key, adp_key = f"pts_{scoring}", f"adp_{scoring}"

    pool = []
    for pid, rec in proj.items():
        st = rec.get("stats") or {}
        pts = st.get(pts_key)
        if pts is None:
            continue
        meta = players.get(pid) or {}
        pos = meta.get("position") or rec.get("position")
        if pos not in ("QB", "RB", "WR", "TE", "K", "DEF"):
            continue
        # DEF records are team-level and always "active"
        if pos != "DEF" and not meta.get("active", True):
            continue

        pts *= CALIBRATION.get(pos, 1.0)

        adp = st.get(adp_key)
        if adp is None or adp >= 900:
            adp = None

        inj = meta.get("injury_status")
        mult = INJ_PENALTY.get(inj, 1.0)

        pstats = (prior.get(pid) or {}).get("stats") or {}
        pool.append({
            "pid": pid,
            "name": display_name(pid, players),
            "pos": pos,
            "team": meta.get("team") or rec.get("team"),
            "bye": byes.get((meta.get("team") or rec.get("team") or "").upper()),
            "sos_playoffs": (sos.get((meta.get("team") or rec.get("team") or "").upper()) or {}).get("sos_playoffs"),
            "age": meta.get("age"),
            "years_exp": meta.get("years_exp"),
            "proj": pts,
            "value": pts * mult,          # injury-adjusted projection
            "injury": inj,
            "adp": adp,
            "prior_pts": pstats.get(f"pts_{scoring}"),
            "prior_gp": pstats.get("gp"),
        })
    return pool


def replacement_levels(pool, cfg):
    """Self-calibrating replacement baselines.

    Start from mandatory starters, then hand out each league-wide FLEX slot to
    whichever eligible position currently has the best *next* player available.
    This adapts to any roster shape instead of hard-coding a flex split.
    """
    teams = cfg["teams"]
    slots = cfg["roster_slots"]
    by_pos = {}
    for p in pool:
        by_pos.setdefault(p["pos"], []).append(p)
    for lst in by_pos.values():
        lst.sort(key=lambda x: -x["value"])

    counts = {pos: teams * n for pos, n in slots.items()
              if pos not in ("FLEX", "SUPERFLEX")}

    flex_total = teams * slots.get("FLEX", 0)
    elig = [p for p in cfg["flex_eligible"] if p in by_pos]
    for _ in range(flex_total):
        best_pos, best_val = None, -1e9
        for pos in elig:
            idx = counts.get(pos, 0)
            lst = by_pos.get(pos, [])
            val = lst[idx]["value"] if idx < len(lst) else -1e9
            if val > best_val:
                best_pos, best_val = pos, val
        if best_pos:
            counts[best_pos] = counts.get(best_pos, 0) + 1

    if cfg.get("superflex"):
        for _ in range(teams * slots.get("SUPERFLEX", 0)):
            best_pos, best_val = None, -1e9
            for pos in ["QB"] + elig:
                idx = counts.get(pos, 0)
                lst = by_pos.get(pos, [])
                val = lst[idx]["value"] if idx < len(lst) else -1e9
                if val > best_val:
                    best_pos, best_val = pos, val
            if best_pos:
                counts[best_pos] = counts.get(best_pos, 0) + 1

    repl = {}
    for pos, lst in by_pos.items():
        idx = min(counts.get(pos, len(lst)), len(lst) - 1)
        repl[pos] = lst[idx]["value"] if lst else 0.0
    return repl, counts


def assign_tiers(pool_pos, gap_mult=0.85):
    """Tier break when the drop to the next player exceeds gap_mult * stdev of drops."""
    if not pool_pos:
        return
    drops = [pool_pos[i]["vorp"] - pool_pos[i + 1]["vorp"]
             for i in range(len(pool_pos) - 1)]
    if not drops:
        pool_pos[0]["tier"] = 1
        return
    mean = sum(drops) / len(drops)
    var = sum((d - mean) ** 2 for d in drops) / len(drops)
    sd = math.sqrt(var)
    thresh = mean + gap_mult * sd
    tier = 1
    pool_pos[0]["tier"] = 1
    for i, d in enumerate(drops):
        if d > thresh:
            tier += 1
        pool_pos[i + 1]["tier"] = tier


# Season-total calibration, measured by comparing Sleeper's own weekly
# projections against actual outcomes over 2024 and 2025 (see MODEL.md).
#
# Only two positions earn a correction, because only two were *stable across
# both seasons* and improved season-total accuracy when a factor fitted on one
# year was applied blind to the next:
#
#   QB   sum(actual)/sum(projected) = 0.920 (2024), 0.870 (2025)
#        applying the 2024 factor to 2025 cut QB season-total MAE by 31.8%
#   DEF  1.065 / 1.084, a smaller but equally consistent under-projection
#
# RB, WR, TE and K all looked correctable on 2024 alone and were not: their
# factors collapsed toward 1.0 in 2025, and applying the 2024 numbers made
# every one of them worse (RB -29%, WR -16%, TE -7%, K -13%). They are left
# alone deliberately.
CALIBRATION = {"QB": 0.90, "DEF": 1.07}


def build_board(cfg=None):
    cfg = cfg or load_config()
    pool = build_pool(cfg)
    repl, counts = replacement_levels(pool, cfg)
    for p in pool:
        p["repl"] = repl.get(p["pos"], 0.0)
        p["vorp"] = p["value"] - p["repl"]
    by_pos = {}
    for p in pool:
        by_pos.setdefault(p["pos"], []).append(p)
    for pos, lst in by_pos.items():
        lst.sort(key=lambda x: -x["vorp"])
        assign_tiers(lst)
        for i, p in enumerate(lst, 1):
            p["pos_rank"] = i
    pool.sort(key=lambda x: -x["vorp"])
    for i, p in enumerate(pool, 1):
        p["overall_rank"] = i
    return pool, repl, counts


if __name__ == "__main__":
    cfg = load_config()
    board, repl, counts = build_board(cfg)
    print(f"League: {cfg['teams']}-team {cfg['scoring']}  slots={cfg['roster_slots']}")
    print("\nReplacement baselines (pts) / starters absorbed league-wide:")
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        if pos in repl:
            print(f"  {pos:<4} repl={repl[pos]:7.1f}  players_rostered_as_starters={counts.get(pos)}")
    print(f"\n{'#':>3} {'PLAYER':<24}{'POS':<5}{'TM':<4}{'PROJ':>7}{'VORP':>8}{'TIER':>5}{'ADP':>7}")
    for p in board[:40]:
        adp = f"{p['adp']:.1f}" if p["adp"] else "-"
        print(f"{p['overall_rank']:>3} {p['name']:<24}{p['pos']:<5}{str(p['team'] or '-'):<4}"
              f"{p['proj']:>7.1f}{p['vorp']:>8.1f}{p.get('tier',0):>5}{adp:>7}")
