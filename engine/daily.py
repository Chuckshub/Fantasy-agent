#!/usr/bin/env python3
"""The everyday check: sync, look for anything that needs doing, report.

    python3 engine/daily.py            # report only
    python3 engine/daily.py --act      # also carry out what it finds

Before the draft it watches the board: injury designations that changed, ADP
moves, and whether we are ready for Friday. After the draft it rebalances the
lineup - benching anyone on bye or ruled out, rotating the best available body
into every slot - looks a few weeks ahead for bye pileups, and scans every
other roster for trades.

Run it after games. Lineup changes are only possible before kickoff, so the
rebalance section flags anything whose game date has already arrived rather
than assuming the swap can still be made.

It is written to be safe to run unattended and often. `--act` is the only flag
that changes anything outside this repo.
"""
import sys, os, json, time, argparse, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import track as TR
import sync as SY
import value_trade as VT
import lineup as LU
import scoring as SC
try:
    import notify_discord as ND
except Exception:
    ND = None
from value import build_board, load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(HERE, "logs", "daily_report.md")


def prev_projection_snapshot(con):
    r = con.execute(
        "SELECT id FROM snapshot WHERE kind='sync' ORDER BY id DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    return r["id"] if r else None


def board_deltas(con, board, prev_snap, limit=12):
    """What moved since the previous sync: projections and ADP."""
    if not prev_snap:
        return []
    old = {r["pid"]: r for r in con.execute(
        "SELECT pid, pts, adp FROM projection WHERE snapshot_id=?", (prev_snap,))}
    rows = []
    for p in board:
        o = old.get(p["pid"])
        if not o:
            continue
        dp = (p.get("proj") or 0) - (o["pts"] or 0)
        da = (p.get("adp") or 0) - (o["adp"] or 0) if o["adp"] and p.get("adp") else 0
        if abs(dp) >= 5 or abs(da) >= 3:
            rows.append((abs(dp), p, dp, da))
    rows.sort(key=lambda r: -r[0])
    return [(p, dp, da) for _, p, dp, da in rows[:limit]]


def injury_flags(board, limit=15):
    out = [p for p in board[:120] if p.get("injury")]
    return out[:limit]


def our_roster(con, cfg, board):
    by = {p["pid"]: p for p in board}
    uid = cfg.get("user_id")
    row = con.execute(
        "SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        return []
    pids = [r["pid"] for r in con.execute(
        "SELECT pid FROM ownership WHERE snapshot_id=? AND owner_id=?",
        (row["s"], uid))]
    return [by[p] for p in pids if p in by]


def all_rosters(con, cfg, board):
    by = {p["pid"]: p for p in board}
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        return {}
    out = {}
    for r in con.execute(
            "SELECT owner_id, pid FROM ownership WHERE snapshot_id=?", (row["s"],)):
        if r["pid"] in by:
            out.setdefault(r["owner_id"], []).append(by[r["pid"]])
    return out


def scan_trades(con, cfg, board, top_n=3):
    """Every counterparty, best mutually-acceptable offers."""
    repl = VT.replacement_levels(board)
    rosters = all_rosters(con, cfg, board)
    us = rosters.pop(cfg.get("user_id"), None)
    if not us:
        return None, []
    names = {m["owner_id"]: (m["team_name"] or m["username"] or m["owner_id"])
             for m in con.execute("SELECT * FROM manager")}
    found = []
    for oid, theirs in rosters.items():
        for t in VT.find_trades(us, theirs, cfg, repl, max_per_side=2, top_n=top_n):
            t["with"] = names.get(oid, oid)
            t["owner_id"] = oid
            found.append(t)
    found.sort(key=lambda t: -t["our_gain"])
    return us, found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--act", action="store_true",
                    help="carry out what the check finds (sends trades)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--discord", action="store_true",
                    help="also post the lineup and reasoning to Discord")
    a = ap.parse_args()

    cfg = load_config()
    con = DB.connect()
    res, wk = None, None
    prev = prev_projection_snapshot(con)
    L = []
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    L.append(f"# StatKing daily check - {now}\n")

    # 1. refresh the store
    TR.cmd_sync()
    board, _, _ = build_board(cfg)
    st = TR.nfl_state()
    draft = SY.get(f"{SY.API}/draft/{cfg['draft_id']}") or {}
    status = draft.get("status")
    picks = SY.picks_made(cfg["draft_id"]) or []
    L.append(f"League **{cfg.get('team_name')}** · draft `{status}` · "
             f"{len(picks)} picks made · NFL {st.get('season')} week {st.get('week')}\n")

    # 2. is the board still scored with this league's rules?
    align = SC.verify_alignment(cfg)
    if align.get("by_pos"):
        if align["ok"]:
            L.append("_Scoring alignment: OK - league scores skill positions as "
                     "standard PPR, projections valid as-is._\n")
        else:
            L.append("## !! SCORING CHANGED\n")
            L.append("The league's scoring no longer matches the projections the "
                     "board is built from. **Every projection is suspect until this "
                     "is reconciled.**\n")
            for pos, d in align["drift"].items():
                L.append(f"- **{pos}**: mean shift {d['mean']:+.1f} pts/season, "
                         f"{d['n_off']} of {d['n']} players off by 5+")
            L.append("")

    # 3. what moved
    deltas = board_deltas(con, board, prev)
    if deltas:
        L.append("## Board movement since last check\n")
        for p, dp, da in deltas:
            bits = []
            if abs(dp) >= 5: bits.append(f"proj {dp:+.1f}")
            if abs(da) >= 3: bits.append(f"ADP {da:+.1f}")
            L.append(f"- **{p['name']}** ({p['pos']}) {', '.join(bits)}")
        L.append("")
    else:
        L.append("_No material board movement since the last check._\n")

    inj = injury_flags(board)
    if inj:
        L.append("## Injury designations in the top 120\n")
        for p in inj:
            L.append(f"- **{p['name']}** ({p['pos']} {p.get('team')}) - {p['injury']}")
        L.append("")

    # 4. mode-specific work
    if status != "complete":
        L.append("## Pre-draft readiness\n")
        ok = []
        ok.append(("projections fetched", os.path.exists(
            os.path.join(HERE, "data", "projections_2026.json"))))
        ok.append(("safety queue built", os.path.exists(
            os.path.join(HERE, "logs", "safety_queue.txt"))))
        ok.append(("byes derived", os.path.exists(
            os.path.join(HERE, "data", "schedule_2026.json"))))
        for label, good in ok:
            L.append(f"- {'x' if good else ' '} {label}".replace("- x", "- [x]")
                     .replace("-  ", "- [ ] "))
        L.append("\n_Reminder: the Sleeper queue and autopick toggle are set by "
                 "hand in the draft room; this tool does not touch them._\n")
    else:
        us, trades = scan_trades(con, cfg, board)
        if us is None:
            L.append("_Draft complete but no ownership synced yet._\n")
        else:
            wk = int(st.get("week") or 1)
            con2 = DB.connect()
            row = con2.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
            starters = {r["pid"] for r in con2.execute(
                "SELECT pid FROM ownership WHERE snapshot_id=? AND owner_id=? "
                "AND is_starter=1", (row["s"], cfg.get("user_id")))} if row and row["s"] else set()

            res = LU.analyse(us, cfg, wk, current_starters=starters)
            L.append(f"## Week {wk} lineup ({len(us)} rostered)\n")
            L.append("**Start:** " + ", ".join(
                f"{p['name']} ({p['pos']} {p['proj']:.1f})" for p in
                sorted(res["lineup"], key=lambda x: -x["proj"])))
            L.append("\n**Bench:** " + ", ".join(
                f"{p['name']} ({p['pos']} {p['proj']:.1f})" for p in
                sorted(res["bench"], key=lambda x: -x["proj"])) + "\n")
            if res["unavailable"]:
                L.append("**Cannot play:** " + ", ".join(
                    f"{p['name']} - {p['reason']}" for p in res["unavailable"]) + "\n")
            if res["problems"]:
                L.append("### Needs action\n")
                for x in res["problems"]:
                    L.append(f"- **{x}**")
                L.append("")
            if res["swaps"]:
                today = LU.today_iso()
                L.append("### Rebalance\n")
                for sw in res["swaps"]:
                    lock = ""
                    for q in (sw["start"], sw["sit"]):
                        if q.get("game_date") and q["game_date"] <= today:
                            lock = " _(may already be locked - game date has arrived)_"
                    L.append(f"- start **{sw['start']['name']}**, sit "
                             f"**{sw['sit']['name']}** ({sw['gain']:+.1f})" + lock)
                L.append("")
            elif starters:
                L.append("_Lineup is already optimal._\n")

            fc = LU.forecast(us, cfg, 4, start_week=wk)
            trouble = [r for r in fc if r["unfilled"]]
            if trouble:
                L.append("### Bye / availability trouble ahead\n")
                for r in trouble:
                    L.append(f"- **week {r['week']}**: short {r['unfilled']}; "
                             f"out/bye: {', '.join(r['unavailable'])}")
                L.append("")
            L.append("## Trade opportunities\n")
            if not trades:
                L.append("_None that both sides should accept._\n")
            for t in trades[:8]:
                L.append(f"- with **{t['with']}**: give {t['give']} → get {t['get']}  \n"
                         f"  us **{t['our_gain']:+.2f}/wk**, them {t['their_gain']:+.2f}/wk "
                         f"(their share {t['their_share']:.0%})")
            L.append("")
            if a.act and trades:
                L.append("> `--act` requested. Trade submission runs through the "
                         "browser (Sleeper has no write API) and is wired up "
                         "post-draft once real rosters exist.\n")

    # `res` and `wk` are only bound when a roster actually synced. Right after
    # the draft, before the first track.py --sync, the draft is complete and
    # ownership is still empty - which used to reach this block and raise
    # NameError into the try, silently writing "name 'res' is not defined" into
    # the report on the one night it matters most.
    if a.discord and ND and res is not None:
        try:
            ND.post(embeds=ND.lineup_embeds(res, wk, cfg))
        except Exception as e:
            L.append(f"\n_(discord post failed: {e})_")

    text = "\n".join(L)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    open(REPORT, "w").write(text)
    if not a.quiet:
        print("\n" + text)
    print(f"\n[report written to {REPORT}]")


if __name__ == "__main__":
    main()
