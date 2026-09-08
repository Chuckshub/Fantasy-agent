#!/usr/bin/env python3
"""Find the weeks we cannot field a legal lineup, and fix them before they land.

This exists because of a hole the rest of the system can see but cannot close.
`lineup.py` optimises among the players we own; `waivers.py` finds in-season
breakouts from recent form. Neither answers the question this roster actually
poses:

    week  6  short RB, DEF
    week  7  short QB, K
    week  8  short RB

Those are not bad luck and they are not a projection problem. They are a roster
construction problem, knowable in week 1 from the bye schedule alone, and no
amount of lineup optimisation fixes a week where there is no legal lineup to
find. An empty starter slot scores zero, which is the single most expensive
thing that can happen to a fantasy week.

    python3 engine/depth.py --holes            every unfillable week ahead
    python3 engine/depth.py --plan             what to acquire, and when
    python3 engine/depth.py --plan --week 6    as it would look in week 6

**Timing is the point.** Rostering a backup quarterback in week 1 for a week 7
bye burns a bench spot for six weeks and is how a roster ends up with no room to
respond to anything else. So a hole is only acted on once it is within
`ACT_WITHIN` weeks; before that it is reported and watched. The recommendation
is deliberately "stream a body the week before", not "carry insurance all
season".

The candidate must actually *play* in the week with the hole - a replacement
quarterback who is himself on bye in week 7 closes nothing - which is the check
that makes this different from simply taking the best free agent at a position.
"""
import sys, os, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import lineup as LU
import waivers as WV
import sync as SY
from value import build_board, load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How far ahead to look for unfillable weeks.
HORIZON = 14
# How close a hole must be before we spend a roster spot closing it.
ACT_WITHIN = 2
# How many candidates to show per hole.
TOP_N = 5
# Acquisitions per run. A bench has three spots; spending them all in one
# go on holes that are still weeks away leaves nothing for an injury.
MAX_MOVES_PER_RUN = 2


def our_roster(cfg, board=None):
    board = board if board is not None else build_board(cfg)[0]
    by = {p["pid"]: p for p in board}
    con = DB.connect()
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        raise SystemExit("no ownership snapshot - run: python3 engine/track.py --sync")
    pids = [r["pid"] for r in con.execute(
        "SELECT pid FROM ownership WHERE snapshot_id=? AND owner_id=?",
        (row["s"], cfg.get("user_id")))]
    return [by[p] for p in pids if p in by]


def last_week(default=18):
    """The last week the schedule actually contains.

    Without this the forecast runs off the end of the season and every position
    reports a hole in week 19, because no team has a game then - a fake crisis
    that would have had the agent trying to sign a quarterback for a week that
    does not exist.
    """
    try:
        _, opponents = LU.load_byes()
    except Exception:
        return default
    weeks = set()
    for sched in (opponents or {}).values():
        for w in (sched or {}):
            try:
                weeks.add(int(w))
            except (TypeError, ValueError):
                continue
    return max(weeks) if weeks else default


def holes(cfg, roster, start_week, season="2026", horizon=HORIZON):
    """Every week ahead where a required starter slot cannot be filled."""
    out = []
    end = last_week()
    span = max(0, min(horizon, end - start_week + 1))
    if span <= 0:
        return out
    rows = LU.forecast(roster, cfg, span, season, start_week=start_week)
    for r in rows:
        if r["unfilled"]:
            out.append({"week": r["week"], "short": dict(r["unfilled"]),
                        "points": r["points"],
                        "unavailable": r["unavailable"]})
    return out


def plays_in_week(player, week, byes, opponents):
    """True if this player's team has a game that week. A replacement who is
    himself on bye closes nothing, which is the whole trap here."""
    team = player.get("team")
    if not team:
        return False
    if byes.get(team) == week:
        return False
    opp = opponents.get(team)
    if opp is not None and str(week) not in opp:
        return False
    return True


def candidates(cfg, pos, week, roster, season="2026", top_n=TOP_N, board=None):
    """Best available free agents at `pos` who actually play in `week`."""
    board = board if board is not None else build_board(cfg)[0]
    taken = WV.rostered_pids(cfg)
    byes, opponents = LU.load_byes()
    pool = [p for p in board
            if p["pos"] == pos
            and p["pid"] not in taken
            and plays_in_week(p, week, byes, opponents)]
    if not pool:
        return []
    # Rank by what they are worth in that week specifically. Before the season
    # there is no weekly projection for a week 7, and effective() falls back to
    # a per-game share of the season projection - which is the right ordering
    # for "who is the best body available", even if the absolute number is soft.
    eff = LU.effective(pool[:80], week, cfg, season)
    eff.sort(key=lambda x: -(x.get("proj") or 0))
    return eff[:top_n]


def build_plan(cfg, start_week, season="2026", horizon=HORIZON,
               act_within=ACT_WITHIN, board=None):
    board = board if board is not None else build_board(cfg)[0]
    roster = our_roster(cfg, board)
    hs = holes(cfg, roster, start_week, season, horizon)
    plan = []
    for h in hs:
        weeks_away = h["week"] - start_week
        for pos, n in h["short"].items():
            cands = candidates(cfg, pos, h["week"], roster, season, board=board)
            plan.append({
                "week": h["week"], "pos": pos, "count": n,
                "weeks_away": weeks_away,
                "act_now": weeks_away <= act_within,
                "candidates": [{"name": c["name"], "pid": c["pid"],
                                "team": c.get("team"), "proj": c.get("proj"),
                                "bye": c.get("bye")} for c in cands],
            })
    return {"week": start_week, "holes": hs, "plan": plan, "roster": len(roster)}


def acquire(cfg, plan, season="2026", week=None, mode="dry-run", verbose=True,
            max_moves=MAX_MOVES_PER_RUN):
    """Close the holes that are close enough to be worth a roster spot.

    Only rows the plan marked `act_now` are touched. Each acquisition picks the
    best candidate who plays in the hole week, and the player dropped to make
    room is chosen by `claim.py`, which refuses to drop anyone who starts this
    week or whose removal would open a *new* hole - otherwise closing week 7
    could quietly create week 9.
    """
    import claim as CL

    week = int(week or plan["week"])
    todo = [i for i in plan["plan"] if i["act_now"] and i["candidates"]]
    # The same free agent often closes more than one hole - one running back
    # covers both the week 6 and the week 8 bye - and signing him twice is not
    # possible. Keep the earliest hole he solves and drop the duplicates.
    seen_pid, deduped = set(), []
    for item in sorted(todo, key=lambda i: i["week"]):
        pid = item["candidates"][0]["pid"]
        if pid in seen_pid:
            continue
        seen_pid.add(pid)
        deduped.append(item)
    # Each acquisition costs a roster spot, and there are only so many surplus
    # players to give up. Closing five holes in one run would strip the bench
    # bare to solve problems that are still weeks away, so this takes the most
    # urgent ones and lets the next scheduled run handle the rest.
    todo = deduped[:max_moves]
    if not todo:
        if verbose:
            print("  no hole is close enough to act on.")
        return {"acted": [], "skipped": []}

    board = build_board(cfg)[0]
    by_pid = {p["pid"]: p for p in board}
    roster = our_roster(cfg, board)
    end = last_week()
    horizon_weeks = list(range(week, min(end, week + HORIZON) + 1))

    acted, skipped = [], []
    pp = CL.PlayersPage(cfg["league_id"])
    try:
        for item in todo:
            cand = item["candidates"][0]
            incoming = by_pid.get(cand["pid"]) or {
                "pid": cand["pid"], "name": cand["name"], "pos": item["pos"],
                "team": cand.get("team"), "proj": cand.get("proj")}
            # Decide the drop by simulating the roster that would result, over
            # every week we can see - not by a rule of thumb about who looks
            # spare this week.
            drop = CL.best_drop_for(cfg, roster, incoming, horizon_weeks, season)
            if not drop:
                skipped.append({"ok": False, "add": cand["name"],
                                "for_week": item["week"], "pos": item["pos"],
                                "why": "no drop improves the roster - "
                                       "leaving it alone"})
                if verbose:
                    print(f"  SKIP add {cand['name']} ({item['pos']}): no drop "
                          f"improves the roster over weeks "
                          f"{horizon_weeks[0]}-{horizon_weeks[-1]}")
                continue
            player = {"pid": cand["pid"], "name": cand["name"],
                      "pos": item["pos"], "team": cand.get("team")}
            r = CL.claim_one(pp, cfg, player, drop, None, week,
                             dry_run=(mode != "submit"))
            r.update({"for_week": item["week"], "pos": item["pos"],
                      "drop_gain": drop.get("gain"),
                      "drop_per_week": drop.get("per_week")})
            CL.log(f"depth {mode}: add {cand['name']} ({item['pos']}) for week "
                   f"{item['week']}, drop {drop['name']} "
                   f"(net {drop.get('gain'):+.1f}) -> {r}")
            if verbose:
                print(f"  {'OK ' if r.get('ok') else 'FAIL'} add {cand['name']} "
                      f"({item['pos']}) for week {item['week']}, drop "
                      f"{drop['name']} (net {drop.get('gain'):+.1f} pts): "
                      f"{r.get('verify') or r.get('why') or 'dry run'}")
            (acted if r.get("ok") else skipped).append(r)
            pp.close_dialog()
            if not r.get("ok"):
                break
            # The roster has changed, so the next decision must be made against
            # the new one rather than the one we started with.
            if mode == "submit":
                roster = our_roster(cfg, board)
            else:
                roster = [p for p in roster if p["pid"] != drop["pid"]] + [incoming]
    finally:
        pp.close_dialog()
        pp.close()
    return {"acted": acted, "skipped": skipped}


def render(p):
    out = []
    A = out.append
    A(f"DEPTH CHECK - from week {p['week']}, {p['roster']} players rostered\n")
    if not p["holes"]:
        A("  No unfillable weeks in the horizon. Every required slot can be filled.")
        return "\n".join(out)
    A("WEEKS WE CANNOT FIELD A LEGAL LINEUP")
    for h in p["holes"]:
        A(f"  week {h['week']:>2}  short {h['short']}  "
          f"(projects {h['points']:.1f} with the slot empty)")
        if h["unavailable"]:
            A(f"            out/bye: {', '.join(h['unavailable'])}")
    A("\nWHAT TO DO")
    for item in p["plan"]:
        when = ("ACT NOW" if item["act_now"]
                else f"watch - {item['weeks_away']} weeks out")
        A(f"  week {item['week']:>2} {item['pos']:<4} x{item['count']}   [{when}]")
        if not item["candidates"]:
            A("      no free agent at this position has a game that week")
        for c in item["candidates"]:
            A(f"      {c['name']:<24}{(c['team'] or '?'):<4}"
              f"{(c['proj'] or 0):>6.1f}  bye {c['bye']}")
    A("\n  Holes are closed by streaming a body the week before, not by carrying")
    A("  insurance all season - a bench spot held for six weeks costs more than")
    A("  it saves. Only the rows marked ACT NOW are worth a roster spot today.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holes", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", default="2026")
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--acquire", action="store_true",
                    help="dry-run the acquisitions for holes that are close enough")
    ap.add_argument("--submit", action="store_true",
                    help="with --acquire, actually make them")
    a = ap.parse_args()

    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = str(st.get("season") or a.season)
    week = int(a.week or st.get("week") or 1)
    p = build_plan(cfg, week, season, a.horizon)
    if a.json:
        print(json.dumps(p, indent=1))
    else:
        print(render(p))
    if a.acquire:
        print("\nACQUIRING" + ("" if a.submit else " (dry run)"))
        acquire(cfg, p, season, week, mode="submit" if a.submit else "dry-run")


if __name__ == "__main__":
    main()
