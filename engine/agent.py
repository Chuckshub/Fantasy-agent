#!/usr/bin/env python3
"""The autonomous operator. Runs unattended; speaks only when it matters.

    python3 engine/agent.py --cycle     every 4 hours: refresh, assess, report
    python3 engine/agent.py --pregame   before kickoff: lineup must be legal
    python3 engine/agent.py --status    what it knows, no side effects

Two jobs, deliberately different in temperament.

**--cycle** is the slow loop. It refreshes the current season's data, re-syncs
league state, re-checks that the board is still scored with this league's rules,
looks for trades, and looks ahead for bye trouble. It posts to Discord only when
something has actually changed, because an agent that reports every four hours
whether or not anything happened trains you to ignore it.

**--pregame** is the fast loop, and it exists for one measured reason: across 90
simulated leagues on real 2025 outcomes, benching players who have no game was
worth +0.128 win rate and +2.18 wins a season - larger than every projection
refinement in this project combined. It runs before each kickoff window, checks
only what can still be changed, and escalates loudly if a starter cannot play.
"""
import sys, os, json, argparse, datetime, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import refresh as RF
import track as TR
import scoring as SC
import lineup as LU
import daily as DA
import waivers as WV
import sync as SY
import setlineup as SL
import brief as BR
from value import build_board, load_config

try:
    import notify_discord as ND
except Exception:
    ND = None

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.expanduser("~/.config/statking/agent_state.json")
LOG = os.path.join(HERE, "logs", "agent.log")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    print(msg)


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"), indent=1)


def post(embeds):
    if not ND:
        return
    try:
        ND.post(embeds=embeds)
    except Exception as e:
        log(f"  discord post failed: {e}")


# ------------------------------------------------------------------ helpers
def context():
    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = st.get("season") or "2026"
    week = int(st.get("week") or 1)
    draft = SY.get(f"{SY.API}/draft/{cfg['draft_id']}") or {}
    return cfg, season, week, draft.get("status")


def our_roster(cfg, board):
    con = DB.connect()
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        return [], set()
    by = {p["pid"]: p for p in board}
    pids, starters = [], set()
    for r in con.execute("SELECT pid, is_starter FROM ownership "
                         "WHERE snapshot_id=? AND owner_id=?",
                         (row["s"], cfg.get("user_id"))):
        pids.append(r["pid"])
        if r["is_starter"]:
            starters.add(r["pid"])
    return [by[p] for p in pids if p in by], starters


def lineup_problems(res):
    """Only the things a human must act on before kickoff."""
    out = list(res.get("problems") or [])
    for s in res.get("swaps") or []:
        if s["start"] is None:
            continue
        if s["gain"] >= 2.0:
            out.append(f"start {s['start']['name']} over {s['sit']['name']} "
                       f"(+{s['gain']:.1f})")
    return out


# ------------------------------------------------------------------- cycles
def cmd_cycle(force_post=False):
    state = load_state()
    cfg, season, week, status = context()
    log(f"cycle: season {season} week {week} draft={status}")

    RF.refresh(season, week, verbose=False)
    TR.cmd_sync()

    board, _, _ = build_board(cfg)
    con = DB.connect()
    fields, urgent = [], False

    align = SC.verify_alignment(cfg)
    if align.get("by_pos") and not align["ok"]:
        urgent = True
        fields.append(("SCORING CHANGED", "The league's scoring no longer matches "
                       "the projections the board is built from. Every projection "
                       "is suspect until reconciled.", 0))

    roster, starters = our_roster(cfg, board)
    signature = {"week": week, "status": status, "roster": len(roster)}

    if status != "complete":
        picks = len(SY.picks_made(cfg["draft_id"]) or [])
        signature["picks"] = picks
        fields.append(("Draft", f"`{status}` - {picks} picks made", 1))
    elif roster:
        res = LU.analyse(roster, cfg, week, current_starters=starters or None)
        probs = lineup_problems(res)
        signature["problems"] = probs
        total = sum(p["proj"] for p in res["lineup"])
        fields.append(("Week %d lineup" % week, f"projected **{total:.1f}**", 1))
        if probs:
            urgent = True
            # Do not merely report it. The four-hourly cycle is the only thing
            # running on most days of the week, so if it leaves the fix to a
            # human the roster stays wrong until someone reads Discord.
            sl = enforce_lineup(week, season)
            fields.extend(_applied_fields(sl))
            signature["applied"] = [s["start"]["name"] for s in
                                    ((sl or {}).get("applied") or [])]
            remaining = [x for x in probs
                         if not any(s["start"]["name"] in x
                                    for s in ((sl or {}).get("applied") or []))]
            if remaining:
                fields.append(("Still needs a human",
                               "\n".join(f"- {x}" for x in remaining), 0))
        fc = [r for r in LU.forecast(roster, cfg, 4, start_week=week) if r["unfilled"]]
        if fc:
            fields.append(("Bye trouble ahead", "\n".join(
                f"week {r['week']}: short {r['unfilled']}" for r in fc), 0))
        try:
            wv = WV.targets(cfg)
            live = [t for t in wv["targets"] if t["gain_ppg"] >= WV.MIN_GAIN_PPG]
            if live:
                urgent = True
                fields.append((
                    f"Waiver targets (FAAB ${wv['budget_left']}/{wv['budget_total']})",
                    "\n".join(
                        f"**{t['player']['name']}** ({t['player']['pos']}) "
                        f"{t['recent_ppg']:.1f} ppg, snaps {t['snap_delta']:+.0f}, "
                        f"vs proj {t['beat_projection']:+.1f} -> bid **${t['bid']}**"
                        for t in live[:5]), 0))
        except Exception as e:
            log(f"  waiver scan failed: {e}")

        us, trades = DA.scan_trades(con, cfg, board)
        if trades:
            signature["top_trade"] = trades[0]["our_gain"]
            deadline = cfg.get("trade_deadline_week")
            if deadline and week <= deadline:
                fields.append(("Best trade available", 
                    f"with **{trades[0]['with']}**: give {', '.join(trades[0]['give'])} "
                    f"-> get {', '.join(trades[0]['get'])}\n"
                    f"us `{trades[0]['our_gain']:+.2f}/wk`, them "
                    f"`{trades[0]['their_gain']:+.2f}/wk`", 0))

    changed = signature != state.get("last_signature")
    state["last_signature"] = signature
    state["last_cycle"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_state(state)

    if fields and (changed or urgent or force_post):
        colour = ND.RED if urgent else ND.BLUE if ND else None
        post([ND.embed(f"Cycle - {season} week {week}",
                       "" if changed else "_no change since last cycle_",
                       colour, fields, "auto-cycle every 4h")] if ND else [])
        log(f"  posted ({'urgent' if urgent else 'changed'})")
    else:
        log("  nothing new; stayed quiet")
    return signature


def enforce_lineup(week, season):
    """Actually set the lineup on Sleeper. Returns the setlineup result or None.

    Detecting the right lineup and applying it used to be separate jobs, the
    second one belonging to a human with a phone. That is where the edge leaked:
    the +2.18 wins a season measured in MODEL.md is contingent on the change
    being made before kickoff, and a Discord message at 09:30 on a Sunday is not
    a mechanism. The agent makes the change itself and verifies it against
    Sleeper's own API, which is the only way to know it landed.
    """
    try:
        return SL.run(week=week, season=season, mode="apply", verbose=False)
    except SL.LineupError as e:
        log(f"  SETLINEUP REFUSED: {e}")
        return {"error": str(e)}
    except Exception as e:
        log(f"  SETLINEUP FAILED: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


def _applied_fields(sl):
    """Discord fields describing what the agent actually changed."""
    fields = []
    if not sl:
        return fields
    if sl.get("error"):
        fields.append(("COULD NOT SET THE LINEUP",
                       f"{sl['error']}\nThe lineup below is a recommendation "
                       f"only - nobody has applied it.", 0))
        return fields
    if sl.get("applied"):
        fields.append(("Lineup changed automatically", "\n".join(
            f"started **{s['start']['name']}**, benched **{s['sit']['name']}** "
            f"({s['gain']:+.1f})" for s in sl["applied"]), 0))
    if sl.get("failed"):
        fields.append(("SWAPS THAT FAILED - do these by hand", "\n".join(
            f"start **{s['start']['name']}**, sit **{s['sit']['name']}**"
            for s in sl["failed"]), 0))
    if sl.get("verified") is False:
        fields.append(("VERIFICATION FAILED",
                       sl.get("verify_why") or "Sleeper does not show the "
                       "expected starters - check the roster by hand.", 0))
    # Either side of a skipped pair can be absent: a starter nobody can legally
    # replace carries no `start`, a bench player with nobody to displace carries
    # no `sit`. Both are worth reporting, so neither may be assumed present.
    skipped = sl.get("skipped") or []
    if skipped:
        def _pair(s):
            up = s["start"]["name"] if s.get("start") else None
            down = s["sit"]["name"] if s.get("sit") else None
            if up and down:
                return f"**{up}** over **{down}** - {s['why']}"
            if down:
                return f"**{down}** stays in - {s['why']}"
            return f"**{up}** stays benched - {s['why']}"
        fields.append(("Left alone", "\n".join(
            _pair(s) for s in skipped[:5]), 0))
    return fields


def cmd_pregame():
    """Fast, narrow, and it acts. Only what can still be changed before kickoff."""
    cfg, season, week, status = context()
    log(f"pregame: season {season} week {week}")
    if status != "complete":
        log("  draft not complete; nothing to set")
        return
    # projections move on inactive reports right up to kickoff
    RF.refresh(season, week, lookback=0, verbose=False)
    TR.cmd_sync()
    board, _, _ = build_board(cfg)
    roster, starters = our_roster(cfg, board)
    if not roster:
        log("  no roster synced")
        return
    res = LU.analyse(roster, cfg, week, current_starters=starters or None)
    probs = lineup_problems(res)
    blockers = [p for p in res["eff"] if p["mult"] == 0 and p["pid"] in starters]

    sl = enforce_lineup(week, season) if (probs or blockers) else None
    if not probs and not blockers:
        log("  lineup is legal and optimal; stayed quiet")
        return

    fields = _applied_fields(sl)
    if blockers:
        fields.append(("Starters who CANNOT play", "\n".join(
            f"**{p['name']}** ({p['pos']}) - {p['reason']}" for p in blockers), 0))
    unfilled = res.get("unfilled") or {}
    if unfilled:
        fields.append(("EMPTY STARTER SLOTS - waiver problem",
                       ", ".join(f"{k} x{v}" for k, v in unfilled.items()), 0))
    body = "\n".join(f"- {x}" for x in probs) or "acting on availability"
    bad = bool(unfilled) or (sl or {}).get("error") or (sl or {}).get("failed") \
          or (sl or {}).get("verified") is False
    post([ND.embed(f"PRE-KICKOFF - week {week}", body,
                   ND.RED if bad else ND.GREEN, fields,
                   "benching players with no game is worth +2.18 wins a season")]
         if ND else [])
    log(f"  pregame: {len((sl or {}).get('applied') or [])} swap(s) applied, "
        f"{len(probs)} problem(s)")


def cmd_rebalance():
    """After a game finishes: re-check availability and fix the lineup.

    Games change two things that matter before the next kickoff window: a player
    can leave injured, and the projections for everyone still to play get
    revised. Waiting for the next four-hourly cycle to notice means a starter
    ruled out on Sunday afternoon can still be in the lineup for the late window.
    This runs after each game slate, does the same work as pregame, and stays
    silent unless it changed something.
    """
    cfg, season, week, status = context()
    log(f"rebalance: season {season} week {week}")
    if status != "complete":
        return
    RF.refresh(season, week, lookback=0, verbose=False)
    TR.cmd_sync()
    board, _, _ = build_board(cfg)
    roster, starters = our_roster(cfg, board)
    if not roster:
        log("  no roster synced")
        return
    res = LU.analyse(roster, cfg, week, current_starters=starters or None)
    blockers = [p for p in res["eff"] if p["mult"] == 0 and p["pid"] in starters]
    probs = lineup_problems(res)
    if not probs and not blockers:
        log("  nothing to rebalance; stayed quiet")
        return
    sl = enforce_lineup(week, season)
    applied = (sl or {}).get("applied") or []
    if not applied and not blockers and not (sl or {}).get("error"):
        log("  nothing worth changing; stayed quiet")
        return
    fields = _applied_fields(sl)
    if blockers:
        fields.append(("Cannot play", "\n".join(
            f"**{p['name']}** ({p['pos']}) - {p['reason']}" for p in blockers), 0))
    post([ND.embed(f"REBALANCE - week {week}",
                   "checked after a game finished",
                   ND.RED if (sl or {}).get("error") else ND.BLUE, fields,
                   "post-game availability and projection check")] if ND else [])
    log(f"  rebalance: {len(applied)} swap(s) applied")


def cmd_depth(submit=True):
    """Look ahead for weeks we cannot field a lineup, and close the near ones.

    This is the one failure the lineup optimiser cannot fix by itself: an empty
    starter slot scores zero and no rearrangement of the roster we own creates a
    quarterback. Holes are knowable weeks ahead from the bye schedule, so the
    agent watches them and signs a body once the week is close enough to be
    worth a roster spot.
    """
    import depth as DP
    cfg, season, week, status = context()
    log(f"depth: season {season} week {week}")
    if status != "complete":
        return
    TR.cmd_sync()
    plan = DP.build_plan(cfg, week, season)
    if not plan["holes"]:
        log("  no unfillable weeks ahead; stayed quiet")
        return
    result = DP.acquire(cfg, plan, season, week,
                        mode="submit" if submit else "dry-run", verbose=False)
    fields = []
    holes = "\n".join(
        f"week {h['week']}: short {', '.join(f'{k} x{v}' for k, v in h['short'].items())}"
        + (f" ({', '.join(h['unavailable'])} out)" if h["unavailable"] else "")
        for h in plan["holes"])
    fields.append(("Weeks we cannot field a legal lineup", holes, 0))
    if result["acted"]:
        fields.append(("Signed", "\n".join(
            f"**{r['add']}** ({r['pos']}) for week {r['for_week']}, dropped "
            f"**{r['drop']}** (net {r.get('drop_gain', 0):+.1f} pts)"
            for r in result["acted"]), 0))
    if result["skipped"]:
        fields.append(("Not done", "\n".join(
            f"{r.get('add') or '-'}: {r.get('why') or r.get('stage')}"
            for r in result["skipped"][:4]), 0))
    upcoming = [i for i in plan["plan"] if not i["act_now"] and i["candidates"]]
    if upcoming:
        fields.append(("Watching", "\n".join(
            f"week {i['week']} {i['pos']} - best available "
            f"**{i['candidates'][0]['name']}** ({i['weeks_away']} weeks out)"
            for i in upcoming[:5]), 0))
    post([ND.embed(f"DEPTH - week {week}",
                   f"{len(plan['holes'])} week(s) ahead cannot be filled",
                   ND.RED if not result["acted"] and any(
                       i["act_now"] for i in plan["plan"]) else ND.BLUE,
                   fields, "an empty starter slot scores zero")] if ND else [])
    log(f"  depth: {len(result['acted'])} signing(s), "
        f"{len(plan['holes'])} hole(s) ahead")


def cmd_forecast():
    """Publish this week's probabilities before anything kicks off.

    Ordering matters more than it looks: this must run before the first game,
    because a forecast published after kickoff is not a forecast. The cron slot
    is early Thursday for that reason, not for convenience.
    """
    import forecast as FC
    cfg, season, week, status = context()
    log(f"forecast: season {season} week {week}")
    if status != "complete":
        return
    RF.refresh(season, week, lookback=0, verbose=False)
    TR.cmd_sync()
    res = FC.publish(week, int(season), verbose=False)
    import power as PW
    pr = PW.rank(week, int(season))
    top = pr["teams"][:3]
    us = next((i for i, t in enumerate(pr["teams"], 1) if t["us"]), None)
    ours = next((t for t in pr["teams"] if t["us"]), None)
    fields = [("Top 3 by median week", "\n".join(
        f"`{i}` **{t['name']}** {t['median']:.1f}  "
        f"(floor {t['p10']:.0f}, ceiling {t['p90']:.0f}, "
        f"{t['p_top']:.0%} to lead the league)"
        for i, t in enumerate(top, 1)), 0)]
    if ours:
        fields.append(("Us", f"ranked **{us} of {len(pr['teams'])}** - median "
                       f"**{ours['median']:.1f}**, floor {ours['p10']:.0f}, "
                       f"ceiling {ours['p90']:.0f}"
                       + (f"\n**{ours['p_win']:.0%}** to beat "
                          f"{ours['opponent']}" if ours.get("p_win") is not None
                          else ""), 0))
    post([ND.embed(f"WEEK {week} FORECAST",
                   f"{res['n']} probabilities published and on the record",
                   ND.BLUE, fields,
                   "graded after the week with Brier and a reliability plot")]
         if ND else [])
    log(f"  forecast: {res['n']} propositions, we rank {us}")


def cmd_grade():
    """Settle last week's forecasts, score them, plot, and write the recap."""
    import grade as GR
    import recap as RC
    cfg, season, week, status = context()
    season = int(season)
    wk = max(1, week - 1)          # grade the week that has finished
    log(f"grade: season {season} week {wk}")
    n = GR.resolve(season, wk, verbose=False)
    rep = GR.report(season, None, verbose=False)   # season to date
    if not rep:
        log("  nothing resolved yet")
        return
    d = rep["overall"]
    fields = [("Score to date",
               f"Brier **{d['brier']:.4f}** vs climatology {d['climatology']:.4f}\n"
               f"skill **{d['skill']:+.4f}** over {d['n']:,} settled propositions", 0),
              ("Decomposition",
               f"reliability {d['reliability']:.4f} (lower better)\n"
               f"resolution {d['resolution']:.4f} (higher better)", 0)]
    if rep["by_kind"]:
        fields.append(("By question type", "\n".join(
            f"`{k}` n={v['n']:,} brier {v['brier']:.4f} skill {v['skill']:+.3f}"
            for k, v in rep["by_kind"].items()), 0))
    png = os.path.join(HERE, "logs", f"reliability_w{wk}.png")
    try:
        GR.plot(png, season, None,
                title=f"Calibration through week {wk} - {d['n']:,} forecasts")
    except Exception as e:
        log(f"  plot failed: {e}")
        png = None
    emb = [ND.embed(f"FORECAST SCORECARD - through week {wk}",
                    f"{n} new propositions settled", ND.BLUE, fields,
                    "a forecast nobody grades is decoration")] if ND else []
    if ND:
        try:
            if png and os.path.exists(png):
                ND.post_file(png, embeds=emb)
            else:
                post(emb)
        except Exception as e:
            log(f"  discord post failed: {e}")
    # the recap, written locally
    try:
        facts = RC.build_facts(wk, season)
        r = RC.write_recap(facts, verbose=False)
        body = r["text"] if r["ok"] else r["fallback"]
        note = (f"written locally by {RC.MODEL}" if r["ok"] else
                "local model produced unsupported numbers - showing the facts")
        post([ND.embed(f"Week {wk} recap", body[:4000], ND.BLUE, None, note)]
             if ND else [])
        log(f"  recap: {'model' if r['ok'] else 'fallback'}")
    except Exception as e:
        log(f"  recap failed: {e}")
    log(f"  graded: brier {d['brier']:.4f}, skill {d['skill']:+.4f}")


def cmd_brief():
    """Post the weekly briefing: who is in, who is out, why, and the edge."""
    cfg, season, week, status = context()
    log(f"brief: season {season} week {week}")
    if status != "complete":
        log("  draft not complete; nothing to brief")
        return
    b = BR.build(week, season, cfg)
    fields = [("Starting", "\n".join(
        f"`{s['slot']:<4}` **{s['name']}** {s['proj']:.1f} - {s['why']}"
        for s in b["starters"]), 0)]
    if b["bench"]:
        fields.append(("Benched, and why", "\n".join(
            f"**{s['name']}** ({s['pos']}) {s['proj']:.1f} - {s['why']}"
            for s in b["bench"]), 0))
    h = b["hygiene"]
    if h["unplayable_on_roster"]:
        fields.append(("Cannot play this week", "\n".join(
            f"**{p['name']}** ({p['pos']}) - {p['reason']}"
            for p in h["unplayable_on_roster"]), 0))
    o = b.get("opponent")
    if o:
        line = (f"**{o['name']}** best lineup projects {o['their_best']:.1f}\n"
                f"we project {b['projected_total']:.1f} - "
                f"margin **{o['margin_vs_best']:+.1f}**")
        if o.get("their_current") is not None:
            line += (f"\nthey have {o['their_current']:.1f} set "
                     f"({o['their_best'] - o['their_current']:+.1f} on their bench)")
        if o.get("their_unplayable"):
            line += (f"\nthey are starting {len(o['their_unplayable'])} player(s) "
                     f"who cannot play: {', '.join(o['their_unplayable'])}")
        fields.append(("This week's matchup", line, 0))
    if b["problems"]:
        fields.append(("Problems", "\n".join(f"- {x}" for x in b["problems"]), 0))
    if b["context"]:
        fields.append(("Context - tracked, not applied to any projection",
                       "\n".join(f"**{c['name']}**: {'; '.join(c['notes'])}"
                                 for c in b["context"][:6]), 0))
    post([ND.embed(f"WEEK {week} BRIEFING",
                   f"projected **{b['projected_total']:.1f}**",
                   ND.RED if b["problems"] else ND.BLUE, fields,
                   "why each player is where he is")] if ND else [])
    log("  briefing posted")


def cmd_status():
    cfg, season, week, status = context()
    con = DB.connect()
    s = load_state()
    print(f"season {season} week {week}  draft={status}")
    print(f"  last cycle : {s.get('last_cycle', 'never')}")
    for t in ("actual", "wproj", "game"):
        n = con.execute(f"SELECT COUNT(*) c FROM {t} WHERE season=?",
                        (str(season),)).fetchone()["c"]
        print(f"  {t:<10} {n:>7,} rows for {season}")
    print(f"  ownership  {con.execute('SELECT COUNT(*) c FROM ownership').fetchone()['c']:>7,}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cycle", action="store_true")
    g.add_argument("--pregame", action="store_true")
    g.add_argument("--rebalance", action="store_true")
    g.add_argument("--brief", action="store_true")
    g.add_argument("--depth", action="store_true")
    g.add_argument("--forecast", action="store_true")
    g.add_argument("--grade", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--post", action="store_true", help="post even if unchanged")
    a = ap.parse_args()
    try:
        if a.cycle:
            cmd_cycle(force_post=a.post)
        elif a.pregame:
            cmd_pregame()
        elif a.rebalance:
            cmd_rebalance()
        elif a.brief:
            cmd_brief()
        elif a.depth:
            cmd_depth()
        elif a.forecast:
            cmd_forecast()
        elif a.grade:
            cmd_grade()
        else:
            cmd_status()
    except Exception as e:
        log(f"AGENT ERROR: {type(e).__name__}: {e}")
        log(traceback.format_exc()[-900:])
        sys.exit(1)
