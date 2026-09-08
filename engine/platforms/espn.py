#!/usr/bin/env python3
"""ESPN Fantasy Football.

ESPN has no published API, but the site itself is a React app talking to a JSON
endpoint, and that endpoint answers perfectly well if you send the cookies a
logged-in browser already holds. So the reader here is real code rather than a
stub - it just needs two cookies, and the setup wizard can lift those straight
out of the browser you are already signed into rather than making you find them
by hand.

    https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/<year>
        /segments/0/leagues/<league_id>?view=mRoster&view=mTeam&view=mSettings

Two things differ from Sleeper in ways that matter to the rest of the engine.

**Player ids are ESPN's own**, not Sleeper's and not nflverse's. That is fine -
the crosswalk in the analytics layer already routes through `espn_id`, which
Sleeper's player file carries, so ESPN ids join to play-by-play without a new
mapping.

**Private leagues need cookies; public ones do not.** A public league reads with
no authentication at all. The wizard tries anonymously first and only asks for
cookies when it has to, because the majority of leagues do not need them and
asking for credentials you do not need is a bad habit.

Writing is the same story as everywhere: no write API, so lineup changes and
waiver claims go through the DOM profile that `engine/explore.py` learns from
your own browser.
"""
import sys, os, json, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .base import Platform, PlatformError

READ = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
        "{season}/segments/0/leagues/{league_id}")
UA = {"User-Agent": "Mozilla/5.0 (fantasy-agent)"}

# ESPN encodes lineup slots as integers. Only the ones a fantasy roster uses are
# listed; anything else falls through to BN so an unknown slot can never be
# mistaken for a starting one.
SLOT = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "DEF", 17: "K",
        23: "FLEX", 20: "BN", 21: "IR", 7: "SUPERFLEX"}
POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}


class ESPN(Platform):
    name = "espn"
    has_read_api = True
    setup_notes = (
        "Your league ID is in the URL when you view your team:\n"
        "  https://fantasy.espn.com/football/team?leagueId=<LEAGUE_ID>&seasonId=<YEAR>\n"
        "Public leagues need nothing else. Private leagues need two cookies\n"
        "(SWID and espn_s2); the setup wizard can read them out of the browser\n"
        "you are already logged into, so you do not have to go hunting.")

    def _cookies(self):
        swid = self.cfg.get("espn_swid") or ""
        s2 = self.cfg.get("espn_s2") or ""
        if swid and s2:
            return {"Cookie": f"SWID={swid}; espn_s2={s2}"}
        return {}

    def _get(self, views=(), extra=""):
        season = self.cfg.get("season") or self.cfg.get("season_year") or 2026
        url = READ.format(season=season, league_id=self.cfg.get("league_id"))
        if views:
            url += "?" + "&".join(f"view={v}" for v in views)
        url += extra
        headers = dict(UA)
        headers.update(self._cookies())
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise PlatformError(
                    "ESPN refused the request. That normally means the league "
                    "is private and needs the SWID and espn_s2 cookies - run "
                    "`python3 engine/setup.py` and it will pull them out of "
                    "your logged-in browser.")
            raise PlatformError(f"ESPN returned {e.code} for {url}")
        except Exception as e:
            raise PlatformError(f"could not reach ESPN: {e}")

    # ------------------------------------------------------------- reads
    def league(self):
        d = self._get(views=("mSettings",))
        s = (d or {}).get("settings") or {}
        roster = (s.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
        slots, bench, flex = {}, 0, set()
        for k, n in roster.items():
            if not n:
                continue
            label = SLOT.get(int(k))
            if label in (None, "IR"):
                continue
            if label == "BN":
                bench += n
            elif label in ("FLEX", "SUPERFLEX"):
                slots[label] = slots.get(label, 0) + n
                flex |= ({"RB", "WR", "TE"} if label == "FLEX"
                         else {"QB", "RB", "WR", "TE"})
            else:
                slots[label] = slots.get(label, 0) + n
        scoring = (s.get("scoringSettings") or {})
        items = {int(i.get("statId")): i.get("points")
                 for i in (scoring.get("scoringItems") or [])
                 if i.get("statId") is not None}
        rec = items.get(53, 0) or 0
        return {
            "name": s.get("name"), "teams": s.get("size"),
            "scoring": "ppr" if rec >= 1 else "half_ppr" if rec > 0 else "std",
            "roster_slots": slots,
            "flex_eligible": sorted(flex or {"RB", "WR", "TE"}),
            "bench_slots": bench,
            "scoring_settings": _espn_scoring(items),
            "playoff_week_start": s.get("scheduleSettings", {}).get(
                "matchupPeriodCount"),
        }

    def rosters(self):
        d = self._get(views=("mRoster", "mTeam"))
        out = []
        for t in (d or {}).get("teams", []):
            entries = ((t.get("roster") or {}).get("entries") or [])
            players, starters = [], []
            for e in entries:
                pid = str(e.get("playerId"))
                players.append(pid)
                if SLOT.get(e.get("lineupSlotId"), "BN") not in ("BN", "IR"):
                    starters.append(pid)
            name = (t.get("name")
                    or f"{t.get('location','')} {t.get('nickname','')}".strip())
            out.append({"roster_id": t.get("id"),
                        "owner_id": (t.get("owners") or [None])[0],
                        "team_name": name or f"Team {t.get('id')}",
                        "players": players, "starters": starters,
                        "settings": {}})
        return out

    def matchups(self, week):
        d = self._get(views=("mMatchupScore",),
                      extra=f"&scoringPeriodId={int(week)}")
        out = []
        for m in (d or {}).get("schedule", []):
            if m.get("matchupPeriodId") != int(week):
                continue
            for side in ("home", "away"):
                s = m.get(side) or {}
                if not s.get("teamId"):
                    continue
                out.append({"roster_id": s.get("teamId"),
                            "matchup_id": m.get("id"),
                            "points": s.get("totalPoints"),
                            "starters": [], "players_points": {}})
        return out

    def player_universe(self):
        d = self._get(views=("kona_player_info",))
        out = {}
        for p in (d or {}).get("players", []):
            pl = p.get("player") or p
            pid = str(pl.get("id") or p.get("id"))
            out[pid] = {
                "name": pl.get("fullName"),
                "pos": POS.get(pl.get("defaultPositionId")),
                "team": pl.get("proTeamId"),
                "injury_status": pl.get("injuryStatus"),
                "espn_id": pid,
            }
        return out

    def projections(self, season, week=None):
        # ESPN ships projections inside the player payload rather than as their
        # own feed. Left unimplemented on purpose: the engine already falls back
        # to modelling from play-by-play, and a half-parsed projection would be
        # worse than an honest absence.
        return {}


def _espn_scoring(items):
    """ESPN stat ids -> the engine's scoring keys, for the ones that matter."""
    m = {3: "pass_yd", 4: "pass_td", 20: "pass_int",
         24: "rush_yd", 25: "rush_td",
         42: "rec_yd", 43: "rec_td", 53: "rec",
         72: "fum_lost"}
    return {name: items[sid] for sid, name in m.items() if sid in items}
