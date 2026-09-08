#!/usr/bin/env python3
"""A platform the agent has never seen, learned entirely from your browser.

This is the escape hatch, and the reason the adapter layer is shaped the way it
is. If you are on a site nobody has written a reader for - a smaller host, a
league on a platform this project has never heard of - you are not stuck. What
the agent needs from a platform is a roster, a lineup, and a way to change them,
and all three are visible on screen in a browser you are already logged into.

So `engine/explore.py` reads them off the page instead of off an API. The
result is a DOM profile, and this adapter is a reader backed by that profile
rather than by HTTP.

**This is the weakest of the three adapters and the docstring should say so.**
Reading a roster off rendered HTML gets names and slots, and that is enough to
set a lineup correctly - which is the single highest-value thing the agent does.
It does not get you historical scoring, per-player weekly points, or the league's
exact scoring rules, so the projection model falls back to modelling from
play-by-play alone and the calibration is thinner. Prefer a real reader if one
exists for your platform; use this when one does not.
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .base import Platform, PlatformError, load_profile


class Generic(Platform):
    name = "generic"
    has_read_api = False
    setup_notes = (
        "For any platform without a built-in reader. You will be walked through\n"
        "your own browser once so the agent can learn where the roster, the\n"
        "lineup buttons and the add-player controls are. Nothing is guessed:\n"
        "every selector it learns is verified against players it can already\n"
        "see on your screen, and shown to you before it is saved.")

    def _profile(self):
        prof = load_profile(self.cfg.get("profile_name") or self.name)
        if not prof:
            raise PlatformError(
                "no DOM profile yet. Run:\n"
                "    python3 engine/explore.py --platform generic\n"
                "and it will learn one from your browser.")
        return prof

    def league(self):
        prof = self._profile()
        # Everything here comes from what the user confirmed during setup,
        # because there is no API to ask. The config is the source of truth.
        return {"name": self.cfg.get("league_name") or "your league",
                "teams": self.cfg.get("teams"),
                "scoring": self.cfg.get("scoring", "ppr"),
                "roster_slots": self.cfg.get("roster_slots") or {},
                "flex_eligible": self.cfg.get("flex_eligible")
                                 or ["RB", "WR", "TE"],
                "bench_slots": self.cfg.get("bench_slots", 6),
                "scoring_settings": self.cfg.get("scoring_settings") or {},
                "learned_from": prof.get("_learned_at")}

    def rosters(self):
        """Read the one roster we can see: the user's own team page.

        A learned profile can only see the pages the user has. Other managers'
        rosters are usually reachable too, but the URL pattern differs per site
        and guessing it is how an agent ends up scraping the wrong league. So
        this returns our roster and says so, rather than inventing thirteen
        empty teams that the rest of the engine would treat as real.
        """
        from .. import cdp  # local import: browser use is optional elsewhere
        prof = self._profile()
        url = prof["team_url"].format(**self.cfg)
        cdp.open_url(url, ready_js="JSON.stringify(document.querySelectorAll(%s).length>0)"
                     % json.dumps(prof["roster_row"]))
        page, _ = cdp.attach(url.split("//", 1)[-1].split("/", 1)[-1][:20] or "http")
        try:
            rows = page.evaluate(_READ_ROSTER_JS % (
                json.dumps(prof["roster_row"]), json.dumps(prof["row_name"]),
                json.dumps(prof["row_slot"])))
        finally:
            page.close()
        if not isinstance(rows, list):
            raise PlatformError(f"could not read the roster page: {rows!r}")
        bench = set(prof.get("bench_slot_labels") or ["BN", "BE", "Bench", "IR"])
        players = [r["name"] for r in rows if r.get("name")]
        starters = [r["name"] for r in rows
                    if r.get("name") and r.get("slot") not in bench]
        return [{"roster_id": 1, "owner_id": self.cfg.get("user_id") or "me",
                 "team_name": self.cfg.get("team_name") or "my team",
                 "players": players, "starters": starters,
                 "settings": {}, "_names_not_ids": True}]

    def matchups(self, week):
        return []

    def player_universe(self):
        return {}


_READ_ROSTER_JS = """
(() => {
  const rows = [...document.querySelectorAll(%s)];
  return JSON.stringify(rows.map((el, i) => {
    const n = el.querySelector(%s), s = el.querySelector(%s);
    return {i,
            name: n ? n.innerText.trim() : null,
            slot: s ? s.innerText.trim().replace(/\\s+/g, '') : null};
  }));
})()
"""
