# Draft strategy

14-team, full PPR, snake, 15 rounds, **our slot 7**.
Picks: 7, 22, 35, 50, 63, 78, 91, 106, 119, 134, 147, 162, 175, 190, 203.
**2 hours per pick** (Sleeper `pick_timer: 7200`) - there is time to recompute
from scratch at every single turn.

Everything below is generated from the engine's own Monte Carlo over this
league's exact settings, not from generic advice.

## The core insight: pick 7 and pick 22 are different problems

Probability a player is still on the board when we pick (4000 sims, ADP noise):

| player | pos | VORP | ADP | @7 | @22 |
|---|---|---:|---:|---:|---:|
| Jahmyr Gibbs | RB | 176 | 1.6 | 11% | 0% |
| Bijan Robinson | RB | 170 | 2.5 | 16% | 0% |
| Puka Nacua | WR | 143 | 4.7 | 31% | 0% |
| Ja'Marr Chase | WR | 142 | 3.0 | 19% | 0% |
| Jaxon Smith-Njigba | WR | 131 | 6.4 | 50% | 0% |
| Amon-Ra St. Brown | WR | 127 | 7.1 | 58% | 0% |
| Christian McCaffrey | RB | 122 | 5.0 | 34% | 0% |
| Jonathan Taylor | RB | 117 | 7.1 | 56% | 0% |
| CeeDee Lamb | WR | 117 | 9.0 | 74% | 0% |
| **Nico Collins** | WR | **109** | 25.3 | **100%** | **80%** |
| James Cook | RB | 106 | 10.1 | 82% | 0% |
| **Brock Bowers** | TE | **98** | 23.6 | **100%** | **71%** |
| **George Pickens** | WR | 92 | 23.6 | 100% | 71% |
| **Josh Allen** | QB | 81 | 22.8 | 100% | 64% |
| **Trey McBride** | TE | 80 | 21.5 | 100% | 55% |

The bolded players are high-VORP **and** cheap in this league's market. They are
still there at 22 the large majority of the time. Taking any of them at 7 burns
roughly 30-70 VORP for nothing.

**Rule for pick 7: take the best player who will NOT survive to 22.** That is
always somebody from the top nine rows above.

## What the engine actually does

Simulating the six picks ahead of us 120 times and asking the engine to pick:

**At 7** - it simply takes best-available from the elite tier, because which of
them falls is the only thing that varies:

    23%  Puka Nacua        17%  Bijan Robinson     11%  Jahmyr Gibbs
    19%  Jaxon Smith-Njigba 14%  Ja'Marr Chase      7%  Christian McCaffrey
                                                    6%  Amon-Ra St. Brown

**At 22** - it harvests the market discount:

    54%  Nico Collins      20%  Derrick Henry      20%  Brock Bowers

Most common openings: `Bijan -> Collins`, `JSN -> Collins`, `Gibbs -> Collins`,
`Nacua -> Collins`, `Nacua -> Henry`, `Bijan -> Bowers`.

## Draft-day rules

1. **Do not reach at 7.** If an elite name fell to us, take it. The engine's
   ordering already accounts for positional scarcity; trust it over gut.
2. **Nico Collins is the target at 22** and is there 80% of the time. If he is
   gone, Henry or Bowers. Do not panic-pivot to a QB.
3. **Josh Allen sits at overall 27 despite projecting the most raw points of any
   player.** Two separate reasons, now both measured rather than argued. First,
   in a 1-QB league replacement level at the position is enormous. Second,
   Sleeper systematically over-projects quarterbacks: summed weekly projections
   came in at 0.920x actual in 2024 and 0.870x in 2025, and correcting for it
   out-of-sample cut QB season-total error by 31.8%. The board now applies a
   0.90 factor, which drops Allen from 22nd to 27th and his VORP from 81 to 73.
   He is 64% to reach pick 22, so he is a *fallback*, not a plan - and slightly
   less of a fallback than he looked yesterday.
4. **Week 6 is the real landmine, not week 11.** Earlier notes flagged week 11
   because six *teams* bye together, but the number that matters is how many
   players we would actually want are affected. Measured against the top 40:

       week  6:  7 - Gibbs, Chase, St. Brown, Achane, Chase Brown, Jefferson, Higgins
       week 13:  6 - Taylor, Bowers, Henry, G.Wilson, Jeanty, Flowers
       week  7:  6 - Cook, Hampton, McConkey, Allen, McLaurin, P.Washington
       week 11:  5 - Bijan, Nacua, Smith-Njigba, London, A.J. Brown
       week  8:  5 - McCaffrey, Collins, Nabers, Olave, Evans

   Week 6 is worse because it is concentrated at the very top of the board, and
   **Jahmyr Gibbs - our most likely pick at 7 - byes in week 6**. If we take him,
   avoid Chase, St. Brown, Jefferson, Achane and Chase Brown at 22.

   The happy accident: the engine's favourite opening, Gibbs (week 6) into Nico
   Collins (week 8), has no collision at all. Bowers and Henry (week 13) are
   equally clean behind him.

   `draft.py` already penalises this in both forms - a same-position collision
   multiplies the score by 0.72, and four or more starters sharing any bye week
   multiplies by 0.85 - so the engine will steer around it on its own. The manual
   watch is only for the case where we deliberately override it.
5. **K and DEF last.** The endgame rule forces them in; never take one early.

## Real-world context the projections may not price in

Checked 2026-08-26; Sleeper's team assignments were verified correct against
reporting (A.J. Brown to NE, Kenneth Walker to KC, Pickens franchise-tagged in DAL).

- **Puka Nacua** - groin soreness, out of practice into preseason week 3, but
  expected to be ready for week 1. Separately he is under investigation for an
  off-field incident that could carry a suspension. That risk is *not* in the
  projections. Treat the 31%-to-reach-7 as a genuine coin flip on whether we
  want him at all.
- **Christian McCaffrey** - age-30 back, missed camp time with "tightness". Same
  word preceded a week-1 IR stint in 2024 (returned week 10) and a fully healthy
  2025. Highest variance name in the top ten.
- **Ja'Marr Chase** - Sleeper flags Questionable; no corroborating injury
  reporting found. Likely a stale tag.
- **Nico Collins** - three straight WR1 seasons on points-per-game (WR9, WR8,
  WR7). The ADP discount is about C.J. Stroud's ceiling and a minor-injury
  history, not about Collins' role. This is why he is the pick at 22.

**Refresh `python3 engine/fetch.py` Friday morning** - injury designations move
right up to kickoff and the board is only as current as that fetch.

Sources: [FantasyPros injuries](https://www.fantasypros.com/2026/08/16-fantasy-football-injuries-to-monitor-2026/),
[RotoBaller on Nacua](https://www.rotoballer.com/will-puka-nacua-play-in-week-1-fantasy-football-injury-update-2026/1907671),
[Yahoo on Collins](https://sports.yahoo.com/articles/nico-collins-fantasy-outlook-2026-113538074.html),
[SI on new teams](https://www.si.com/onsi/fantasy/news/a-j-brown-and-3-more-fantasy-football-playmakers-with-new-teams-and-new-roles),
[Footballguys offseason recap](https://www.footballguys.com/article/2026-welcome-back-to-fantasy-football-offseason-recap)
