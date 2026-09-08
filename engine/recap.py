#!/usr/bin/env python3
"""The weekly recap, written by a local model from our own numbers.

    python3 engine/recap.py --week 1            build facts, write, print
    python3 engine/recap.py --week 1 --post     ... and send it to Discord
    python3 engine/recap.py --facts --week 1    just the fact sheet, no model

Runs against Ollama on this machine. Nothing leaves the box and nothing is
billed, which is the whole reason a local model runs on a four-core i5 rather
than an API being called.

The default is a 7B rather than the 3B first tried, for a reason worth keeping:
the 3B passed the number guard and still wrote that we "started Rome Odunze
instead of Kincaid" when the facts said the exact opposite. Numbers can be
checked mechanically; a reversed relationship cannot, and it is the kind of
error a reader believes. The 7B takes about two minutes a week on this CPU,
which is nothing for a job that runs once.

**The model is a writer, not an analyst.** Every number in the recap is computed
here, in Python, from the store; the model's only job is to turn a fact sheet
into prose. That distinction is enforced rather than hoped for: `check_numbers`
extracts every numeric token from the generated text and refuses any that does
not appear in the facts it was given. A small local model asked to write about
football will otherwise cheerfully invent a stat line, and a recap that is 90%
accurate is worse than no recap, because the 10% is indistinguishable.

If the guard rejects a draft, it retries at a lower temperature, and if it still
fails it falls back to printing the fact sheet plainly. A boring true recap
beats a lively false one.
"""
import sys, os, re, json, argparse, urllib.request, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as DB
import nflverse as NV
import grade as GR
import sync as SY
from value import build_board, load_config

try:
    import notify_discord as ND
except Exception:
    ND = None

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("STATKING_LLM", "qwen2.5:7b-instruct")
TIMEOUT = 420


# ------------------------------------------------------------------- facts
def build_facts(week=None, season=None):
    cfg = load_config()
    st = SY.get(f"{SY.API}/state/nfl") or {}
    season = int(season or st.get("season") or 2026)
    week = int(week if week is not None else (st.get("week") or 1))

    board, _, _ = build_board(cfg)
    by = {p["pid"]: p for p in board}
    s = DB.connect()
    names, myrid = {}, None
    for r in s.execute("SELECT roster_id, team_name, username, owner_id FROM manager"):
        names[r["roster_id"]] = r["team_name"] or r["username"]
        if str(r["owner_id"]) == str(cfg.get("user_id")):
            myrid = r["roster_id"]

    tpts, won = GR.league_results(cfg, week)
    con = NV.connect()
    pts = GR.actual_points(con, season, week)

    # our starters and what they did against what we forecast
    row = s.execute("SELECT MAX(snapshot_id) x FROM ownership").fetchone()
    snap = row["x"] if row else None
    starters, bench = [], []
    if snap and myrid:
        for x in s.execute("SELECT pid,is_starter FROM ownership "
                           "WHERE snapshot_id=? AND roster_id=?", (snap, myrid)):
            p = by.get(x["pid"])
            if not p:
                continue
            rec = {"name": p["name"], "pos": p["pos"],
                   "actual": round(pts.get(str(x["pid"]), 0.0), 1)}
            (starters if x["is_starter"] else bench).append(rec)
    starters.sort(key=lambda r: -r["actual"])
    bench.sort(key=lambda r: -r["actual"])

    standings = sorted(((names.get(r, str(r)), round(v, 1))
                        for r, v in tpts.items()), key=lambda t: -t[1])
    my_pts = round(tpts.get(myrid, 0.0), 1) if myrid else None
    opp_name = opp_pts = None
    ms = SY.get(f"{SY.API}/league/{cfg['league_id']}/matchups/{week}") or []
    mine = next((m for m in ms if m.get("roster_id") == myrid), None)
    if mine and mine.get("matchup_id") is not None:
        o = next((m for m in ms if m.get("matchup_id") == mine["matchup_id"]
                  and m["roster_id"] != myrid), None)
        if o:
            opp_name = names.get(o["roster_id"], "?")
            opp_pts = round(o.get("points") or 0.0, 1)

    # how the forecasts did
    fc = None
    try:
        rows = GR.fetch(con, season, week)
        if rows:
            d = GR.decompose([(r[2], r[3]) for r in rows])
            clim = GR.brier([(d["base_rate"], o) for _, o in
                             [(r[2], r[3]) for r in rows]])
            fc = {"n": d["n"], "brier": round(d["brier"], 4),
                  "climatology": round(clim, 4),
                  "skill": round(1 - d["brier"] / clim, 4) if clim else None}
    except Exception:
        pass

    # the biggest bench mistake, if any
    worst = None
    if starters and bench:
        low = min(starters, key=lambda r: r["actual"])
        high = max(bench, key=lambda r: r["actual"])
        if high["actual"] > low["actual"]:
            worst = {"sat": high["name"], "sat_pts": high["actual"],
                     "started": low["name"], "started_pts": low["actual"],
                     "cost": round(high["actual"] - low["actual"], 1)}

    return {"season": season, "week": week, "team": cfg.get("team_name"),
            "league_size": len(names) or len(standings),
            "our_points": my_pts, "opponent": opp_name,
            "opponent_points": opp_pts,
            "result": (None if my_pts is None or opp_pts is None
                       else ("win" if my_pts > opp_pts else
                             "loss" if my_pts < opp_pts else "tie")),
            "starters": starters, "bench": bench,
            "league_scores": standings,
            "our_rank": next((i for i, (n, _) in enumerate(standings, 1)
                              if n == cfg.get("team_name")), None),
            "forecasts": fc, "bench_mistake": worst}


def facts_text(f):
    """The fact sheet the model is allowed to use, and nothing else.

    Phrasing here is defensive. A small model will happily invert a
    relationship - given "Odunze was benched and outscored Kincaid" it wrote
    "the decision to start Odunze instead of Kincaid" - so anything with a
    direction is stated twice, once per side, in short sentences that cannot be
    recombined into their own opposite.
    """
    n_teams = f.get("league_size") or len(f["league_scores"])
    L = [f"League: {n_teams}-team full PPR. Our team: {f['team']}.",
         f"Season {f['season']}, week {f['week']}."]
    if f["our_points"] is not None:
        L.append(f"We scored {f['our_points']} points.")
    if f["opponent"]:
        L.append(f"We played {f['opponent']}, who scored {f['opponent_points']}. "
                 f"Result: we took a {f['result']}.")
    if f["our_rank"]:
        L.append(f"We ranked {f['our_rank']} out of {n_teams} teams "
                 f"in points scored this week.")
    if f["starters"]:
        L.append("These players were IN our starting lineup, with the points "
                 "they scored: " + ", ".join(
                     f"{p['name']} ({p['pos']}) {p['actual']}"
                     for p in f["starters"]) + ".")
    if f["bench"]:
        L.append("These players were NOT in our lineup - they sat on our bench, "
                 "and their points did not count for us: " + ", ".join(
                     f"{p['name']} {p['actual']}" for p in f["bench"]) + ".")
    if f["bench_mistake"]:
        m = f["bench_mistake"]
        L.append(f"Our worst lineup decision: we benched {m['sat']}, who scored "
                 f"{m['sat_pts']}. We started {m['started']} instead, who scored "
                 f"only {m['started_pts']}. Benching {m['sat']} cost us "
                 f"{m['cost']} points. To be clear: {m['sat']} did NOT play for "
                 f"us, and {m['started']} DID play for us.")
    if f["league_scores"]:
        top = f["league_scores"][:3]
        L.append("Highest scoring teams this week: " + ", ".join(
            f"{n} {v}" for n, v in top) + ".")
    if f["forecasts"]:
        fc = f["forecasts"]
        L.append(f"We published {fc['n']} probability forecasts before this week. "
                 f"Brier score {fc['brier']} against a climatology of "
                 f"{fc['climatology']}, skill score {fc['skill']}.")
    return "\n".join(L)


# --------------------------------------------------------------- the model
PROMPT = """You are the beat writer for a fantasy football team called {team}.
Write a short weekly recap - three tight paragraphs, no headings, no bullet
points, no markdown.

ABSOLUTE RULE: every number and every player name you write MUST come from the
facts below. Do not invent a statistic, a player, an opponent or a score. Do
not estimate. If something is not in the facts, do not mention it.

SECOND ABSOLUTE RULE: do not reverse who started and who sat. A player
described as benched did NOT play for us. A player described as in our lineup
DID play for us. Read those sentences twice before writing about them.

Be plain and direct. No hype, no cliches about "grit" or "statement wins". If
the team lost, say so first. If a bench decision cost points, say what it cost.

FACTS
{facts}

Write the recap now."""


def ollama(prompt, model=MODEL, temperature=0.4, timeout=TIMEOUT):
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": temperature, "num_predict": 600},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()).get("response", "").strip()


NUM = re.compile(r"\d+(?:\.\d+)?")


def check_numbers(text, facts_str):
    """Every number in the prose must appear in the facts. Returns the bad ones.

    A local 3B model is perfectly capable of writing "Achane ran for 94 yards"
    when no rushing yards were ever given to it. Numbers are the part a reader
    will quote and the part that is checkable, so they are checked. Small
    integers up to twelve are allowed through as ordinary prose ("three
    receivers", "the second half") rather than claims.
    """
    allowed = set(NUM.findall(facts_str))
    # Also allow the integer form of any decimal fact, and vice versa.
    for a in list(allowed):
        if "." in a:
            allowed.add(a.split(".")[0])
        else:
            allowed.add(f"{a}.0")
    bad = []
    for tok in NUM.findall(text):
        if tok in allowed:
            continue
        try:
            if float(tok) <= 12 and "." not in tok:
                continue
        except ValueError:
            pass
        bad.append(tok)
    return bad


def write_recap(facts, tries=3, verbose=True):
    fs = facts_text(facts)
    prompt = PROMPT.format(team=facts.get("team") or "our team", facts=fs)
    last, last_bad = None, None
    for i, temp in enumerate([0.4, 0.15, 0.0][:tries]):
        try:
            text = ollama(prompt, temperature=temp)
        except Exception as e:
            if verbose:
                print(f"  ! ollama failed: {e}", file=sys.stderr)
            break
        bad = check_numbers(text, fs)
        if verbose:
            print(f"  draft {i+1} at temp {temp}: {len(text)} chars, "
                  f"{len(bad)} unsupported number(s)"
                  + (f" {bad[:6]}" if bad else ""))
        if not bad:
            return {"ok": True, "text": text, "attempts": i + 1, "facts": fs}
        last, last_bad = text, bad
    return {"ok": False, "text": last, "bad": last_bad, "facts": fs,
            "fallback": fs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int)
    ap.add_argument("--facts", action="store_true")
    ap.add_argument("--post", action="store_true")
    a = ap.parse_args()
    f = build_facts(a.week, a.season)
    if a.facts:
        print(facts_text(f))
        return
    r = write_recap(f)
    print()
    if r["ok"]:
        print(r["text"])
    else:
        print("MODEL OUTPUT REJECTED - unsupported numbers: "
              f"{r.get('bad')}\nFalling back to the fact sheet:\n")
        print(r["fallback"])
    if a.post and ND:
        body = r["text"] if r["ok"] else r["fallback"]
        note = ("written locally by " + MODEL) if r["ok"] else \
               "the local model produced unsupported numbers; showing the facts"
        ND.post(embeds=[ND.embed(f"Week {f['week']} recap", body[:4000],
                                 ND.BLUE, None, note)])
        print("\nposted to Discord.")


if __name__ == "__main__":
    main()
