#!/usr/bin/env python3
"""First-run setup. Asks which platform you are on, then configures itself.

    python3 engine/setup.py

Everything the agent needs it can work out for itself once it knows two things:
which fantasy platform you use, and which league is yours. Roster slots, scoring
rules, team count, flex eligibility and your own team id are all read from the
league rather than typed in, because a hand-typed scoring table is a silent
source of wrong answers for a whole season - and this project has already been
bitten once by scoring that looked right and was not.

What it cannot read is how to *change* your lineup, because no platform offers
a write API. For Sleeper that mapping ships with the project. For anything else
this hands off to `engine/explore.py`, which learns it from your own browser.
"""
import sys, os, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from platforms import base as PB

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(HERE, "config.json")


def ask(prompt, default=None, choices=None):
    while True:
        suffix = f" [{default}]" if default else ""
        if choices:
            suffix = f" ({'/'.join(choices)})" + suffix
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if not raw:
            continue
        if choices and raw.lower() not in [c.lower() for c in choices]:
            print(f"  please choose one of: {', '.join(choices)}")
            continue
        return raw


def choose_platform():
    reg = PB.available()
    print("\nWhich fantasy platform is your league on?\n")
    order = ["sleeper", "espn", "generic"]
    labels = {
        "sleeper": "Sleeper      - fully supported, read and write",
        "espn": "ESPN         - reads work; lineup control is learned from your browser",
        "generic": "Something else - everything is learned from your browser",
    }
    for i, k in enumerate(order, 1):
        print(f"  {i}. {labels.get(k, k)}")
    print()
    while True:
        raw = ask("Platform", default="1")
        key = None
        if raw.isdigit() and 1 <= int(raw) <= len(order):
            key = order[int(raw) - 1]
        elif raw.lower() in reg:
            key = raw.lower()
        if key:
            cls = reg[key]
            if cls.setup_notes:
                print("\n" + "\n".join("  " + l for l in
                                       cls.setup_notes.splitlines()) + "\n")
            return key, cls
        print("  didn't recognise that - enter 1, 2 or 3.")


def configure(platform_key, cls, cfg):
    cfg["platform"] = platform_key
    cfg["league_id"] = ask("League ID", default=cfg.get("league_id") or None)
    if platform_key == "espn":
        cfg["season"] = int(ask("Season year", default=str(cfg.get("season") or 2026)))
        if ask("Is the league private (needs login to view)?",
               default="n", choices=["y", "n"]).lower() == "y":
            print("  Both cookies are visible in your browser's devtools under\n"
                  "  Application > Cookies > fantasy.espn.com.")
            cfg["espn_swid"] = ask("  SWID cookie", default=cfg.get("espn_swid") or "")
            cfg["espn_s2"] = ask("  espn_s2 cookie", default=cfg.get("espn_s2") or "")

    plat = cls(cfg)
    print("\nReading your league ...")
    try:
        lg = plat.league()
    except Exception as e:
        print(f"\n  could not read the league: {e}")
        if platform_key == "generic":
            print("  That is expected for 'something else' - carry on.")
            lg = {}
        else:
            return None

    for k in ("teams", "scoring", "roster_slots", "flex_eligible",
              "bench_slots", "scoring_settings", "playoff_week_start",
              "waiver_budget", "draft_id"):
        if lg.get(k):
            cfg[k] = lg[k]
    if lg.get("name"):
        print(f"  league     : {lg['name']}")
    print(f"  teams      : {cfg.get('teams')}")
    print(f"  scoring    : {cfg.get('scoring')}")
    print(f"  starters   : {cfg.get('roster_slots')}")
    print(f"  bench      : {cfg.get('bench_slots')}")

    # Which of these teams is yours? Ask rather than guess - picking the wrong
    # one silently manages a stranger's roster.
    try:
        rs = plat.rosters()
    except Exception as e:
        rs = []
        print(f"  (could not list teams: {e})")
    if rs:
        print("\nWhich team is yours?")
        for i, r in enumerate(rs, 1):
            print(f"  {i:>2}. {r.get('team_name') or r.get('roster_id')}")
        pick = ask("Team", default="1")
        idx = int(pick) - 1 if pick.isdigit() and 0 < int(pick) <= len(rs) else 0
        cfg["team_name"] = rs[idx].get("team_name")
        cfg["user_id"] = str(rs[idx].get("owner_id") or "")
        cfg["roster_id"] = rs[idx].get("roster_id")
        print(f"  -> {cfg['team_name']}")
    else:
        cfg["team_name"] = ask("Your team name", default=cfg.get("team_name") or "my team")

    return cfg


def write(cfg):
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    print(f"\nwrote {CONFIG}")


def next_steps(platform_key):
    prof = PB.load_profile(platform_key)
    gaps = PB.profile_gaps(prof)
    print("\n" + "=" * 66)
    if platform_key == "sleeper" and not gaps:
        print("Sleeper is fully supported. Nothing else to map.\n")
    else:
        print("ONE MORE STEP - teaching the agent to change your lineup\n")
        print("No fantasy platform offers an API for setting a lineup or making")
        print("a waiver claim, so the agent does it by driving the real site in")
        print("a browser you are logged into. It needs to be shown where things")
        print("are, once.\n")
        print("  1. Start a browser it can attach to:")
        print("       ./run_chrome.sh")
        print("  2. Log into your fantasy platform in that window.")
        print("  3. Open your team page, then run:\n")
        print(f"       python3 engine/explore.py --platform {platform_key} \\")
        print("           --url '<your team page URL>' \\")
        print("           --players 'Player One,Player Two,Player Three'\n")
        print("The player names are anchors - it finds them on screen and works")
        print("out the page structure from where they sit. It only reads; it")
        print("disables anything that looks destructive before it starts, and")
        print("shows you what it learned before saving with --save.\n")
    print("Then check everything lines up:")
    print("    python3 engine/agent.py --status")
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", help="skip the question")
    a = ap.parse_args()

    cfg = {}
    if os.path.exists(CONFIG):
        try:
            cfg = json.load(open(CONFIG))
            print(f"(updating the existing {CONFIG})")
        except Exception:
            cfg = {}

    print("=" * 66)
    print("  fantasy-agent setup")
    print("=" * 66)

    if a.platform:
        reg = PB.available()
        if a.platform not in reg:
            raise SystemExit(f"unknown platform {a.platform!r}")
        key, cls = a.platform, reg[a.platform]
    else:
        key, cls = choose_platform()

    cfg = configure(key, cls, cfg)
    if cfg is None:
        raise SystemExit("\nsetup did not complete - fix the error above and re-run.")
    write(cfg)
    next_steps(key)


if __name__ == "__main__":
    main()
