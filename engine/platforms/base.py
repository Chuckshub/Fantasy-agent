#!/usr/bin/env python3
"""What a fantasy platform has to provide, and the two very different halves.

Every platform this agent supports splits cleanly in two, and the split is
worth naming because it decides how much work a new platform is.

**Reading** is easy and varies a lot. Sleeper publishes an open, unauthenticated
JSON API. ESPN has an undocumented but stable JSON endpoint that works with the
cookies a logged-in browser already holds. Yahoo requires OAuth. Each one needs
its own code, but it is ordinary HTTP and it is testable offline.

**Writing** is hard and varies hardly at all. No major platform offers a write
API for setting a lineup or making a waiver claim, so every one of them is
automated the same way: drive the real web app in a browser the user is already
logged into. What differs between platforms is not the technique, it is the
*selectors* - which element is a roster row, which one is the position button,
which one is the add button.

That second observation is what makes a new platform tractable. The write logic
does not need reimplementing per site; it needs a **DOM profile**, and a profile
can be learned by looking at the user's own browser rather than written by hand.
See `engine/explore.py`.

A platform therefore supplies:
  * a reader - however it likes
  * a DOM profile - usually learned, occasionally hand-written

and inherits everything else.
"""
import sys, os, json

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "profiles")


class PlatformError(Exception):
    """Anything that means we cannot safely read or write this league."""


# --------------------------------------------------------------- DOM profile
# A profile is deliberately just data. It is produced by the explorer, checked
# by a human once, and then read by the generic driver. Keeping it as JSON
# rather than code means a platform changing its markup is a re-run of the
# explorer rather than a patch release.
PROFILE_KEYS = {
    "team_url": "URL template for the user's roster page, {league_id} allowed",
    "players_url": "URL template for the add/free-agent list",
    "roster_row": "CSS selector matching one row per rostered player",
    "row_name": "selector, relative to a row, holding the player's name",
    "row_slot": "selector, relative to a row, holding the lineup slot label",
    "row_slot_click": "selector for the element that starts a lineup move",
    "bench_slot_labels": "list of slot labels that mean 'not starting'",
    "player_search": "selector for the free-agent search input",
    "player_row": "CSS selector matching one row in the free-agent list",
    "player_add": "selector, relative to a player row, for the add control",
    "confirm_add": "selector for the button that commits an add",
    "drop_row": "selector matching a droppable player inside the add dialog",
}


def profile_path(platform):
    return os.path.join(PROFILE_DIR, f"{platform}.json")


def load_profile(platform):
    p = profile_path(platform)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def save_profile(platform, profile):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with open(profile_path(platform), "w") as f:
        json.dump(profile, f, indent=1, sort_keys=True)
    return profile_path(platform)


def profile_gaps(profile):
    """Which required keys a profile is still missing."""
    if not profile:
        return sorted(PROFILE_KEYS)
    return sorted(k for k in PROFILE_KEYS if not profile.get(k))


# ------------------------------------------------------------------- reader
class Platform:
    """Base class. Subclasses implement the reads; writes are inherited.

    Every read returns plain dicts in one shape regardless of platform, because
    the whole engine above this - the projection model, the calibration, the
    lineup optimiser - must never learn which site the data came from. The day
    it does is the day supporting a second platform means touching all of it.
    """

    name = "base"
    #: True when the platform offers a read API that needs no browser at all.
    has_read_api = False
    #: Human-readable notes shown by the setup wizard.
    setup_notes = ""

    def __init__(self, cfg):
        self.cfg = cfg

    # ---- reads, implemented per platform -------------------------------
    def league(self):
        """{name, teams, scoring, roster_slots, flex_eligible, ...}"""
        raise NotImplementedError

    def rosters(self):
        """[{roster_id, owner_id, team_name, players[], starters[]}]"""
        raise NotImplementedError

    def matchups(self, week):
        """[{roster_id, matchup_id, points, starters[], players_points{}}]"""
        raise NotImplementedError

    def player_universe(self):
        """{platform_player_id: {name, pos, team, injury_status, bye}}"""
        raise NotImplementedError

    def projections(self, season, week=None):
        """{platform_player_id: {stats...}} - or {} if the platform has none.

        A platform without its own projections is not a blocker: the engine can
        fall back to modelling from play-by-play. It is a quality difference,
        not a capability one, and the setup wizard says so rather than refusing.
        """
        return {}

    # ---- writes, inherited ---------------------------------------------
    def profile(self):
        prof = load_profile(self.name)
        if not prof:
            raise PlatformError(
                f"no DOM profile for '{self.name}'. Run:\n"
                f"    python3 engine/explore.py --platform {self.name}\n"
                "which walks through your own browser and learns one.")
        gaps = profile_gaps(prof)
        if gaps:
            raise PlatformError(
                f"the DOM profile for '{self.name}' is incomplete - missing "
                f"{', '.join(gaps)}. Re-run engine/explore.py to fill it in.")
        return prof


# ---------------------------------------------------------------- registry
def available():
    """{name: class} for every platform that can be selected."""
    from . import sleeper, espn, generic
    return {sleeper.Sleeper.name: sleeper.Sleeper,
            espn.ESPN.name: espn.ESPN,
            generic.Generic.name: generic.Generic}


def get(name, cfg=None):
    reg = available()
    key = (name or "").lower().strip()
    if key not in reg:
        raise PlatformError(
            f"unknown platform '{name}'. Known: {', '.join(sorted(reg))}. "
            "Run python3 engine/setup.py to choose one.")
    return reg[key](cfg or {})
