#!/usr/bin/env python3
"""Draft state + pick decision engine.

Core idea: a pick's worth is not its VORP alone, it's VORP plus the
*opportunity cost* of not taking that position now -- how much worse the
position will be by the time we pick again. Positional runs are what actually
cost you a draft, so we estimate them by Monte-Carlo simulating the picks
between now and our next turn using each player's ADP as a noisy signal.
"""
import json, os, random, math
from value import build_board, load_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
random.seed(17)  # deterministic recommendations for a given board state


def adp_sigma(adp):
    """Uncertainty grows with ADP: early picks are predictable, late ones aren't."""
    return max(4.0, 0.22 * adp)


class DraftState:
    def __init__(self, cfg, board):
        self.cfg = cfg
        self.board = board
        self.by_pid = {p["pid"]: p for p in board}
        self.drafted = {}        # pid -> team slot that took them
        self.my_roster = []
        self.teams = cfg["teams"]
        self.slots = cfg["roster_slots"]
        self.my_slot = cfg.get("draft_slot")
        # Trust the league's own round count. Deriving it from starter slots
        # plus bench assumes those three numbers agree with the draft, and they
        # did not: a 13-round draft against 10 starters + 5 bench derived 15,
        # so my_pick_numbers() invented two picks (190, 203) that could never
        # come. rounds_left, legal_candidates and availability_multiplier all
        # read this, so an inflated value makes the endgame look further away
        # than it is. Fall back to the derived figure only when the league did
        # not tell us.
        self.rounds = (cfg.get("rounds")
                       or sum(cfg["roster_slots"].values()) + cfg.get("bench_slots", 0))

    # ---------- snake draft math ----------
    def pick_no_for(self, slot, rnd):
        """1-indexed overall pick number for a draft slot in a given round."""
        if rnd % 2 == 1:
            return (rnd - 1) * self.teams + slot
        return (rnd - 1) * self.teams + (self.teams - slot + 1)

    def my_pick_numbers(self):
        if not self.my_slot:
            return []
        return [self.pick_no_for(self.my_slot, r) for r in range(1, self.rounds + 1)]

    def next_two_picks(self, picks_made):
        """(current pick number, our following pick number) given picks already made."""
        upcoming = [p for p in self.my_pick_numbers() if p > picks_made]
        cur = upcoming[0] if upcoming else None
        nxt = upcoming[1] if len(upcoming) > 1 else None
        return cur, nxt

    # ---------- roster accounting ----------
    def roster_counts(self):
        c = {}
        for p in self.my_roster:
            c[p["pos"]] = c.get(p["pos"], 0) + 1
        return c

    def unfilled_starters(self):
        """Remaining mandatory starter slots by position, flex counted separately."""
        c = self.roster_counts()
        need = {}
        for pos, n in self.slots.items():
            if pos in ("FLEX", "SUPERFLEX"):
                continue
            need[pos] = max(0, n - c.get(pos, 0))
        # flex absorbs surplus RB/WR/TE
        surplus = sum(max(0, c.get(p, 0) - self.slots.get(p, 0))
                      for p in self.cfg["flex_eligible"])
        need["FLEX"] = max(0, self.slots.get("FLEX", 0) - surplus)
        return need

    def available(self):
        return [p for p in self.board if p["pid"] not in self.drafted]


def simulate_survival(state, avail, picks_between, sims=300):
    """P(player still on the board at our next pick) via ADP-noise Monte Carlo."""
    if picks_between <= 0:
        return {p["pid"]: 1.0 for p in avail}
    pool = [p for p in avail if p.get("adp")]
    noadp = [p for p in avail if not p.get("adp")]
    survived = {p["pid"]: 0 for p in avail}
    for _ in range(sims):
        scored = []
        for p in pool:
            jitter = random.gauss(0, adp_sigma(p["adp"]))
            scored.append((p["adp"] + jitter, p["pid"]))
        scored.sort()
        taken = {pid for _, pid in scored[:picks_between]}
        for p in avail:
            if p["pid"] not in taken:
                survived[p["pid"]] += 1
    out = {pid: n / sims for pid, n in survived.items()}
    for p in noadp:            # undrafted-by-ADP players almost always survive
        out[p["pid"]] = 0.97
    return out


def expected_best_next(avail, survival, pos):
    """E[VORP of best player at pos available at our next pick].

    Players are sorted by VORP; the best survivor is the first one that lasts,
    so we walk down the list accumulating the probability everyone above is gone.
    """
    cand = sorted([p for p in avail if p["pos"] == pos], key=lambda x: -x["vorp"])
    exp, p_all_gone = 0.0, 1.0
    for p in cand[:40]:
        s = survival.get(p["pid"], 0.0)
        exp += p_all_gone * s * p["vorp"]
        p_all_gone *= (1 - s)
        if p_all_gone < 1e-4:
            break
    return exp


def max_at_pos(cfg):
    """Hard roster caps. K/DEF are streaming positions -- never roster a second.
    Carrying a backup you can never start is strictly worse than a lottery ticket."""
    sf = cfg.get("superflex")
    return {"QB": 3 if sf else 2, "RB": 6, "WR": 7, "TE": 2, "K": 1, "DEF": 1}


def mandatory_unfilled(state):
    """Starter slots we cannot legally leave empty on Sunday."""
    c = state.roster_counts()
    need = {}
    for pos, n in state.slots.items():
        if pos in ("FLEX", "SUPERFLEX"):
            continue
        d = n - c.get(pos, 0)
        if d > 0:
            need[pos] = d
    return need


# Kicker and defence projections barely predict the finish: a preseason ranking
# correlates with actual season order at rho +0.01/+0.25 (K) and +0.18/+0.37
# (DEF) over 2024-25, against +0.62/+0.71 for tight end. See MODEL.md.
#
# So their VORP is real but unreliable, and the honest treatment is a haircut
# rather than a blanket ban. A hard "never before the last two rounds" rule was
# costing us: at pick 134 the best available defence was VORP 10 and the best
# available skill player was 6, while waiting to pick 190 leaves roughly the
# twelfth-best defence at VORP -2. Discounting instead lets them win once the
# skill board is genuinely bare, and lose while it is not.
KDEF_RELIABILITY = 0.40


def legal_candidates(state, avail, rounds_left):
    """Filter to picks that keep a legal, non-wasteful roster.

    Three rules, in order of importance:
      1. Never exceed the positional cap (stops K/DEF and QB hoarding).
      2. If remaining rounds only just cover our unfilled starter slots,
         draft nothing but those slots -- this is what prevents ending
         the draft with an empty TE or QB spot.
      3. K/DEF are held out only while more than six rounds remain. After that
         they compete on merit, carrying the KDEF_RELIABILITY haircut, because
         their projections barely predict the finish. The old rule banned them
         until the final two rounds outright, which meant passing a VORP-10
         defence for a VORP-6 receiver and then taking whatever was left.
    """
    caps = max_at_pos(state.cfg)
    counts = state.roster_counts()
    must = mandatory_unfilled(state)
    must_total = sum(must.values())
    endgame = must_total >= rounds_left

    out = []
    for p in avail:
        pos = p["pos"]
        if counts.get(pos, 0) >= caps.get(pos, 99):
            continue
        if endgame and pos not in must:
            continue
        # Held out only while there are plenty of rounds left; from then on
        # they compete on discounted value rather than being excluded.
        if pos in ("K", "DEF") and rounds_left > 6 and not endgame:
            continue
        out.append(p)
    if out:
        return out
    # last resort: anything that respects caps, else anything at all
    fallback = [p for p in avail if counts.get(p["pos"], 0) < caps.get(p["pos"], 99)]
    return fallback or avail


BENCH_FLOOR = {"QB": 0.18, "TE": 0.22, "K": 0.05, "DEF": 0.05, "RB": 0.50, "WR": 0.45}


def need_multiplier(state, pos, rounds_left):
    """Marginal value of one more player at this position.

    VORP measures what a player is worth *if you start him*. Applied blindly it
    tells you a backup QB is worth +23 points, when in a 1-QB league he will
    score us exactly zero all season. So we weight raw VORP by how likely this
    player is to ever occupy a lineup slot: mandatory starter > FLEX > bench,
    and bench value differs sharply by position (an RB4 gets used when someone
    pulls a hamstring; a K2 never does).
    """
    counts = state.roster_counts()
    c = counts.get(pos, 0)
    slots = state.slots
    base = slots.get(pos, 0)
    flex_cap = slots.get("FLEX", 0) if pos in state.cfg["flex_eligible"] else 0
    startable = base + flex_cap

    if c < base:
        m = 1.0 + 0.13 * (base - c)          # unfilled mandatory starter
    elif c < startable:
        m = 0.85                              # would slot into a FLEX
    else:
        over = c - startable
        m = BENCH_FLOOR.get(pos, 0.4) * (0.7 ** over)

    # urgency: not enough rounds left to cover our remaining holes
    need = mandatory_unfilled(state)
    if sum(need.values()) >= rounds_left and pos in need:
        m *= 1.6
    return m


_BYES_CACHE = {}


def _byes():
    """Team -> bye week, loaded once from the derived 2026 schedule."""
    if not _BYES_CACHE:
        import json
        p = os.path.join(HERE, "data", "schedule_2026.json")
        try:
            with open(p) as f:
                _BYES_CACHE.update(json.load(f).get("byes") or {})
        except Exception:
            _BYES_CACHE["__none__"] = 0
    return _BYES_CACHE


def bye_penalty(state, player):
    """Penalise picks that concentrate our bye exposure.

    The failure this prevents is concrete: draft three RBs who all bye in week
    7 and that week we cannot field a legal lineup, scoring a guaranteed zero
    in those slots. Same-position collisions are far more damaging than
    cross-position ones, because only a same-position body can fill the slot.
    """
    byes = _byes()
    tm = (player.get("team") or "").upper()
    wk = byes.get(tm) or player.get("bye")
    if not wk:
        return 1.0
    pos = player["pos"]
    # K and DEF used to be exempt, which was harmless only while they were
    # drafted last from whatever remained. Now that they compete on merit the
    # exemption is a hole: Cam Little was taken at pick 147 with a week-7 bye,
    # entirely unexamined, making him the FIFTH of our players out in the one
    # week we already could not field. They are starting positions with a single
    # slot each - if the kicker is on bye we score nothing at kicker.

    starters = state.slots.get(pos, 0)
    flex = state.slots.get("FLEX", 0) if pos in state.cfg["flex_eligible"] else 0
    need_live = starters                      # bodies we must field at this pos

    same_pos = sum(1 for p in state.my_roster
                   if p["pos"] == pos
                   and (byes.get((p.get("team") or "").upper()) or p.get("bye")) == wk)
    # Counts K and DEF too. They are starting slots, so a week where our kicker
    # is also out is genuinely worse, and excluding them understated week seven
    # by exactly one body.
    any_pos = sum(1 for p in state.my_roster
                  if p["pos"] in ("QB", "RB", "WR", "TE", "K", "DEF")
                  and (byes.get((p.get("team") or "").upper()) or p.get("bye")) == wk)

    # What matters is how many bodies are LEFT that week, not how many are out.
    # The old test counted players on the bye, so it could not tell a six-WR
    # roster losing two from a three-WR roster losing two. It let Parker
    # Washington through at 0.88 for five points of VORP, leaving us with one
    # startable receiver in week 7 against two WR slots plus flex.
    pos_total = sum(1 for p in state.my_roster if p["pos"] == pos) + 1
    pos_out = same_pos + 1
    remaining = pos_total - pos_out
    shortfall = max(0, need_live - remaining)

    m = 1.0
    # A shortfall only counts as a COLLISION - it needs someone already on the
    # roster sharing that week. Without this guard the first player drafted at
    # any position is always "short", because on his bye we would have nobody
    # there. That is true, unavoidable, and identical for every candidate at the
    # position, so penalising it discriminates nothing while quietly suppressing
    # QB, TE, K and DEF across the whole board. It scored Dak Prescott (bye 14,
    # no overlap with us) exactly like Trevor Lawrence (bye 7, overlapping two
    # of our receivers).
    if same_pos >= 1:
        if shortfall >= 2:
            m *= 0.55             # cannot field the position at all
        elif shortfall == 1:
            m *= 0.72
        else:
            m *= 0.94             # shares a bye but we still have cover
    # Broad roster-wide pileup on one week. Counts the candidate too: the
    # question is what the roster looks like AFTER the pick, and the previous
    # version compared the existing roster only, so it fired a pick late. With
    # McConkey and Washington already on week 7, adding a third body there drew
    # no penalty at all because the count stopped at two.
    # Thresholds stay where they were - shifting them up while also adding the
    # candidate simply cancelled the correction out, and Trevor Lawrence still
    # scored a clean 1.00 for a week that already had two of our receivers on it.
    any_after = any_pos + (1 if pos in ("QB", "RB", "WR", "TE") else 0)
    if any_after >= 4:
        m *= 0.85
    elif any_after == 3:
        m *= 0.93

    # Zeroing out a starting position, on a week that is already thin.
    #
    # The general "first at a position" case must NOT be penalised - every
    # quarterback leaves us without one on his own bye, so it separates nothing.
    # But Lawrence and Purdy had identical VORP, and Lawrence's bye landed on
    # the one week already holding two of our receivers. He won by 0.9 points
    # and guaranteed us zero quarterbacks in week 7. The 0.93 pileup multiplier
    # was not enough to catch it.
    #
    # So the penalty is aimed narrowly at the harm: this pick would leave the
    # position empty that week, AND the week is already crowded with our
    # players. A clean bye week is untouched.
    # Scale with how crowded the week already is. A flat 0.82 was far too weak
    # once several players shared a bye: with five of eleven already out in week
    # seven, Brenton Strange still scored highest and would have been a SIXTH -
    # and a fourth Jacksonville player, since same team always means same bye.
    # Emptying a position on a week that is already gutted has to be close to
    # disqualifying, not a nudge.
    if remaining == 0 and need_live >= 1 and any_pos >= 2:
        m *= max(0.35, 1.0 - 0.12 * any_pos)
    return m


def bye_coverage_bonus(state, player):
    """Reward a bench player who can actually cover a week we cannot field.

    The engine penalised bye COLLISIONS but never rewarded bye COVERAGE, and for
    a backup those are not the same thing. With Kincaid (TE, bye 7) rostered, it
    ranked Brenton Strange - also bye 7 - above T.J. Hockenson on raw value,
    even though Strange leaves us with zero tight ends in week 7 and Hockenson
    fixes it. A backup whose bye matches the starter's is insurance against
    injury only; one with a different bye is insurance against both.

    Only fires when the position would otherwise be EMPTY that week, which is
    what makes it coverage rather than mere depth.
    """
    pos = player["pos"]
    if pos not in ("QB", "RB", "WR", "TE"):
        return 1.0
    byes = _byes()

    def bye_of(p):
        return byes.get((p.get("team") or "").upper()) or p.get("bye")

    mine = [p for p in state.my_roster if p["pos"] == pos]
    if not mine:
        return 1.0
    need = state.slots.get(pos, 0)
    if need < 1:
        return 1.0
    # weeks where every player we hold at this position is out
    theirs = [bye_of(p) for p in mine]
    holes = {w for w in theirs if w and all(b == w for b in theirs)}
    if not holes:
        return 1.0
    cand = bye_of(player)
    return 1.35 if cand not in holes else 1.0


def stack_bonus(state, player):
    """QB + his own pass catcher raises weekly ceiling (correlated scoring)."""
    if player["pos"] not in ("QB", "WR", "TE"):
        return 1.0
    tm = player.get("team")
    if not tm:
        return 1.0
    have_qb = any(p["pos"] == "QB" and p.get("team") == tm for p in state.my_roster)
    have_pc = any(p["pos"] in ("WR", "TE") and p.get("team") == tm for p in state.my_roster)
    if (player["pos"] == "QB" and have_pc) or (player["pos"] in ("WR", "TE") and have_qb):
        return 1.04
    return 1.0


# Playoff schedule is worth a nudge, not a shove. It covers 3 of 17 weeks, and
# only pays off if we reach the postseason at all. Capped at +/-3% of a player's
# score, it breaks ties between comparable players without ever overriding a
# real talent gap: 3% of a VORP-100 player is 3 points.
PLAYOFF_SOS_SWING = 0.03

# Availability. The board applies a flat multiplier per injury designation -
# every "Questionable" is treated identically - which is how Christian McCaffrey
# went at 1.07 on a 0.95 haircut despite having played 75% of his team's games
# over the recency-weighted window, and 4 of 17 in 2024. Combined with the tag
# that is roughly 56% availability, and for a first-round pick availability is
# not a rounding correction: it is most of the value.
#
# Dampened rather than applied in full, because missing a game does not cost the
# whole projection - a bench player starts instead. k=0.5 means a 56%-available
# player takes a 22% haircut, which reorders him behind a comparable durable one
# without erasing him. Floored so no player is ever written off entirely.
AVAIL_WEIGHT = 0.5
AVAIL_FLOOR_MULT = 0.70
AVAIL_TAPER_FLOOR = 0.15      # never taper below this share of full weight
_AVAIL_CACHE = {}


def availability_multiplier(player, round_no=1, rounds=15):
    """Discount by how often this player is actually on the field.

    **Tapered by round.** Availability is most of the value of an early pick,
    because a first-round starter who misses six games cannot be replaced from
    our bench. By round ten we are buying insurance, and a backup's own
    availability barely matters - he is the contingency, not the plan.

    The taper stands on its own logic, not on the pick-50 case I first blamed it
    for. Decomposing that pick showed availability was NOT the deciding factor:
    McLaurin and Odunze scored 0.923 and 0.924 there, essentially identical. What
    actually separated them was the bye penalty - McLaurin is WAS, bye week 7,
    and we already held McConkey on week 7, so he took a 0.88 collision multiplier.
    The engine was right and I misread it. Recorded here because a comment that
    justifies a design with a wrong finding is worse than no comment.

    Deliberately NOT used for weekly lineup scoring, where it could not be shown
    to improve prediction (see MODEL.md). Roster construction is a different
    question: there, games missed are lost outright.
    """
    pid = player.get("pid")
    key = (pid, round_no)
    if key in _AVAIL_CACHE:
        return _AVAIL_CACHE[key]
    m = 1.0
    try:
        import model as _MO
        av, _ = _MO.availability(pid, player.get("injury"))
        if av is not None:
            taper = max(AVAIL_TAPER_FLOOR,
                        1.0 - (round_no - 1) / float(max(1, rounds)))
            w = AVAIL_WEIGHT * taper
            m = max(AVAIL_FLOOR_MULT, 1.0 - w * (1.0 - av))
    except Exception:
        m = 1.0
    _AVAIL_CACHE[key] = m
    return m


def playoff_sos_multiplier(player, mu, sd):
    """Reward an easy fantasy-playoff schedule (weeks 15-17), penalise a hard one.

    `sos_playoffs` runs 0 (weakest defenses faced) to 1 (toughest), so a high
    value is a *harder* schedule for that player's offense and earns a discount.

    Only offensive skill players get this. A defense wants to face a weak
    *offense*, which this metric does not measure - it is built from opposing
    defensive strength - so applying it to DEF would be meaningless at best and
    wrong-signed at worst. Kickers are excluded for the same ambiguity: a stingy
    opposing defense means fewer touchdowns but more field-goal attempts.
    """
    if player["pos"] not in ("QB", "RB", "WR", "TE"):
        return 1.0
    v = player.get("sos_playoffs")
    if v is None or sd <= 0:
        return 1.0
    z = max(-2.0, min(2.0, (v - mu) / sd))
    return 1.0 - PLAYOFF_SOS_SWING * (z / 2.0)


def recommend(state, picks_made, top_n=7, sims=300):
    """Return ranked candidate list for the pick that is on the clock."""
    avail = state.available()
    cur, nxt = state.next_two_picks(picks_made)
    if cur is None:
        cur = picks_made + 1
    picks_between = (nxt - cur) if nxt else 0
    rounds_left = max(1, state.rounds - len(state.my_roster))
    this_round = (cur - 1) // state.teams + 1 if cur else 1

    avail = legal_candidates(state, avail, rounds_left)
    sos_vals = [p["sos_playoffs"] for p in avail if p.get("sos_playoffs") is not None]
    if sos_vals:
        sos_mu = sum(sos_vals) / len(sos_vals)
        sos_sd = (sum((x - sos_mu) ** 2 for x in sos_vals) / len(sos_vals)) ** 0.5
    else:
        sos_mu, sos_sd = 0.5, 0.0
    survival = simulate_survival(state, avail, picks_between, sims=sims)
    exp_next = {pos: expected_best_next(avail, survival, pos)
                for pos in ("QB", "RB", "WR", "TE", "K", "DEF")}

    scored = []
    for p in avail:
        pos = p["pos"]
        # opportunity cost: what this position degrades to if we wait
        dropoff = max(0.0, p["vorp"] - exp_next.get(pos, 0.0))
        base = p["vorp"] + 0.55 * dropoff
        rel = KDEF_RELIABILITY if pos in ("K", "DEF") else 1.0
        mult = (rel
                * need_multiplier(state, pos, rounds_left)
                * bye_penalty(state, p)
                * stack_bonus(state, p)
                * playoff_sos_multiplier(p, sos_mu, sos_sd)
                * availability_multiplier(p, this_round, state.rounds)
                * bye_coverage_bonus(state, p))
        # A discount must always make a pick *worse*. Multiplying a negative
        # VORP by 0.18 would make a replacement-level backup QB look better
        # than a startable flyer, so below replacement we divide instead.
        score = base * mult if base > 0 else base / max(mult, 0.1)
        scored.append({
            "player": p, "score": score, "vorp": p["vorp"],
            "dropoff": dropoff, "survive_next": survival.get(p["pid"], 0.0),
        })
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_n], {"pick": cur, "next_pick": nxt, "round": this_round,
                            "teams": state.teams,
                            "picks_between": picks_between, "exp_next": exp_next}


def explain(cand, meta):
    p = cand["player"]
    s = cand["survive_next"]
    bits = [f"VORP {p['vorp']:.0f}", f"{p['pos']}{p.get('pos_rank','?')}", f"tier {p.get('tier','?')}"]
    if p.get("adp"):
        bits.append(f"ADP {p['adp']:.0f}")
    if meta["next_pick"]:
        bits.append(f"{s*100:.0f}% to survive to pick {meta['next_pick']}")
    if cand["dropoff"] > 12:
        bits.append(f"tier cliff: position drops {cand['dropoff']:.0f} VORP if we wait")
    if p.get("injury"):
        bits.append(f"INJ:{p['injury']}")
    rnd = (meta["pick"] - 1) // meta.get("teams", 14) + 1 if meta.get("pick") else 1
    am = availability_multiplier(p, rnd)
    if am < 0.95:
        bits.append(f"availability discount x{am:.2f}")
    v = p.get("sos_playoffs")
    if v is not None and p["pos"] in ("QB", "RB", "WR", "TE"):
        if v <= 0.35:
            bits.append(f"easy wk15-17 schedule ({v:.2f})")
        elif v >= 0.65:
            bits.append(f"hard wk15-17 schedule ({v:.2f})")
    return " | ".join(bits)
