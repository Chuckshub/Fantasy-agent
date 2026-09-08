#!/usr/bin/env python3
"""Live Sleeper draft sync: resolve league settings, our slot, and picks made.

Usage:
  python3 engine/sync.py <draft_id_or_url> [sleeper_username]
Writes real league settings into config.json so the board matches the league
instead of our defaults.
"""
import json, os, re, sys, urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (statking)"}
API = "https://api.sleeper.app/v1"


def get(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ! {url} -> {e}", file=sys.stderr)
        return None


def parse_draft_id(s):
    """Accept a raw id or any sleeper.com URL containing one."""
    s = s.strip()
    if s.isdigit():
        return s
    m = re.search(r"/draft/(?:nfl/)?(\d+)", s) or re.search(r"(\d{15,})", s)
    return m.group(1) if m else None


SCORING_MAP = {"ppr": "ppr", "half_ppr": "half_ppr", "std": "std", "standard": "std", "2qb": "half_ppr"}


def detect_scoring(league):
    """Infer scoring bucket from the league's actual rec value."""
    ss = (league or {}).get("scoring_settings") or {}
    rec = ss.get("rec")
    if rec is None:
        return None
    if rec >= 0.75:
        return "ppr"
    if rec >= 0.25:
        return "half_ppr"
    return "std"


def build_slots(roster_positions):
    """Sleeper roster_positions -> our slot config."""
    slots, bench, flex_el = {}, 0, set()
    superflex = False
    for rp in roster_positions or []:
        if rp == "BN":
            bench += 1
        elif rp in ("QB", "RB", "WR", "TE", "K", "DEF"):
            slots[rp] = slots.get(rp, 0) + 1
        elif rp in ("FLEX", "WRRB_FLEX", "REC_FLEX", "WRRB_WRT"):
            slots["FLEX"] = slots.get("FLEX", 0) + 1
            flex_el |= {"RB", "WR", "TE"} if rp in ("FLEX", "WRRB_WRT") else {"WR", "RB"}
        elif rp in ("SUPER_FLEX", "QB_FLEX"):
            slots["SUPERFLEX"] = slots.get("SUPERFLEX", 0) + 1
            superflex = True
        elif rp in ("IDP_FLEX", "DL", "LB", "DB"):
            pass  # IDP unsupported; ignored rather than mis-valued
    return slots, bench, sorted(flex_el or {"RB", "WR", "TE"}), superflex


def sync(draft_id, username=None, write=True):
    d = get(f"{API}/draft/{draft_id}")
    if not d:
        print("Could not load that draft id."); return None
    league_id = d.get("league_id")
    league = get(f"{API}/league/{league_id}") if league_id else None
    settings = d.get("settings") or {}

    slots, bench, flex_el, superflex = build_slots(
        (league or {}).get("roster_positions") or d.get("roster_positions"))
    scoring = detect_scoring(league) or SCORING_MAP.get(
        (d.get("metadata") or {}).get("scoring_type", ""), "half_ppr")

    cfg_path = os.path.join(HERE, "config.json")
    cfg = json.load(open(cfg_path))
    cfg.update({
        "league_id": league_id, "draft_id": str(draft_id),
        "scoring": scoring,
        "teams": settings.get("teams") or cfg["teams"],
        "roster_slots": slots or cfg["roster_slots"],
        "flex_eligible": flex_el,
        "bench_slots": bench or cfg.get("bench_slots", 6),
        "superflex": superflex,
        "draft_type": d.get("type"), "draft_status": d.get("status"),
        "rounds": settings.get("rounds"),
    })

    # League rules the engine and the season-long agent both need. These change
    # strategy, not just bookkeeping: the trade deadline bounds when offers are
    # legal, the veto threshold is why offers must look fair, and the absence of
    # IR slots means an injured player costs a roster spot.
    ls = (league or {}).get("settings") or {}
    cfg.update({
        "playoff_teams": ls.get("playoff_teams"),
        "playoff_week_start": ls.get("playoff_week_start"),
        "trade_deadline_week": ls.get("trade_deadline"),
        "trades_enabled": not ls.get("disable_trades", 0),
        "pick_trading": bool(ls.get("pick_trading")),
        "veto_votes_needed": ls.get("veto_votes_needed"),
        "trade_review_days": ls.get("trade_review_days"),
        "waiver_budget": ls.get("waiver_budget"),
        "waiver_bid_min": ls.get("waiver_bid_min"),
        "waiver_day_of_week": ls.get("waiver_day_of_week"),
        "waiver_clear_days": ls.get("waiver_clear_days"),
        "reserve_slots": ls.get("reserve_slots"),
        "max_keepers": ls.get("max_keepers"),
        "best_ball": bool(ls.get("best_ball")),
        "scoring_settings": (league or {}).get("scoring_settings") or {},
    })

    # our draft slot
    order = d.get("draft_order") or {}
    uid = None
    if username:
        u = get(f"{API}/user/{username}")
        uid = (u or {}).get("user_id")
        if uid and uid in order:
            cfg["draft_slot"] = order[uid]
            cfg["user_id"] = uid
    if write:
        json.dump(cfg, open(cfg_path, "w"), indent=2)

    picks = get(f"{API}/draft/{draft_id}/picks") or []
    print(f"draft {draft_id}: type={d.get('type')} status={d.get('status')}")
    print(f"  teams={cfg['teams']} rounds={cfg.get('rounds')} scoring={scoring} superflex={superflex}")
    print(f"  starters={slots} bench={bench} flex_eligible={flex_el}")
    print(f"  our slot={cfg.get('draft_slot')}  picks_made={len(picks)}")
    if not cfg.get("draft_slot"):
        print("  ! draft slot unknown -- pass your Sleeper username as arg 2")
    return cfg, d, picks


def picks_made(draft_id):
    return get(f"{API}/draft/{draft_id}/picks") or []


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    did = parse_draft_id(sys.argv[1])
    if not did:
        print("Could not parse a draft id from that input."); sys.exit(1)
    sync(did, sys.argv[2] if len(sys.argv) > 2 else None)
