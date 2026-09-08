#!/usr/bin/env python3
"""Sleeper. The reference implementation, and the only one proven in a season.

Sleeper is the easy case and worth understanding first, because the other
adapters are defined by how they differ from it: the read API is open, needs no
authentication, and returns everything - league settings, every roster, weekly
matchups with per-player scoring, and a full player universe with live injury
designations.

What it does not offer is any way to *write*. Setting a lineup or making a
waiver claim happens by driving the real web app, which is what the DOM profile
is for.
"""
import sys, os, json, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .base import Platform, PlatformError

API = "https://api.sleeper.app/v1"
API2 = "https://api.sleeper.com"
UA = {"User-Agent": "Mozilla/5.0 (fantasy-agent)"}


def _get(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ! {url} -> {e}", file=sys.stderr)
        return None


class Sleeper(Platform):
    name = "sleeper"
    has_read_api = True
    setup_notes = (
        "Sleeper's API is open - no login, no token. All you need is your\n"
        "league ID, which is the long number in your league's URL:\n"
        "  https://sleeper.com/leagues/<LEAGUE_ID>/team")

    # ------------------------------------------------------------- reads
    def league(self):
        lg = _get(f"{API}/league/{self.cfg['league_id']}")
        if not lg:
            raise PlatformError(
                f"could not read league {self.cfg.get('league_id')!r} from "
                "Sleeper. Check the ID from your league URL.")
        slots, bench, flex = {}, 0, set()
        for pos in (lg.get("roster_positions") or []):
            if pos == "BN":
                bench += 1
            elif pos in ("FLEX", "SUPER_FLEX", "REC_FLEX", "WRRB_FLEX"):
                key = "SUPERFLEX" if pos == "SUPER_FLEX" else "FLEX"
                slots[key] = slots.get(key, 0) + 1
                flex |= {"RB", "WR", "TE"} if key == "FLEX" else {"QB", "RB", "WR", "TE"}
            elif pos != "IDP_FLEX":
                slots[pos] = slots.get(pos, 0) + 1
        ss = lg.get("scoring_settings") or {}
        return {
            "name": lg.get("name"), "teams": lg.get("total_rosters"),
            "scoring": ("ppr" if ss.get("rec", 0) >= 1 else
                        "half_ppr" if ss.get("rec", 0) > 0 else "std"),
            "roster_slots": slots, "flex_eligible": sorted(flex or {"RB", "WR", "TE"}),
            "bench_slots": bench, "scoring_settings": ss,
            "playoff_week_start": (lg.get("settings") or {}).get("playoff_week_start"),
            "waiver_budget": (lg.get("settings") or {}).get("waiver_budget"),
            "draft_id": lg.get("draft_id"),
        }

    def rosters(self):
        users = {u["user_id"]: u for u in
                 (_get(f"{API}/league/{self.cfg['league_id']}/users") or [])}
        out = []
        for r in (_get(f"{API}/league/{self.cfg['league_id']}/rosters") or []):
            u = users.get(r.get("owner_id")) or {}
            out.append({
                "roster_id": r.get("roster_id"), "owner_id": r.get("owner_id"),
                "team_name": (u.get("metadata") or {}).get("team_name")
                             or u.get("display_name"),
                "players": [str(p) for p in (r.get("players") or [])],
                "starters": [str(p) for p in (r.get("starters") or []) if p and p != "0"],
                "settings": r.get("settings") or {},
            })
        return out

    def matchups(self, week):
        out = []
        for m in (_get(f"{API}/league/{self.cfg['league_id']}/matchups/{week}") or []):
            out.append({
                "roster_id": m.get("roster_id"), "matchup_id": m.get("matchup_id"),
                "points": m.get("points"),
                "starters": [str(p) for p in (m.get("starters") or [])],
                "players_points": {str(k): v for k, v in
                                   (m.get("players_points") or {}).items()},
            })
        return out

    def player_universe(self):
        raw = _get(f"{API}/players/nfl") or {}
        out = {}
        for pid, p in raw.items():
            if not p:
                continue
            out[str(pid)] = {
                "name": p.get("full_name") or
                        f"{p.get('first_name','')} {p.get('last_name','')}".strip(),
                "pos": p.get("position"), "team": p.get("team"),
                "injury_status": p.get("injury_status"),
                "gsis_id": p.get("gsis_id"), "espn_id": p.get("espn_id"),
                "age": p.get("age"), "years_exp": p.get("years_exp"),
            }
        return out

    def projections(self, season, week=None):
        base = (f"{API2}/projections/nfl/{season}"
                + (f"/{week}" if week else ""))
        out = {}
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
            rows = _get(f"{base}?season_type=regular&position[]={pos}"
                        f"&order_by=pts_ppr") or []
            for r in rows:
                pid = r.get("player_id")
                if pid and r.get("stats"):
                    out.setdefault(str(pid), {"stats": r["stats"],
                                              "position": pos,
                                              "team": r.get("team"),
                                              "opponent": r.get("opponent"),
                                              "date": r.get("date")})
        return out

    def state(self):
        return _get(f"{API}/state/nfl") or {}
