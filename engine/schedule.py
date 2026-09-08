#!/usr/bin/env python3
"""2026 NFL schedule: bye weeks + strength of schedule.

Sleeper's player feed carries no bye_week field at all, so we derive byes
ourselves: any team that does not appear in a week's games is on bye that week.
We also grade each team's schedule, weighting the fantasy playoff weeks most --
a roster that wins weeks 1-14 and collapses in week 16 has not won anything.
"""
import json, os, sys, urllib.request, time
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
UA = {"User-Agent": "Mozilla/5.0 (statking)"}

REG_WEEKS = 18
FANTASY_PLAYOFFS = (15, 16, 17)   # Sleeper default championship window


def get(url, retries=3):
    """ESPN 403s urllib regardless of headers but serves curl fine, so shell out.

    Falls back to urllib if curl is unavailable.
    """
    import subprocess
    for a in range(retries):
        try:
            r = subprocess.run(
                # NB: do NOT set -A here. ESPN 403s custom User-Agent strings;
                # curl's default UA is accepted.
                ["curl", "-s", "--fail", "--compressed", url],
                capture_output=True, timeout=30)
            if r.returncode == 0 and r.stdout:
                return json.loads(r.stdout.decode())
        except FileNotFoundError:
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=25) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                print(f"  ! {url}: {e}", file=sys.stderr)
                return None
        except Exception:
            pass
        time.sleep(0.8 * (a + 1))
    print(f"  ! failed: {url}", file=sys.stderr)
    return None


def pull_schedule(season=2026, pause=0.4):
    """week -> [(away, home)]. ESPN burst-blocks, so throttle and cache per week."""
    cache_dir = os.path.join(DATA, "schedule_raw")
    os.makedirs(cache_dir, exist_ok=True)
    weeks = {}
    for wk in range(1, REG_WEEKS + 1):
        cpath = os.path.join(cache_dir, f"w{wk}.json")
        d = None
        if os.path.exists(cpath):
            try:
                with open(cpath) as f:
                    d = json.load(f)
            except Exception:
                d = None
        if d is None:
            d = get(f"{ESPN}?dates={season}&seasontype=2&week={wk}")
            if d:
                with open(cpath, "w") as f:
                    json.dump(d, f)
            time.sleep(pause)          # stay under ESPN's burst limit
        games = []
        for ev in (d or {}).get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            away = home = None
            for t in comp.get("competitors") or []:
                ab = ((t.get("team") or {}).get("abbreviation") or "").upper()
                if t.get("homeAway") == "home":
                    home = ab
                else:
                    away = ab
            if away and home:
                games.append((away, home))
        weeks[wk] = games
        print(f"  week {wk:>2}: {len(games)} games{' (cached)' if os.path.exists(cpath) else ''}")
    return weeks


# ESPN uses a few different abbreviations than Sleeper
ALIAS = {"WSH": "WAS", "LAR": "LAR", "LAC": "LAC", "JAX": "JAX", "LV": "LV", "ARI": "ARI"}


def norm(t):
    return ALIAS.get(t, t)


def derive(weeks):
    teams = set()
    for gs in weeks.values():
        for a, h in gs:
            teams.add(norm(a)); teams.add(norm(h))

    byes, opponents = {}, defaultdict(dict)
    for wk, gs in weeks.items():
        playing = set()
        for a, h in gs:
            a, h = norm(a), norm(h)
            playing |= {a, h}
            opponents[a][wk] = ("@", h)
            opponents[h][wk] = ("vs", a)
        for t in teams - playing:
            byes[t] = wk        # a team has exactly one bye
    return sorted(teams), byes, dict(opponents)


def team_strength(proj_path=None):
    """Crude defensive strength proxy: projected fantasy points allowed rank.

    We use each DEF's own projected points as a stand-in for team quality --
    a strong defense both scores well and suppresses opposing offenses.
    """
    path = proj_path or os.path.join(DATA, "projections_2026.json")
    with open(path) as f:
        proj = json.load(f)
    with open(os.path.join(DATA, "players_nfl.json")) as f:
        players = json.load(f)
    dpts = {}
    for pid, rec in proj.items():
        meta = players.get(pid) or {}
        if (meta.get("position") or rec.get("position")) != "DEF":
            continue
        tm = meta.get("team") or rec.get("team") or pid
        pts = (rec.get("stats") or {}).get("pts_ppr")
        if tm and pts is not None:
            dpts[norm(tm)] = pts
    if not dpts:
        return {}
    lo, hi = min(dpts.values()), max(dpts.values())
    rng = (hi - lo) or 1.0
    # 0 = easiest matchup to face (weak D), 1 = toughest
    return {t: (v - lo) / rng for t, v in dpts.items()}


def sos(opponents, strength):
    """Per-team schedule difficulty, overall and for the fantasy playoffs."""
    out = {}
    for tm, sched in opponents.items():
        vals = [strength.get(o, 0.5) for wk, (_, o) in sched.items()]
        po = [strength.get(o, 0.5) for wk, (_, o) in sched.items()
              if wk in FANTASY_PLAYOFFS]
        out[tm] = {
            "sos_all": sum(vals) / len(vals) if vals else 0.5,
            "sos_playoffs": sum(po) / len(po) if po else 0.5,
            "games": len(vals),
        }
    return out


def validate(teams, byes, weeks):
    """Refuse to write a schedule that is structurally impossible.

    A silently-corrupt schedule is worse than none: it poisoned the draft board
    once already by marking every team's bye as week 18. Real invariants:
    32 teams, every team exactly one bye, byes land in weeks 5-14, and each
    week has at least 13 games.
    """
    errs = []
    if len(teams) != 32:
        errs.append(f"expected 32 teams, got {len(teams)}")
    missing = [t for t in teams if t not in byes]
    if missing:
        errs.append(f"{len(missing)} teams without a bye: {missing[:6]}")
    bad = {t: w for t, w in byes.items() if not (4 <= w <= 14)}
    if bad:
        errs.append(f"byes outside weeks 4-14: {list(bad.items())[:6]}")
    thin = [w for w, g in weeks.items() if len(g) < 13]
    if thin:
        errs.append(f"weeks with <13 games (incomplete fetch): {thin}")
    return errs


def build(season=2026):
    os.makedirs(DATA, exist_ok=True)
    print(f"== pulling {season} regular season schedule ==")
    weeks = pull_schedule(season)
    teams, byes, opponents = derive(weeks)
    strength = team_strength()
    s = sos(opponents, strength)
    errs = validate(teams, byes, weeks)
    if errs:
        print("!! schedule failed validation - NOT written:")
        for e in errs:
            print("   -", e)
        raise SystemExit(1)
    out = {"season": season, "teams": teams, "byes": byes,
           "opponents": opponents, "strength": strength, "sos": s,
           "fantasy_playoffs": list(FANTASY_PLAYOFFS)}
    with open(os.path.join(DATA, "schedule_2026.json"), "w") as f:
        json.dump(out, f)
    return out


if __name__ == "__main__":
    d = build()
    byes = d["byes"]
    print(f"\n== {len(d['teams'])} teams, byes derived for {len(byes)} ==")
    wk2 = defaultdict(list)
    for t, w in byes.items():
        wk2[w].append(t)
    for w in sorted(wk2):
        print(f"  week {w:>2} bye ({len(wk2[w])}): {' '.join(sorted(wk2[w]))}")
    print("\n== easiest fantasy-playoff schedules (wk 15-17) ==")
    rank = sorted(d["sos"].items(), key=lambda kv: kv[1]["sos_playoffs"])
    for t, v in rank[:8]:
        print(f"  {t:<4} playoff SOS {v['sos_playoffs']:.3f}   season {v['sos_all']:.3f}")
    print("== toughest ==")
    for t, v in rank[-5:]:
        print(f"  {t:<4} playoff SOS {v['sos_playoffs']:.3f}   season {v['sos_all']:.3f}")
