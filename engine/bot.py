#!/usr/bin/env python3
"""StatKing Discord command bot - polls #NFL-Fantasy-2026 and answers commands.

    python3 engine/bot.py            # run the loop (ctrl-c to stop)
    python3 engine/bot.py --once     # process pending commands and exit

There is no discord.py here, and Discord's Gateway would need the privileged
MESSAGE_CONTENT intent enabled in the developer portal. Polling the REST
endpoint needs neither - message content comes through on `GET /channels/
{id}/messages` for any bot with Read Message History - so that is what this
does. The cost is a few seconds of latency, which no fantasy command cares
about.
"""
import sys, os, json, time, argparse, traceback, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify_discord as ND
import db as DB
from value import build_board, load_config
import draft as D
import sync as SY

POLL_SEC = 5
STATE = os.path.expanduser("~/.config/statking/bot_state.json")

COMMANDS = []
SLOW = set()
ALIASES = {}


def cmd(name, args, desc, slow=False, aliases=()):
    """Register a command. `aliases` are accepted but not listed in !help.

    Aliases exist because two commands shipped under mangled names -
    `help_lenovo` and `f-lenovo` - and the obvious spellings `!help` and
    `!feature` did nothing at all. Renaming them without keeping the old
    spellings working would break anything that already refers to them.
    """
    def deco(fn):
        COMMANDS.append((name, args, desc, fn))
        if slow:
            SLOW.add(name)
        for a in aliases:
            ALIASES[a] = name
            if slow:
                SLOW.add(a)
        return fn
    return deco


# ------------------------------------------------------------------- state
def _load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, "w"))


# ------------------------------------------------------------ shared helpers
def _board():
    cfg = load_config()
    board, _, _ = build_board(cfg)
    return cfg, board


def _live_state():
    """Engine state reflecting picks already made in the real draft."""
    cfg, board = _board()
    st = D.DraftState(cfg, board)
    st.my_slot = cfg.get("draft_slot")
    picks = SY.picks_made(cfg["draft_id"]) or []
    for pk in picks:
        pid = pk.get("player_id")
        if not pid:
            continue
        st.drafted[pid] = pk.get("draft_slot")
        if pk.get("draft_slot") == st.my_slot and pid in st.by_pid:
            st.my_roster.append(st.by_pid[pid])
    return cfg, st, picks


def _our_roster(cfg, board):
    con = DB.connect()
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        return []
    by = {p["pid"]: p for p in board}
    return [by[r["pid"]] for r in con.execute(
        "SELECT pid FROM ownership WHERE snapshot_id=? AND owner_id=?",
        (row["s"], cfg.get("user_id"))) if r["pid"] in by]


# ---------------------------------------------------------------- commands
@cmd("help", "", "this list", aliases=("help_lenovo", "commands", "h"))
def c_help(args):
    cfg = load_config()
    lines = [f"`!{n}{(' ' + a) if a else ''}` - {d}" for n, a, d, _ in COMMANDS]
    return [ND.embed("StatKing commands", "\n".join(lines), ND.BLUE,
                     footer=f"league: {cfg.get('team_name','?')} - "
                            f"{cfg.get('teams','?')}-team {cfg.get('scoring','ppr')}")]


@cmd("status", "", "draft status, our roster count, engine health")
def c_status(args):
    cfg, st, picks = _live_state()
    d = SY.get(f"{SY.API}/draft/{cfg['draft_id']}") or {}
    cur, nxt = st.next_two_picks(len(picks))
    fields = [
        ("Draft", f"`{d.get('status')}` - {len(picks)} picks made", 1),
        ("Our next pick", f"#{cur}" + (f" (then #{nxt})" if nxt else ""), 1),
        ("Roster", f"{len(st.my_roster)} players", 1),
    ]
    return [ND.embed("Status", f"**{cfg.get('team_name')}** - slot {st.my_slot} of "
                     f"{st.teams}", ND.BLUE, fields)]


@cmd("pick", "", "what the engine takes right now, with reasoning")
def c_pick(args):
    cfg, st, picks = _live_state()
    cands, meta = D.recommend(st, len(picks), top_n=5)
    if not cands:
        return [ND.embed("No legal candidates", "roster caps may be full", ND.RED)]
    best = cands[0]["player"]
    alts = "\n".join(
        f"**{c['player']['name']}** ({c['player']['pos']}) `{c['score']:.0f}` - "
        f"{D.explain(c, meta)}" for c in cands[1:])
    return [ND.embed(f"Pick #{meta['pick']}: take {best['name']}",
                     f"**{best['pos']} - {best.get('team')}**\n"
                     f"{D.explain(cands[0], meta)}", ND.GREEN,
                     [("Alternatives", alts or "-", 0)])]


@cmd("board", "[n]", "best available right now")
def c_board(args):
    n = int(args[0]) if args and args[0].isdigit() else 12
    cfg, st, picks = _live_state()
    rows = st.available()[:min(n, 20)]
    body = "\n".join(
        f"`{p['overall_rank']:>3}` **{p['name']}** {p['pos']}-{p.get('team') or 'FA'} "
        f"- VORP {p['vorp']:.0f}, ADP {p['adp']:.0f}" if p.get("adp") else
        f"`{p['overall_rank']:>3}` **{p['name']}** {p['pos']} - VORP {p['vorp']:.0f}"
        for p in rows)
    return [ND.embed("Best available", body, ND.BLUE)]


@cmd("player", "<name>", "full history: ppg, floor/ceiling, boom/bust, matchups")
def c_player(args):
    import history as HI
    if not args:
        return [ND.embed("Usage", "`!player Nico Collins`", ND.AMBER)]
    name = " ".join(args)
    cfg, board = _board()
    p = next((x for x in board if x["name"].lower() == name.lower()), None)
    if not p:
        return [ND.embed("Not found", f"no player named **{name}** on the board", ND.AMBER)]
    prof = HI.player_profile(p["pid"])
    if not prof:
        # Rookies and returnees have no regular-season record at all. Preseason
        # is the only evidence that exists for them, so show it rather than
        # shrugging - while being explicit about what it is worth.
        pre = DB.connect().execute(
            "SELECT SUM(pts) pts, SUM(snaps) snaps, COUNT(*) g FROM preseason "
            "WHERE pid=? AND season=(SELECT MAX(season) FROM preseason)",
            (p["pid"],)).fetchone()
        if pre and pre["g"]:
            return [ND.embed(
                f"{p['name']} ({p['pos']} {p.get('team')})",
                f"No regular-season games on record. 2026 projection "
                f"**{p['proj']:.0f}** pts, VORP {p['vorp']:.0f}"
                + (f", ADP {p['adp']:.1f}" if p.get("adp") else ""), ND.AMBER,
                [("Preseason only", f"{pre['g']} games, **{pre['pts'] or 0:.1f}** PPR "
                  f"on {pre['snaps'] or 0:.0f} snaps", 0),
                 ("How much to trust it",
                  "Preseason production did **not** replicate as a predictor "
                  "across 2024 and 2025 - the correlation flipped sign. Treat it "
                  "as evidence he is on the field, not that he will score.", 0)])]
        return [ND.embed(p["name"], "no game logs stored yet", ND.AMBER)]
    solid = {o: h for o, h in prof["h2h"].items() if h["n"] >= HI.MIN_H2H}
    h2h = "\n".join(f"vs **{o}** {h['ppg']:.1f}/gm over {h['n']} games"
                    for o, h in sorted(solid.items(), key=lambda kv: -kv[1]["ppg"])) \
        or "_every head-to-head sample is 1-2 games - not evidence_"
    fields = [
        ("Baseline", f"{prof['games']} gms - **{prof['ppg']:.1f}** PPR/gm "
                     f"(sd {prof['sd']:.1f})", 1),
        ("Range", f"floor {prof['floor']:.1f} - ceiling {prof['ceiling']:.1f}", 1),
        ("Form", f"last 5: {prof['last5_ppg']:.1f}/gm", 1),
        ("Boom / bust", f"{prof['boom_rate']:.0%} / {prof['bust_rate']:.0%}", 1),
        (f"Opponents faced {HI.MIN_H2H}+ times", h2h, 0),
    ]
    return [ND.embed(f"{p['name']} ({p['pos']} {p.get('team')})",
                     f"2026 projection **{p['proj']:.0f}** pts - VORP {p['vorp']:.0f}"
                     + (f" - ADP {p['adp']:.1f}" if p.get("adp") else ""),
                     ND.BLUE, fields)]


@cmd("defense", "<POS>", "most generous and toughest defenses vs a position")
def c_defense(args):
    import history as HI
    pos = (args[0].upper() if args else "WR")
    dvp = HI.defense_vs_position("2025")
    teams = dvp.get(pos)
    if not teams:
        return [ND.embed("No data", f"nothing stored for {pos}", ND.AMBER)]
    rank = sorted(teams.items(), key=lambda kv: -kv[1]["ppg"])
    soft = "\n".join(f"`{d:<4}` {v['ppg']:>5.1f}/gm  z {v['z']:+.2f}" for d, v in rank[:6])
    hard = "\n".join(f"`{d:<4}` {v['ppg']:>5.1f}/gm  z {v['z']:+.2f}" for d, v in rank[-6:])
    return [ND.embed(f"Defenses vs {pos} (2025, {rank[0][1]['mu']:.1f} PPR/gm avg)",
                     "17 games behind every number - this is the reliable matchup signal.",
                     ND.BLUE, [("Most generous - target", soft, 1),
                               ("Toughest - fade", hard, 1)])]


@cmd("lineup", "[week]", "optimal lineup, who sits and why")
def c_lineup(args):
    import lineup as LU
    wk = int(args[0]) if args and args[0].isdigit() else 1
    cfg, board = _board()
    roster = _our_roster(cfg, board)
    if not roster:
        return [ND.embed("No roster yet",
                         "The draft has not happened, so there is nothing to set. "
                         "Try `!pick` or `!board`.", ND.AMBER)]
    res = LU.analyse(roster, cfg, wk)
    return ND.lineup_embeds(res, wk, cfg)


@cmd("edge", "[week]", "where the data says we have an edge this week")
def c_edge(args):
    import lineup as LU, history as HI
    wk = int(args[0]) if args and args[0].isdigit() else 1
    cfg, board = _board()
    roster = _our_roster(cfg, board)
    if not roster:
        return [ND.embed("No roster yet", "Draft first.", ND.AMBER)]
    eff = LU.effective(roster, wk, cfg)
    ranked = sorted([p for p in eff if p.get("opponent") and p["mult"] > 0],
                    key=lambda x: -x.get("matchup_mult", 1.0))
    good = [p for p in ranked if p.get("matchup_mult", 1) > 1.04][:5]
    bad = [p for p in ranked if p.get("matchup_mult", 1) < 0.96][-5:]
    def fmt(ps):
        return "\n".join(f"**{p['name']}** ({p['pos']}) vs {p['opponent']} "
                         f"`x{p['matchup_mult']:.2f}`\n_{p['matchup_why']}_" for p in ps) or "-"
    return [ND.embed(f"Week {wk} edge", 
                     "Driven by defense-vs-position over a full 17-game sample. "
                     "Head-to-head splits under 3 games are ignored on purpose.",
                     ND.GREEN, [("Exploit", fmt(good), 0), ("Fade", fmt(bad), 0)])]


@cmd("durability", "<name>", "how often he actually plays, by season")
def c_durability(args):
    import model as MO
    if not args:
        return [ND.embed("Usage", "`!durability Christian McCaffrey`", ND.AMBER)]
    cfg, board = _board()
    p = next((x for x in board if x["name"].lower() == " ".join(args).lower()), None)
    if not p:
        return [ND.embed("Not found", " ".join(args), ND.AMBER)]
    d = MO.durability(p["pid"])
    if not d:
        return [ND.embed(p["name"], "no game logs", ND.AMBER)]
    body = "\n".join(
        f"`{s['season']}` played **{s['played']}** of {s['of']} ({s['share']:.0%})"
        for s in sorted(d["seasons"], key=lambda x: -x["season"]))
    av, why = MO.availability(p["pid"], p.get("injury"))
    colour = ND.GREEN if av >= 0.85 else (ND.AMBER if av >= 0.7 else ND.RED)
    return [ND.embed(f"{p['name']} - {d['rate']:.0%} durable",
                     body, colour,
                     [("Availability now", f"**{av:.0%}** - {why}", 0)],
                     "recency-weighted; context for roster risk, not a "
                     "coefficient on weekly points")]


@cmd("feature", "<what you want built>",
     "queue a feature and build it autonomously",
     aliases=("f-lenovo", "build"))
def c_feature(args):
    import feature as FT
    if not args:
        rows = FT.listing(8)
        body = "\n".join(
            f"`#{r['id']}` **{r['status']}** {(r['branch'] or '')} - {r['request'][:70]}"
            for r in rows) or "_nothing requested yet_"
        return [ND.embed("Feature requests", body, ND.BLUE,
                         [("Usage", "`!feature make !board show bye weeks`", 0)])]

    request = " ".join(args)
    fid = FT.add(request)
    # Build out of process: a build takes minutes and the bot polls every five
    # seconds, so doing it inline would freeze every other command.
    subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "feature.py"), "--work", "--id", str(fid)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    return [ND.embed(
        f"Feature #{fid} queued", f"> {request}", ND.AMBER,
        [("What happens now",
          "A headless Claude run builds it in an isolated git worktree, then it "
          "must compile, import, and prove it left the live-draft files alone "
          "before the work is accepted. I will post the result here.", 0),
         ("While a draft is live",
          "The branch is committed but **not merged**. Hot-patching the engine "
          "that is currently drafting for us is not worth a convenience "
          "feature - it merges once the draft completes.", 0)],
        "run !feature with no arguments to see the queue")]


@cmd("model", "", "what the weighting model does, and what it rejected")
def c_model(args):
    import model as MO
    return [ND.embed(
        "The weighting model",
        f"`composite = {MO.BLEND_ALPHA:g} x Sleeper weekly projection + "
        f"{1-MO.BLEND_ALPHA:.1f} x season-to-date average`\n\n"
        "Backtested on 85,109 game logs (2019-2025) and 14,314 historical "
        "weekly projections.", ND.BLUE,
        [("Kept", "blend at alpha 0.8 - beat pure projection in **both** graded "
                  "seasons (2024: 4.841 vs 4.849 MAE; 2025: 4.983 vs 5.025)", 0),
         ("Rejected - all of it", "prior-season matchup `-1.07%`\n"
                      "same-season walk-forward matchup `null`\n"
                      "positional bias correction `-1.27%`\n"
                      "durability weighting `did not replicate`\n"
                      "Vegas implied team total `null - already priced in`\n"
                      "variance-aware lineups `null, all |z| < 1`", 0),
         ("The finding that actually matters",
          "90 simulated leagues on real 2025 outcomes:\n"
          "naive (starts players with no game) **0.436** win rate\n"
          "careful (benches them) **0.564** win rate\n"
          "`+10.3 pts/week, z +17.4, +2.18 wins per season`\n\n"
          "Never starting a zero is worth more than every clever adjustment "
          "combined. That is what the daily check is for.", 0),
         ("Why matchup is shown but not applied",
          "A cap sweep found error rising monotonically with the size of the "
          "adjustment (+/-5% -> +0.15%, +/-35% -> -1.75%). The optimum is zero.", 0),
         ("Hard facts, not coefficients",
          "Byes and Out/IR/PUP/Sus are zeroes, and those players leave the "
          "selection pool so an empty slot shows up as a waiver problem.", 0)],
        "full write-up in MODEL.md")]


@cmd("score", "[week]", "live matchup score, our players, and their games",
     aliases=("scores", "scoreboard"))
def c_score(args):
    """Live scoreboard: our matchup, every starter, and where his game is up to.

    Requested through !feature and silently swallowed, because !feature was not
    a command at the time.

    Points come from Sleeper's matchups feed, which carries per-player scoring
    for the week. Game state comes from the schedule feed, so a player who has
    not kicked off is distinguishable from one who played and scored nothing -
    a zero means very different things in those two cases, and a scoreboard
    that conflates them is worse than no scoreboard.
    """
    import lineup as LU
    import setlineup as SL
    cfg, board = _board()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = str(st.get("season") or "2026")
    week = int(args[0]) if args and args[0].isdigit() else int(st.get("week") or 1)

    rosters = SY.get(f"{SY.API}/league/{cfg['league_id']}/rosters") or []
    ours = next((r for r in rosters
                 if str(r.get("owner_id")) == str(cfg.get("user_id"))), None)
    if not ours:
        return [ND.embed("No roster", "we do not have a roster in this league yet",
                         ND.AMBER)]
    ms = SY.get(f"{SY.API}/league/{cfg['league_id']}/matchups/{week}") or []
    mine = next((m for m in ms if m.get("roster_id") == ours.get("roster_id")), None)
    if not mine:
        return [ND.embed(f"Week {week}", "no matchup posted for this week yet",
                         ND.AMBER)]
    opp = next((m for m in ms if m.get("matchup_id") == mine.get("matchup_id")
                and m.get("roster_id") != mine.get("roster_id")), None)

    con = DB.connect()
    def team_name(rid):
        row = con.execute("SELECT team_name, username FROM manager "
                          "WHERE roster_id=?", (rid,)).fetchone()
        return (row["team_name"] or row["username"]) if row else f"roster {rid}"

    by_pid = {p["pid"]: p for p in board}
    status = SL.game_status_index(season, week)
    pts = mine.get("players_points") or {}
    starters = [s for s in (mine.get("starters") or []) if s and s != "0"]

    def line(pid):
        p = by_pid.get(pid) or {}
        nm = p.get("name") or pid
        pos = p.get("pos") or "?"
        got = pts.get(pid)
        g = (status.get(p.get("team") or "") or {}).get("status")
        mark = {"pre_game": "not started", "complete": "final"}.get(g, g or "-")
        return f"`{pos:<3}` **{nm}** - {0.0 if got is None else got:.1f}  _{mark}_"

    started = sum(1 for pid in starters
                  if (status.get((by_pid.get(pid) or {}).get("team") or "")
                      or {}).get("status") != "pre_game")
    us = mine.get("points") or 0.0
    them = (opp or {}).get("points") or 0.0
    head = (f"**{cfg.get('team_name')}** {us:.1f} - {them:.1f} "
            f"**{team_name(opp['roster_id']) if opp else 'no opponent'}**")
    if not started:
        head += "\n_no game has kicked off yet - these are all zero, not bad_"

    fields = [("Starters", "\n".join(line(p) for p in starters) or "-", 0)]
    bench = [p for p in (mine.get("players") or []) if p not in starters]
    if bench:
        fields.append(("Bench", "\n".join(line(p) for p in bench), 0))
    if not started:
        res = LU.analyse([by_pid[p] for p in starters if p in by_pid],
                         cfg, week, season=season)
        fields.append(("Projected", f"our starters project "
                       f"**{sum(x['proj'] for x in res['eff']):.1f}**", 0))
    return [ND.embed(f"Week {week} scoreboard", head,
                     ND.GREEN if us >= them else ND.AMBER, fields,
                     f"{started}/{len(starters)} of our starters have kicked off")]


@cmd("power", "[week]", "every team ranked by simulated median week", slow=True,
     aliases=("sim",))
def c_power(args):
    import power as PW
    week = int(args[0]) if args and args[0].isdigit() else None
    res = PW.rank(week)
    body = "\n".join(
        f"`{i:>2}` **{t['name'][:16]}** {t['median']:.1f}  "
        f"({t['p10']:.0f}-{t['p90']:.0f})"
        + (f"  {t['p_win']:.0%} to win" if t["p_win"] is not None else "")
        + ("  **<- us**" if t["us"] else "")
        for i, t in enumerate(res["teams"], 1))
    return [ND.embed(f"Week {res['week']} power ranking", body, ND.BLUE,
                     [("How to read it",
                       "median simulated week, with the 10th-90th percentile "
                       "range beside it. Ranked by median rather than mean "
                       "because fantasy weeks are right-skewed and the mean is "
                       "dragged around by ceilings a team sees three times a "
                       "season.", 0)],
                     f"{res['sims']:,} simulations per team")]


@cmd("forecast", "[week]", "the probabilities we published this week", slow=True,
     aliases=("odds",))
def c_forecast(args):
    import nflverse as NV
    week = int(args[0]) if args and args[0].isdigit() else None
    con = NV.connect()
    q = ("SELECT kind, label, prob, resolved, outcome FROM forecast "
         "WHERE 1=1" + (f" AND week={week}" if week else ""))
    rows = con.execute(q + " ORDER BY prob DESC").fetchall()
    if not rows:
        return [ND.embed("No forecasts", "none published yet - "
                         "`engine/agent.py --forecast`", ND.AMBER)]
    mw = [r for r in rows if r[0] == "matchup_win"]
    po = [r for r in rows if r[0] == "player_over"]
    fields = []
    if mw:
        fields.append(("Matchups", "\n".join(
            f"{r[2]:.0%}  {r[1]}"
            + ("" if not r[3] else ("  HIT" if r[4] else "  miss"))
            for r in mw), 0))
    if po:
        fields.append(("Most confident player calls", "\n".join(
            f"{r[2]:.0%}  {r[1]}" for r in po[:8]), 0))
        fields.append(("Longest shots", "\n".join(
            f"{r[2]:.0%}  {r[1]}" for r in po[-5:]), 0))
    return [ND.embed(f"{len(rows)} forecasts on record",
                     "published before kickoff, graded after", ND.BLUE, fields,
                     "run !brier for how they have scored")]


@cmd("brier", "", "how well calibrated our probabilities have been", slow=True,
     aliases=("calibration", "score"))
def c_brier(args):
    import grade as GR
    rep = GR.report(verbose=False)
    if not rep:
        return [ND.embed("Nothing graded yet",
                         "forecasts are scored once a week completes", ND.AMBER)]
    d = rep["overall"]
    verdict = ("we add real information" if d["skill"] > 0.05 else
               "barely better than the base rate" if d["skill"] > 0 else
               "**no better than guessing the base rate**")
    fields = [("Score",
               f"Brier **{d['brier']:.4f}** (0 perfect, 0.25 coin flip)\n"
               f"climatology {d['climatology']:.4f}\n"
               f"skill **{d['skill']:+.4f}** - {verdict}", 0),
              ("Decomposition",
               f"reliability {d['reliability']:.4f} - are our 70%s right 70%?\n"
               f"resolution {d['resolution']:.4f} - do we separate likely "
               f"from unlikely?", 0)]
    if rep["by_kind"]:
        fields.append(("By type", "\n".join(
            f"`{k}` n={v['n']:,}  brier {v['brier']:.4f}  skill {v['skill']:+.3f}"
            for k, v in rep["by_kind"].items()), 0))
    table = "\n".join(
        f"`{b['lo']:.0%}-{b['hi']:.0%}` n={b['n']:<5} said {b['mean_p']:.0%}, "
        f"happened {b['obs']:.0%}"
        for b in d["bins"] if b)
    fields.append(("Reliability", table or "-", 0))
    return [ND.embed(f"Calibration - {d['n']:,} settled propositions",
                     "a forecast nobody grades is decoration", ND.BLUE, fields)]


@cmd("stack", "[week]", "where we rank this week, and at every position",
     slow=True, aliases=("rank", "power"))
def c_stack(args):
    """How we compare to the other thirteen teams this week.

    Two different questions, and conflating them hides the interesting half.
    *Best possible lineup* measures the roster. *What they have set* measures
    the manager - and the gap between the two is points being left on a bench,
    which is the one edge this project has actually measured. Showing both makes
    visible how many opponents are beating themselves.
    """
    import statistics, collections
    import lineup as LU
    import value_trade as VT
    cfg, board = _board()
    by = {p["pid"]: p for p in board}
    con = DB.connect()
    row = con.execute("SELECT MAX(snapshot_id) s FROM ownership").fetchone()
    if not row or not row["s"]:
        return [ND.embed("No league snapshot", "run `track.py --sync` first", ND.AMBER)]
    snap = row["s"]
    st = SY.get(f"{SY.API}/state/nfl") or {}
    week = int(args[0]) if args and args[0].isdigit() else int(st.get("week") or 1)

    names, myrid = {}, None
    for r in con.execute("SELECT roster_id, team_name, username, owner_id FROM manager"):
        names[r["roster_id"]] = r["team_name"] or r["username"]
        if str(r["owner_id"]) == str(cfg.get("user_id")):
            myrid = r["roster_id"]

    rows, pos_tot = [], collections.defaultdict(dict)
    for r in con.execute("SELECT DISTINCT roster_id FROM ownership WHERE snapshot_id=?",
                         (snap,)):
        rid = r["roster_id"]
        pids, starters = [], set()
        for x in con.execute("SELECT pid,is_starter FROM ownership "
                             "WHERE snapshot_id=? AND roster_id=?", (snap, rid)):
            pids.append(x["pid"])
            if x["is_starter"]:
                starters.add(x["pid"])
        roster = [by[p] for p in pids if p in by]
        if not roster:
            continue
        eff = LU.effective(roster, week, cfg)
        playable = [p for p in eff if p["mult"] > 0]
        best, _, _ = VT.optimal_lineup(playable, cfg["roster_slots"],
                                       set(cfg["flex_eligible"]))
        setl = [p for p in eff if p["pid"] in starters]
        agg = collections.defaultdict(float)
        for p in best:
            agg[p["pos"]] += p["proj"]
        for k, v in agg.items():
            pos_tot[k][rid] = v
        rows.append({"rid": rid, "name": names.get(rid, str(rid)),
                     "best": sum(p["proj"] for p in best),
                     "set": sum(p["proj"] for p in setl) if setl else None})
    if not rows:
        return [ND.embed("No rosters", "nothing synced yet", ND.AMBER)]
    rows.sort(key=lambda r: -r["best"])
    vals = [r["best"] for r in rows]
    mine = next((i for i, r in enumerate(rows, 1) if r["rid"] == myrid), None)

    table = "\n".join(
        f"`{i:>2}` **{r['name'][:16]}** {r['best']:.1f}"
        + (f"  _(-{r['best'] - r['set']:.1f} on bench)_"
           if r["set"] is not None and r["best"] - r["set"] > 0.05 else "")
        + ("  **<- us**" if r["rid"] == myrid else "")
        for i, r in enumerate(rows, 1))

    pos_lines = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
        d = pos_tot.get(pos) or {}
        if myrid not in d:
            continue
        v = sorted(d.values(), reverse=True)
        rank = v.index(d[myrid]) + 1
        pos_lines.append(f"`{pos:<3}` {d[myrid]:.1f}  vs league mean "
                         f"{statistics.fmean(v):.1f}  - **{rank}/{len(v)}**")

    leaking = sum(1 for r in rows
                  if r["set"] is not None and r["best"] - r["set"] > 0.05)
    fields = [("Every team, best possible lineup", table, 0),
              ("Us by position", "\n".join(pos_lines) or "-", 0),
              ("Managers leaving points on their bench",
               f"**{leaking} of {len(rows)}** - we are not one of them", 0)]
    return [ND.embed(
        f"Week {week} - we rank {mine} of {len(rows)}",
        f"our best lineup **{rows[mine-1]['best']:.1f}**, league mean "
        f"{statistics.fmean(vals):.1f}, median {statistics.median(vals):.1f}",
        ND.GREEN if mine and mine <= len(rows) / 2 else ND.AMBER, fields,
        "best possible lineup, not what each manager has actually set")]


@cmd("standings", "", "league points chart + our expected points", slow=True)
def c_standings(args):
    import lineup as LU
    cfg, board = _board()
    con = DB.connect()
    rows = con.execute(
        "SELECT roster_id, SUM(points) tot, COUNT(*) n FROM matchup "
        "GROUP BY roster_id ORDER BY tot DESC").fetchall()
    names = {m["roster_id"]: (m["team_name"] or m["username"] or "?")
             for m in con.execute("SELECT * FROM manager")}
    if not rows:
        body = "_No games played yet - standings appear once week 1 completes._"
        chart = ""
    else:
        pairs = [(names.get(r["roster_id"], f"team {r['roster_id']}"), r["tot"] or 0)
                 for r in rows]
        chart = "```\n" + ND.bar_chart(pairs) + "\n```"
        body = chart
    roster = _our_roster(cfg, board)
    fields = []
    if roster:
        # Seventeen weeks of analyse() means seventeen rounds of HTTP fetches.
        # lineup.py memoises them per process, so this is slow once and instant
        # afterwards - but the first call still blocks the poll loop, which is
        # why !standings sends an acknowledgement before it starts.
        weeks = list(range(1, 18))
        tot = 0.0
        for wk in weeks:
            res = LU.analyse(roster, cfg, wk)
            tot += sum(p["proj"] for p in res["lineup"])
        fields = [("Our expected PPG", f"**{tot / len(weeks):.1f}**", 1),
                  ("Expected season total", f"**{tot:.0f}**", 1)]
    return [ND.embed("League standings", body, ND.BLUE, fields)]


@cmd("waivers", "", "waiver targets and what to bid", slow=True)
def c_waivers(args):
    import waivers as WV
    r = WV.targets(_board()[0])
    if not r["targets"]:
        return [ND.embed("Waivers", "No usage data yet - the season has not "
                         "produced game logs.", ND.BLUE,
                         [("Budget", f"${r['budget_left']} of ${r['budget_total']}", 1)])]
    body = "\n".join(
        f"**{t['player']['name']}** ({t['player']['pos']} {t['player'].get('team')}) "
        f"- {t['recent_ppg']:.1f} ppg, snaps {t['snap_delta']:+.0f}, "
        f"beating projection by {t['beat_projection']:+.1f} -> bid **${t['bid']}**"
        for t in r["targets"][:6])
    return [ND.embed(f"Waiver targets - week {r['week']}", body, ND.GREEN,
                     [("FAAB left", f"${r['budget_left']} of ${r['budget_total']}", 1),
                      ("Weeks remaining", str(r["weeks_left"]), 1),
                      ("Why this matters",
                       "25-34% of weekly top-N finishes in 2025 came from players "
                       "projected under 6 points in week 1. About a third of "
                       "startable production is never drafted.", 0)],
                     "bid sizing is a heuristic, not a backtested model")]


@cmd("trades", "", "trade offers that both sides should accept")
def c_trades(args):
    import daily as DA
    cfg, board = _board()
    con = DB.connect()
    us, trades = DA.scan_trades(con, cfg, board)
    if us is None:
        return [ND.embed("No rosters yet", "Draft first.", ND.AMBER)]
    if not trades:
        return [ND.embed("No trades", "Nothing that both sides should accept.", ND.BLUE)]
    body = "\n\n".join(
        f"**{t['with']}**\ngive {', '.join(t['give'])}\nget {', '.join(t['get'])}\n"
        f"us `{t['our_gain']:+.2f}/wk` - them `{t['their_gain']:+.2f}/wk` "
        f"({t['their_share']:.0%} of surplus)" for t in trades[:5])
    return [ND.embed("Trade opportunities", body, ND.GREEN,
                     footer=f"deadline week {cfg.get('trade_deadline_week')} - "
                            f"{cfg.get('veto_votes_needed')} votes veto a trade")]


# -------------------------------------------------------------------- loop
def resolve(name):
    """Canonical command name for what the user typed, or None."""
    name = (name or "").lower()
    if any(n == name for n, _, _, _ in COMMANDS):
        return name
    return ALIASES.get(name)


def dispatch(text):
    parts = text.strip().lstrip("!").split()
    if not parts:
        return None
    typed, args = parts[0].lower(), parts[1:]
    name = resolve(typed)
    if name:
        for n, _, _, fn in COMMANDS:
            if n == name:
                try:
                    return fn(args)
                except Exception as e:
                    traceback.print_exc()
                    return [ND.embed("Command failed",
                                     f"`{type(e).__name__}: {e}`", ND.RED)]
    # An unrecognised command used to return None, and the caller only posts
    # when there is something to post - so a typo produced total silence. That
    # is how `!help` and `!feature` went unanswered for a day without anyone
    # being able to tell the bot was even alive. Say something.
    import difflib
    known = [n for n, _, _, _ in COMMANDS]
    close = difflib.get_close_matches(typed, known + list(ALIASES), n=3, cutoff=0.5)
    close = [resolve(c) or c for c in close]
    seen, suggest = set(), []
    for c in close:
        if c not in seen:
            seen.add(c)
            suggest.append(c)
    body = f"`!{typed}` is not a command."
    if suggest:
        body += "  Did you mean " + ", ".join(f"`!{c}`" for c in suggest) + "?"
    return [ND.embed("Unknown command", body, ND.AMBER,
                     [("Everything I answer to",
                       ", ".join(f"`!{n}`" for n in known), 0)],
                     "run !help for the full list with descriptions")]


def poll_once(env, me_id, state):
    cid = env["DISCORD_CHANNEL_ID"]
    q = f"/channels/{cid}/messages?limit=20"
    if state.get("last_id"):
        q += f"&after={state['last_id']}"
    msgs = ND._req(q, token=env["DISCORD_BOT_TOKEN"]) or []
    for m in reversed(msgs):
        state["last_id"] = max(state.get("last_id", "0"), m["id"], key=int)
        if (m.get("author") or {}).get("id") == me_id:
            continue
        content = (m.get("content") or "").strip()
        if not content.startswith("!"):
            continue
        print(f"  cmd: {content[:60]}")
        name = content.strip().lstrip("!").split()[0].lower() if content.strip("!") else ""
        if name in SLOW:
            try:
                ND.post(content="working on it - this one takes a moment...", env=env)
            except Exception:
                pass
        out = dispatch(content)
        if out:
            ND.post(embeds=out, env=env)
    _save_state(state)
    return len(msgs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    env = ND.load_env()
    if not env.get("DISCORD_CHANNEL_ID"):
        ND.resolve_channel(env)
    me_id = ND._req("/users/@me", token=env["DISCORD_BOT_TOKEN"])["id"]
    state = _load_state()
    print(f"listening on channel {env['DISCORD_CHANNEL_ID']} "
          f"({len(COMMANDS)} commands). ctrl-c to stop.")
    if a.once:
        poll_once(env, me_id, state); return
    while True:
        try:
            poll_once(env, me_id, state)
        except KeyboardInterrupt:
            print("\nstopped."); return
        except Exception as e:
            print(f"  ! {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
