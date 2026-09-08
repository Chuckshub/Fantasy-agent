#!/usr/bin/env python3
"""The weekly briefing: who starts, who sits, why, and where the edge is.

`lineup.py` decides. This explains the decision, because a number with no
reasoning behind it cannot be checked - and an operator that cannot be checked
gets trusted when it is wrong.

    python3 engine/brief.py                 this week, printed
    python3 engine/brief.py --week 5
    python3 engine/brief.py --json          machine-readable, for the agent

Three sections, answering three different questions.

**Why is each starter in?** The projection is broken back into its parts: the
composite the model actually uses (0.8 * Sleeper's weekly number + 0.2 * the
player's season-to-date mean), plus the availability multiplier that scales it.
Nothing else moves the number, and the brief says so.

**Why is each bench player out?** Either he cannot play - bye, Out, IR, and
those are facts rather than judgements - or somebody beat him, in which case
the brief names who and by how much. "Benched" with no margin attached is the
kind of output that hides a bug.

**Where is the edge?** Two honest numbers. The first is availability hygiene:
how many roster spots would have scored zero this week if nobody checked, which
is the one effect this project has actually measured (+2.18 wins a season,
MODEL.md). The second is the margin against the specific team we play, with
their own lineup run through the same engine.

Context that is deliberately NOT in the projection - defense-vs-position, the
market's implied team total, durability - is reported in its own section and
labelled as context. Every one of them was backtested and none of them improved
weekly prediction (MODEL.md, "Rejected"). Showing them is useful; letting them
move a lineup is not, and blurring that line would make this document a liar.
"""
import sys, os, json, argparse, statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import lineup as LU
import model as MO
import history as HI
import sync as SY
import value_trade as VT
from value import build_board, load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ rosters
def latest_snapshot(con):
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    return row["s"] if row and row["s"] else None


def roster_for(con, snap, owner_id, by_pid):
    pids, starters = [], set()
    for r in con.execute("SELECT pid, is_starter FROM ownership "
                         "WHERE snapshot_id=? AND owner_id=?", (snap, owner_id)):
        pids.append(r["pid"])
        if r["is_starter"]:
            starters.add(r["pid"])
    return [by_pid[p] for p in pids if p in by_pid], starters


def roster_by_roster_id(con, snap, roster_id, by_pid):
    pids, starters = [], set()
    for r in con.execute("SELECT pid, is_starter FROM ownership "
                         "WHERE snapshot_id=? AND roster_id=?", (snap, roster_id)):
        pids.append(r["pid"])
        if r["is_starter"]:
            starters.add(r["pid"])
    return [by_pid[p] for p in pids if p in by_pid], starters


def our_opponent(cfg, week, con, snap):
    """(roster_id, manager name) of the team we play this week, or (None, None)."""
    rosters = SY.get(f"{SY.API}/league/{cfg['league_id']}/rosters") or []
    ours = next((r for r in rosters
                 if str(r.get("owner_id")) == str(cfg.get("user_id"))), None)
    if not ours:
        return None, None
    ms = SY.get(f"{SY.API}/league/{cfg['league_id']}/matchups/{week}") or []
    mine = next((m for m in ms if m.get("roster_id") == ours.get("roster_id")), None)
    if not mine or mine.get("matchup_id") is None:
        return None, None
    opp = next((m for m in ms
                if m.get("matchup_id") == mine["matchup_id"]
                and m.get("roster_id") != ours["roster_id"]), None)
    if not opp:
        return None, None
    rid = opp["roster_id"]
    row = con.execute("SELECT team_name, username FROM manager WHERE roster_id=?",
                      (rid,)).fetchone()
    name = (row["team_name"] or row["username"]) if row else f"roster {rid}"
    return rid, name


# -------------------------------------------------------------------- pieces
def explain_starter(p, week):
    """Break a starter's number into the parts the model actually uses."""
    raw = p.get("week_raw")
    std = p.get("season_to_date")
    parts = []
    if raw is not None:
        parts.append(f"Sleeper wk proj {raw:.1f}")
    if std is not None:
        parts.append(f"season avg {std:.1f}")
    if raw is not None and std is not None:
        parts.append(f"blend {MO.composite(raw, std):.1f}")
    if p.get("mult", 1.0) != 1.0:
        parts.append(f"x{p['mult']:.2f} ({p.get('reason') or 'health'})")
    return "; ".join(parts) or "no weekly projection - season pace used"


def slot_assignment(lineup, cfg):
    """{pid: slot} - which slot each starter actually occupies.

    `optimal_lineup` returns the chosen players but not the slots they fill, and
    the distinction decides who a bench player is really competing with. A WR is
    not competing with the tight end holding the TE slot; he is competing with
    the weakest player in a WR or FLEX slot. Getting this wrong made the brief
    claim an 11.5-point receiver was "tied" with a 9.4-point tight end.

    The same greedy rule as `optimal_lineup` is replayed over the chosen eleven,
    which is deterministic and does not depend on the order they came back in.
    """
    pool = sorted(lineup, key=lambda p: -(p.get("proj") or 0))
    used, where = set(), {}
    for pos, n in cfg["roster_slots"].items():
        if pos in ("FLEX", "SUPERFLEX"):
            continue
        taken = 0
        for p in pool:
            if taken >= n:
                break
            if p["pid"] in used or p["pos"] != pos:
                continue
            used.add(p["pid"]); where[p["pid"]] = pos; taken += 1
    flex_el = set(cfg["flex_eligible"])
    for _ in range(cfg["roster_slots"].get("FLEX", 0)):
        pick = next((p for p in pool
                     if p["pid"] not in used and p["pos"] in flex_el), None)
        if pick:
            used.add(pick["pid"]); where[pick["pid"]] = "FLEX"
    for _ in range(cfg["roster_slots"].get("SUPERFLEX", 0)):
        elig = flex_el | {"QB"}
        pick = next((p for p in pool
                     if p["pid"] not in used and p["pos"] in elig), None)
        if pick:
            used.add(pick["pid"]); where[pick["pid"]] = "SUPERFLEX"
    return where


def eligible_slots(pos, cfg):
    """The slots a player at `pos` could actually occupy."""
    out = set()
    if pos in cfg["roster_slots"]:
        out.add(pos)
    if pos in set(cfg["flex_eligible"]):
        out.add("FLEX")
    if cfg.get("superflex") or "SUPERFLEX" in cfg["roster_slots"]:
        if pos in set(cfg["flex_eligible"]) | {"QB"}:
            out.add("SUPERFLEX")
    return out


def explain_bench(p, lineup, where, cfg):
    """Why this player is not starting: a fact, or a name and a margin."""
    if p.get("mult", 1.0) == 0:
        return f"cannot play - {p.get('reason')}"
    mine = eligible_slots(p["pos"], cfg)
    # Only the starters occupying a slot this player could have taken.
    rivals = [q for q in lineup if where.get(q["pid"]) in mine]
    if not rivals:
        return f"no lineup slot a {p['pos']} can fill"
    worst = min(rivals, key=lambda x: x["proj"])
    gap = worst["proj"] - p["proj"]
    slot = where.get(worst["pid"])
    if gap <= 0.05:
        return (f"level with {worst['name']} ({slot}, {worst['proj']:.1f}) - "
                f"either is defensible")
    return (f"{gap:.1f} behind {worst['name']}, the weakest player he could "
            f"displace ({slot} slot, {worst['proj']:.1f})")


def context_notes(eff, season, week, con):
    """Signals we track but deliberately do not apply. Labelled as such."""
    notes = []
    try:
        dvp = HI.defense_vs_position(str(int(season) - 1))
    except Exception:
        dvp = {}
    try:
        vegas = MO.implied_totals(con)
    except Exception:
        vegas = {}
    for p in eff:
        if p.get("mult", 1.0) == 0:
            continue
        bits = []
        opp = p.get("opponent")
        cell = (dvp.get(p["pos"]) or {}).get(opp) if opp else None
        if cell:
            tone = "generous" if cell["z"] > 0.5 else "stingy" if cell["z"] < -0.5 else "average"
            bits.append(f"{opp} was {tone} to {p['pos']} last year "
                        f"({cell['ppg']:.1f} PPR/gm, z {cell['z']:+.2f})")
        it = vegas.get((str(season), int(week), p.get("team")))
        if it is not None:
            bits.append(f"market implies {it:.1f} team points")
        if bits:
            notes.append({"name": p["name"], "pos": p["pos"], "notes": bits})
    return notes


# --------------------------------------------------------------------- build
def build(week=None, season="2026", cfg=None):
    cfg = cfg or load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = str(st.get("season") or season)
    week = int(week or st.get("week") or 1)

    con = DB.connect()
    board, _, _ = build_board(cfg)
    by_pid = {p["pid"]: p for p in board}
    snap = latest_snapshot(con)
    if not snap:
        raise SystemExit("no ownership snapshot - run: python3 engine/track.py --sync")

    roster, starters = roster_for(con, snap, cfg.get("user_id"), by_pid)
    if not roster:
        raise SystemExit("ownership snapshot holds no players for us")
    res = LU.analyse(roster, cfg, week, current_starters=starters or None,
                     season=season)

    lineup = sorted(res["lineup"], key=lambda x: -x["proj"])
    total = sum(p["proj"] for p in lineup)

    where = slot_assignment(lineup, cfg)
    starts = [{"name": p["name"], "pos": p["pos"], "team": p.get("team"),
               "slot": where.get(p["pid"], p["pos"]),
               "opponent": p.get("opponent"), "proj": p["proj"],
               "why": explain_starter(p, week)} for p in lineup]

    sits = []
    for p in sorted(res["bench"] + res["unavailable"], key=lambda x: -x["proj"]):
        sits.append({"name": p["name"], "pos": p["pos"], "proj": p["proj"],
                     "why": explain_bench(p, lineup, where, cfg)})

    # ---- edge 1: availability hygiene, the only measured effect in this project
    zeroed = [p for p in res["eff"] if p.get("mult", 1.0) == 0]
    would_have_started = [p for p in zeroed if p["pid"] in (starters or set())]
    hygiene = {
        "unplayable_on_roster": [{"name": p["name"], "pos": p["pos"],
                                  "reason": p.get("reason")} for p in zeroed],
        "caught_in_lineup": [{"name": p["name"], "reason": p.get("reason")}
                             for p in would_have_started],
    }

    # ---- edge 2: the actual opponent, through the same engine
    opp_block = None
    rid, opp_name = our_opponent(cfg, week, con, snap)
    if rid:
        opp_roster, opp_starters = roster_by_roster_id(con, snap, rid, by_pid)
        if opp_roster:
            opp_eff = LU.effective(opp_roster, week, cfg, season)
            playable = [p for p in opp_eff if p["mult"] > 0]
            opp_best, _, _ = VT.optimal_lineup(playable, cfg["roster_slots"],
                                               set(cfg["flex_eligible"]))
            opp_set = [p for p in opp_eff if p["pid"] in opp_starters]
            opp_block = {
                "name": opp_name,
                "their_best": round(sum(p["proj"] for p in opp_best), 1),
                "their_current": round(sum(p["proj"] for p in opp_set), 1)
                                 if opp_set else None,
                "margin_vs_best": round(total - sum(p["proj"] for p in opp_best), 1),
                "their_unplayable": [p["name"] for p in opp_eff
                                     if p["mult"] == 0 and p["pid"] in opp_starters],
            }

    return {
        "season": season, "week": week,
        "projected_total": round(total, 1),
        "starters": starts,
        "bench": sits,
        "problems": res.get("problems") or [],
        "swaps": [{"start": s["start"]["name"] if s["start"] else None,
                   "sit": s["sit"]["name"], "gain": s["gain"]}
                  for s in (res.get("swaps") or [])],
        "hygiene": hygiene,
        "opponent": opp_block,
        "context": context_notes(res["eff"], season, week, con),
    }


# --------------------------------------------------------------------- print
def render(b):
    out = []
    A = out.append
    A(f"WEEK {b['week']} BRIEFING - {b['season']}")
    A(f"projected {b['projected_total']:.1f} points\n")

    A("WHY THESE TEN ARE IN")
    for s in b["starters"]:
        opp = f" vs {s['opponent']}" if s.get("opponent") else ""
        A(f"  {s.get('slot', s['pos']):<5}{s['name']:<24}{s['proj']:>6.1f}{opp}")
        A(f"        {s['why']}")

    A("\nWHY THE REST ARE OUT")
    for s in b["bench"]:
        A(f"  {s['pos']:<4}{s['name']:<24}{s['proj']:>6.1f}")
        A(f"        {s['why']}")

    h = b["hygiene"]
    A("\nWHERE THE EDGE IS")
    n = len(h["unplayable_on_roster"])
    if n:
        A(f"  {n} player(s) on this roster score zero this week:")
        for p in h["unplayable_on_roster"]:
            A(f"     {p['name']} ({p['pos']}) - {p['reason']}")
        if h["caught_in_lineup"]:
            A(f"  {len(h['caught_in_lineup'])} of them were in the lineup and "
              f"have been taken out. That check is the whole measured edge:")
            A( "  +0.128 win rate, +2.18 wins a season (MODEL.md).")
        else:
            A("  None of them were in the lineup - the check found nothing to fix,")
            A("  which is the check working, not the check being unnecessary.")
    else:
        A("  Every player on this roster has a game and is healthy this week.")

    o = b.get("opponent")
    if o:
        A(f"\n  Opponent: {o['name']}")
        A(f"     their best possible lineup : {o['their_best']:.1f}")
        if o["their_current"] is not None:
            slack = o["their_best"] - o["their_current"]
            A(f"     what they have set        : {o['their_current']:.1f}"
              f"  ({slack:+.1f} left on their bench)")
        A(f"     our margin vs their best  : {o['margin_vs_best']:+.1f}")
        if o["their_unplayable"]:
            A(f"     they are starting {len(o['their_unplayable'])} player(s) who "
              f"cannot play: {', '.join(o['their_unplayable'])}")

    if b["problems"]:
        A("\nPROBLEMS")
        for p in b["problems"]:
            A(f"  - {p}")

    if b["context"]:
        A("\nCONTEXT - tracked, shown, and deliberately not applied")
        A("  (defense-vs-position and the market were both backtested and neither")
        A("   improved weekly prediction, so neither moves a projection above.)")
        for c in b["context"][:10]:
            A(f"  {c['name']} ({c['pos']}): {'; '.join(c['notes'])}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", default="2026")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    b = build(a.week, a.season)
    print(json.dumps(b, indent=1) if a.json else render(b))


if __name__ == "__main__":
    main()
