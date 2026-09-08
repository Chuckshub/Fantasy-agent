#!/usr/bin/env python3
"""Discord reporting for StatKing.

Posts draft picks, daily lineup reasoning, the weekly matchup edge, and league
standings into #NFL-Fantasy-2026.

    python3 engine/notify_discord.py --test
    python3 engine/notify_discord.py --lineup --week 4
    python3 engine/notify_discord.py --standings
    python3 engine/notify_discord.py --edge --week 4

There is no discord.py on this machine and no way to install one, so this talks
to Discord's REST API over urllib. That is enough for posting; it means the bot
does not maintain a gateway connection and cannot *receive* commands, which is
fine for a reporter.

The token lives in ~/.config/statking/discord.env (mode 600), deliberately
outside the repo so it cannot be committed. Nothing here ever prints it.
"""
import sys, os, json, time, uuid, argparse, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ENV_PATH = os.path.expanduser("~/.config/statking/discord.env")
API = "https://discord.com/api/v10"
UA = "StatKing (https://localhost, 1.0)"

GREEN, AMBER, RED, BLUE = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x3498DB


class DiscordError(Exception):
    pass


def load_env():
    if not os.path.exists(ENV_PATH):
        raise DiscordError(f"no config at {ENV_PATH}")
    env = {}
    for line in open(ENV_PATH):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    if not env.get("DISCORD_BOT_TOKEN"):
        raise DiscordError("DISCORD_BOT_TOKEN missing")
    return env


def _req(path, method="GET", body=None, token=None, tries=4):
    """One API call, honouring Discord's rate limiter.

    Discord answers 429 with a `retry_after` in seconds. Ignoring it and
    retrying immediately just earns a longer ban, so wait exactly as told.
    """
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(tries):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bot {token}", "User-Agent": UA,
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code == 429 and attempt < tries - 1:
                try:
                    wait = float(json.loads(detail).get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait + 0.15, 10.0))
                continue
            if e.code == 403:
                raise DiscordError(
                    f"Discord returned 403 on {method} {path}. The bot is in the "
                    "server but lacks permission for this (posting needs View "
                    "Channel + Send Messages + Embed Links; reading commands "
                    "also needs Read Message History).")
            raise DiscordError(f"Discord API {e.code} on {method} {path}: {detail}")
    raise DiscordError(f"rate limited repeatedly on {method} {path}")


def resolve_channel(env, save=True):
    """Find #NFL-Fantasy-2026. Caches the id so we stop scanning every run."""
    token = env["DISCORD_BOT_TOKEN"]
    if env.get("DISCORD_CHANNEL_ID"):
        return env["DISCORD_CHANNEL_ID"]
    want = (env.get("DISCORD_CHANNEL_NAME") or "NFL-Fantasy-2026").lower()
    guilds = _req("/users/@me/guilds", token=token)
    if not guilds:
        raise DiscordError(
            "The bot is not in any Discord server yet, so it has nowhere to "
            "post. Invite it first - see the invite URL printed by --invite.")
    for g in guilds:
        for c in _req(f"/guilds/{g['id']}/channels", token=token) or []:
            if c.get("type") == 0 and c["name"].lower() == want:
                env["DISCORD_CHANNEL_ID"] = c["id"]   # cache in memory too, or
                if save:                              # every call rescans and 429s
                    with open(ENV_PATH, "a") as f:
                        f.write(f"DISCORD_CHANNEL_ID={c['id']}\n")
                return c["id"]
    names = [f"#{c['name']}" for g in guilds
             for c in (_req(f"/guilds/{g['id']}/channels", token=token) or [])
             if c.get("type") == 0]
    raise DiscordError(f"no channel named #{want}. Bot can see: {', '.join(names) or 'none'}")


def post(content=None, embeds=None, env=None):
    env = env or load_env()
    cid = resolve_channel(env)
    body = {}
    if content:
        body["content"] = content[:1900]
    if embeds:
        body["embeds"] = embeds[:10]
    return _req(f"/channels/{cid}/messages", "POST", body,
                token=env["DISCORD_BOT_TOKEN"])


def post_file(path, content=None, embeds=None, env=None, filename=None):
    """Upload a file (a chart) with an optional message.

    Discord takes attachments as multipart/form-data, which `_req` cannot send
    because it serialises JSON. Rather than pull in a dependency for one call,
    the body is assembled by hand - it is a well-specified format and this is
    the only place that needs it. Rate limiting is handled the same way as
    everywhere else: obey `retry_after` rather than hammering.
    """
    env = env or load_env()
    cid = resolve_channel(env)
    filename = filename or os.path.basename(path)
    with open(path, "rb") as f:
        blob = f.read()

    payload = {}
    if content:
        payload["content"] = content[:1900]
    if embeds:
        payload["embeds"] = embeds[:10]
    payload["attachments"] = [{"id": 0, "filename": filename}]

    boundary = "----statking" + uuid.uuid4().hex
    pre = (f"--{boundary}\r\n"
           'Content-Disposition: form-data; name="payload_json"\r\n'
           "Content-Type: application/json\r\n\r\n"
           f"{json.dumps(payload)}\r\n"
           f"--{boundary}\r\n"
           f'Content-Disposition: form-data; name="files[0]"; '
           f'filename="{filename}"\r\n'
           "Content-Type: application/octet-stream\r\n\r\n").encode()
    body = pre + blob + f"\r\n--{boundary}--\r\n".encode()

    url = f"{API}/channels/{cid}/messages"
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bot {env['DISCORD_BOT_TOKEN']}", "User-Agent": UA,
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code == 429 and attempt < 3:
                try:
                    wait = float(json.loads(detail).get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait + 0.15, 10.0))
                continue
            raise DiscordError(f"Discord API {e.code} uploading {filename}: {detail}")
    raise DiscordError(f"rate limited repeatedly uploading {filename}")


def invite_url(env=None):
    env = env or load_env()
    me = _req("/users/@me", token=env["DISCORD_BOT_TOKEN"])
    # View Channels + Send Messages + Embed Links + Attach Files
    perms = 1024 | 2048 | 16384 | 32768
    return (f"https://discord.com/api/oauth2/authorize?client_id={me['id']}"
            f"&permissions={perms}&scope=bot"), me.get("username")


# ------------------------------------------------------------------ charts
BLOCKS = "▏▎▍▌▋▊▉█"


def bar_chart(pairs, width=28, unit=""):
    """Monospace bar chart. Discord renders code blocks in a fixed-width font,
    which makes this readable without shipping an image encoder."""
    if not pairs:
        return "(no data)"
    hi = max(v for _, v in pairs) or 1.0
    pad = max(len(str(k)) for k, _ in pairs)
    out = []
    for k, v in pairs:
        filled = (v / hi) * width
        full = int(filled)
        frac = filled - full
        bar = "█" * full + (BLOCKS[int(frac * 8)] if frac > 0.06 else "")
        out.append(f"{str(k):<{pad}}  {bar:<{width + 1}} {v:>7.1f}{unit}")
    return "\n".join(out)


# ---------------------------------------------------------------- messages
def embed(title, desc, color=BLUE, fields=None, footer=None):
    e = {"title": title[:250], "description": desc[:4000], "color": color}
    if fields:
        e["fields"] = [{"name": n[:250], "value": v[:1020],
                        "inline": bool(i)} for n, v, i in fields][:25]
    if footer:
        e["footer"] = {"text": footer[:2040]}
    return e


def draft_pick_embed(player, pick_no, reasoning, ok=True):
    return embed(
        f"{'Pick made' if ok else 'PICK FAILED'} - #{pick_no}: {player['name']}",
        f"**{player['pos']} - {player.get('team') or 'FA'}**\n{reasoning}",
        GREEN if ok else RED, footer="StatKing draft engine")


def pick_feed_embed(picks, state, cfg, board_top=None):
    """Announce picks other teams made, and what they cost us.

    A live draft feed is the point of the channel: silence between our own picks
    reads like the automation is dead. Each message says who went, whether any of
    our targets just came off the board, and what we would take if we were on the
    clock right now.
    """
    lines = []
    for p in picks:
        m = p.get("metadata") or {}
        rnd = p.get("round")
        slot = p.get("draft_slot")
        who = f"{m.get('first_name','')} {m.get('last_name','')}".strip()
        mine = " **<- US**" if slot == cfg.get("draft_slot") else ""
        lines.append(f"`{rnd}.{str(slot).zfill(2)}` pick {p.get('pick_no')} - "
                     f"**{who}** ({m.get('position','')} {m.get('team','')}){mine}")
    fields = []
    if board_top:
        fields.append(("Best available now", "\n".join(
            f"**{q['name']}** ({q['pos']}) VORP {q['vorp']:.0f}"
            for q in board_top[:5]), 0))
    if state:
        fields.append(("Our next pick", state, 1))
    return embed("Draft feed", "\n".join(lines[-8:]), BLUE, fields)


def pick_analysis_embed(best, cands, meta, state, cfg, deliberation=None):
    """The full reasoning behind one of our picks, as a table.

    A one-line "we took X" gives no way to tell a considered pick from a coin
    flip. This shows every candidate that was close, what separated them, and -
    when the margin is thin - says so plainly instead of narrating confidence
    the numbers do not support.
    """
    p = best["player"]
    rows = []
    for c in cands[:6]:
        q = c["player"]
        mark = "**>**" if q["pid"] == p["pid"] else "  "
        rows.append(f"{mark} `{c['score']:>6.1f}` **{q['name']}** {q['pos']}"
                    f"-{q.get('team') or 'FA'} · VORP {q['vorp']:.0f} · "
                    f"{c['survive_next']*100:.0f}% to last")
    fields = [("Candidates considered", "\n".join(rows), 0)]

    gap = (cands[0]["score"] - cands[1]["score"]) if len(cands) > 1 else 99
    if gap < 1.5:
        fields.append(("Margin", f"Top two separated by **{gap:.2f} points** - "
                       "close enough that this is a tiebreak, not a conviction.", 0))
    fields.append(("Why", explain_short(best, meta), 0))

    have = state.roster_counts()
    need = {k: v for k, v in state.unfilled_starters().items() if v}
    fields.append(("Roster after", f"{dict(sorted(have.items()))}", 1))
    fields.append(("Still needed", f"{need or 'starters filled'}", 1))
    byes = {}
    for q in state.my_roster:
        if q.get("bye"):
            byes.setdefault(q["bye"], []).append(q["name"])
    clash = {k: v for k, v in byes.items() if len(v) > 1}
    fields.append(("Bye weeks", ", ".join(f"wk{k}: {len(v)}" for k, v in sorted(byes.items()))
                   + (f"  ** collision {clash} **" if clash else "  (no collision)"), 0))
    if deliberation:
        fields.append(("Deliberation", deliberation, 0))
    return embed(f"Pick {meta['pick']} (round {meta.get('round','?')}): {p['name']}",
                 f"**{p['pos']} - {p.get('team') or 'FA'}**", GREEN, fields,
                 "scores are VORP plus opportunity cost, times need / bye / "
                 "playoff-schedule / availability")


def explain_short(cand, meta):
    import draft as _D
    return _D.explain(cand, meta)


def lineup_embeds(res, week, cfg):
    starters = sorted(res["lineup"], key=lambda x: -x["proj"])
    bench = sorted(res["bench"], key=lambda x: -x["proj"])
    total = sum(p["proj"] for p in starters)

    def line(p, why=True):
        bits = f"**{p['name']}** ({p['pos']}) `{p['proj']:.1f}`"
        if p.get("opponent"):
            m = p.get("matchup_mult", 1.0)
            tag = "favourable" if m > 1.04 else ("tough" if m < 0.96 else "neutral")
            bits += f" vs {p['opponent']} - {tag}"
        if p.get("reason"):
            bits += f" _{p['reason']}_"
        return bits

    fields = [("Starting", "\n".join(line(p) for p in starters[:12]) or "-", 0)]
    if bench:
        fields.append(("Bench - and why they sit", "\n".join(
            f"{line(p)}" for p in bench[:8]), 0))
    if res.get("unavailable"):
        fields.append(("Cannot play", "\n".join(
            f"**{p['name']}** ({p['pos']}) - {p['reason']}"
            for p in res["unavailable"]), 0))
    if res.get("problems"):
        fields.append(("Needs action", "\n".join(f"- {x}" for x in res["problems"]), 0))
    if res.get("swaps"):
        fields.append(("Rebalance", "\n".join(
            (f"sit **{s['sit']['name']}** - no replacement available, pick one up"
             if s["start"] is None else
             f"start **{s['start']['name']}**, sit **{s['sit']['name']}** "
             f"({s['gain']:+.1f})") for s in res["swaps"]), 0))
    color = RED if res.get("problems") else (AMBER if res.get("swaps") else GREEN)
    return [embed(f"Week {week} lineup", f"Projected **{total:.1f}** points",
                  color, fields, "projections x availability; bye/Out = 0")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--invite", action="store_true")
    ap.add_argument("--lineup", action="store_true")
    ap.add_argument("--standings", action="store_true")
    ap.add_argument("--week", type=int, default=1)
    ap.add_argument("--roster", help="comma-separated names (before the draft)")
    a = ap.parse_args()

    env = load_env()
    if a.invite:
        url, who = invite_url(env)
        print(f"bot: {who}\ninvite URL:\n  {url}")
        return
    if a.test:
        r = post(embeds=[embed(
            "StatKing is online",
            "Draft alerts, daily lineup reasoning, weekly matchup edges and "
            "standings will post here.", GREEN,
            footer="pure-stdlib Python; no gateway, posting only")], env=env)
        print("posted, message id", r.get("id"))
        return

    from value import build_board, load_config
    import lineup as LU
    cfg = load_config()
    board, _, _ = build_board(cfg)
    if a.lineup:
        if not a.roster:
            print("no roster yet - pass --roster for a pre-draft dry run"); return
        by = {p["name"].lower(): p for p in board}
        roster = [by[n.strip().lower()] for n in a.roster.split(",")
                  if n.strip().lower() in by]
        res = LU.analyse(roster, cfg, a.week)
        r = post(embeds=lineup_embeds(res, a.week, cfg), env=env)
        print("posted, message id", r.get("id"))


if __name__ == "__main__":
    try:
        main()
    except DiscordError as e:
        print(f"discord: {e}", file=sys.stderr); sys.exit(1)
