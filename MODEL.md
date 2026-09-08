# The weighting model - what survived testing

Built 2026-08-26 on **85,109 game logs (2019-2025)** and **14,314 historical
weekly projections (2024-2025)**, all crawled from Sleeper into
`data/statking.db`.

Every idea below was backtested before it was believed. Most of them failed.
This document keeps the failures because they are the more useful half: they
are the adjustments that *feel* obviously right and demonstrably are not.

## Method

Prediction target: a player's actual PPR points in a given week. Metric: mean
absolute error over every qualifying player-week. Baseline: the player's own
season-to-date average. Anything claiming to help has to beat that.

Two rules that decide most of the outcomes:

- **Out-of-sample or it doesn't count.** A correction fitted and evaluated on
  the same season will always flatter itself.
- **Walk-forward.** Predicting week W uses only weeks before W. Nothing gets to
  see its own answer.

## Rejected

| idea | result | why it fails |
|---|---|---|
| Prior-season defense-vs-position | **-0.03% to -1.07%** across 2023/24/25 | Defensive quality does not carry year to year. Roster and scheme turnover mean last season's ranking is closer to noise than signal. |
| Same-season walk-forward matchup | **-0.61% to +0.31%** (null) | Even using only what a defense has conceded *this* year, adjusting for it does not beat leaving it alone. |
| Positional bias correction | **-1.27%** out-of-sample | Sleeper over-projects QBs (+1.08 in 2024, +2.89 in 2025) and under-projects TEs. Both signs are stable - but correcting by the measured mean makes MAE *worse*, because fantasy scoring is right-skewed and MAE is minimised by the median, not the mean. |
| Durability weighting of projections | **+2.21% (2025), -0.01% (2024)** | Does not replicate. The test is also weak by construction: it can only see players Sleeper bothered to project, so genuinely injured players are largely absent from the sample. |

The matchup result deserves emphasis, because it is the one everybody assumes.
A cap sweep settles it:

    cap +/- 5%    MAE 5.1550   +0.15%
    cap +/-10%    MAE 5.1554   +0.15%
    cap +/-18%    MAE 5.1673   -0.08%
    cap +/-25%    MAE 5.1920   -0.56%
    cap +/-35%    MAE 5.2533   -1.75%

Error rises monotonically with the size of the adjustment. The optimum is at
zero. `MATCHUP_CAP` is therefore `0.0`: the numbers are still computed and
still shown, because "Dallas concedes 36.1 PPR/gm to receivers" is worth a
human's attention, but they no longer move a projection.

| Vegas implied team total | **beta=0 optimal (2024), +0.26% (2025)** | The market's implied team total adds nothing on top of Sleeper's projection - because Sleeper has already priced it in. Informative rather than disappointing: it says the projections are close to efficient. |
| Variance-aware lineups (buy volatility when projected to lose) | **null, all abs(z) < 1** | Simulated 90 twelve-team leagues on real 2025 outcomes, half the managers using each rule, across a grid of tilt and trigger settings. The only consistent direction was *negative* at high tilt: distorting a lineup away from the best players costs more than the variance is worth. |

| Preseason production | **r flips sign: -0.100 (2024), +0.126 (2025)** | Tested as a predictor of the projection's residual across ~200 player-seasons each year. 2025 looked promising (QB +0.45, WR +0.37) and 2024 did not replicate any of it. Two years, opposite signs, is not a signal. |

Preseason is still **stored and shown**, for one reason that needs no predictive
claim: nine of the top 200 on our board have no regular-season record at all, and
for a rookie the exhibition games are the only evidence that exists. Jeremiyah
Love (RB ARI, board rank 49, ADP 26.9) put up 10.2 PPR on 24 snaps. That is worth
seeing before spending a pick on him; it is not worth a coefficient. It lives in
its own table so nothing downstream can mistake an exhibition snap for a real
one - `durability()` and the season-to-date average would both be wrong if it
landed in `actual`.

## Re-tested 2026-09-08, with better data and better questions

The rejections above were challenged - reasonably - on the grounds that they
might be stale. Three things had changed since they were measured:

- **The scoring was wrong.** Those tests ran on Sleeper's generic `pts_ppr`,
  which prices passing yards at 0.05 against this league's 0.04. Every
  quarterback residual in the original sample was about two points out.
- **There was no usage data.** Target share, air-yards share, carry share and
  red-zone looks did not exist in the store until the nflverse play-by-play was
  ingested. They had never been tested at all.
- **The metric was MAE**, which answers the wrong question twice. A lineup does
  not need an accurate number, it needs the right *ordering*. A published
  probability does not need an accurate number either, it needs *calibration*.

So `engine/signals.py` re-ran the whole question: ten candidate signals, fitted
on 2024 and graded blind on 2025, measured three ways - MAE, within-position-week
rank correlation, and Brier skill through the calibration model. Two functional
forms were tried, because the first one was the wrong estimator.

**Nothing earned its place.** Best out-of-sample gains, linear form:

| signal | d MAE | d rank | d Brier skill |
|---|---|---|---|
| usage carry share | +0.0086 | +0.0013 | +0.0009 |
| vegas implied total | +0.0059 | +0.0018 | +0.0014 |
| stale vs recent points | +0.0045 | -0.0016 | +0.0006 |
| defence vs position | +0.0028 | -0.0022 | -0.0001 |

Against a baseline MAE of 5.28 and rank correlation of 0.447, these are noise.

### The near-miss worth recording

A decile view of 2025 looked, briefly, like a real find: bucket players by their
team's Vegas implied total and mean residual ratio climbs monotonically from
**0.75x in the bottom decile to 0.98x in the top**. That is a 30% relative
spread, and the linear fit had hidden it - OLS on a heavy-tailed ratio returns a
slope dominated by a handful of 3x weeks, which is why the coefficient came back
at 0.013 and looked worthless.

Fitting deciles on 2024 and applying them to 2025 made everything **worse**
(`d rank -0.0141`, `d skill -0.0044`). The reason is visible when the two
seasons are put side by side:

    vegas decile, mean residual ratio
    2024:  0.90 0.83 0.94 0.97 0.93 1.00 0.82 0.91 0.86 0.98
    2025:  0.75 0.88 0.84 0.82 0.89 0.87 0.91 0.93 0.86 0.98
    correlation between the two curves: +0.051

The 2025 pattern is not in 2024. Correlations between the two seasons' decile
curves are +0.05 for the market, +0.12 for recent form and +0.12 for target
share - which is to say, none of them replicate. This is the same failure mode
as every other rejected idea in this document, caught only because the test was
run out of sample.

### Why this keeps happening

The pattern across a dozen failed signals now points somewhere specific:
**Sleeper's projection has already priced them in.** Target share, red-zone
role, opponent quality and the betting market are exactly what a professional
projection is built from. What is left over after that work is residual, and the
residual is close to noise with respect to the same inputs.

That is a reason to keep showing these numbers to a human - "Chicago concedes
36.1 PPR/gm to receivers" is worth knowing - and a reason never to let them move
a projection. `engine/signals.py` stays in the repo so any future candidate gets
the same treatment rather than an argument.

## The finding that matters

Six weighting ideas were tested and six failed. Then the boring one was measured:

    Availability hygiene - 90 simulated leagues on 2025 actuals

    naive   (starts players who have no game) : 0.4357 win rate, 117.9 pts/wk
    careful (benches them)                    : 0.5641 win rate, 128.2 pts/wk

    difference: +0.1283 win rate  (z +17.4)   +10.3 points per week
             -> +2.18 wins per 17-week season

**That is the whole edge.** Not matchups, not the market, not variance games -
simply never starting a player who has no game. It is worth more than two wins
a season, and it dwarfs every clever adjustment in this document by an order of
magnitude.

It is also the one thing that requires no prediction at all. Byes and inactives
are facts, knowable in advance, and the entire value proposition is checking
them every week without fail. That is what `lineup.py` and the twice-daily cron
exist to do, and it is why an empty starter slot is escalated as a waiver
problem rather than quietly filled with a zero.

## Kept

**`composite = 0.8 x Sleeper's weekly projection + 0.2 x season-to-date average`**

That is the entire model. It is the only thing that beat the baseline in both
graded seasons:

| | 2024 | 2025 |
|---|---|---|
| season-to-date average alone | 5.131 | 5.164 |
| Sleeper projection alone | 4.849 | 5.025 |
| **blend at alpha 0.8** | **4.841** | **4.983** |

The blend curve is smooth and unimodal in both years, with the minimum at
alpha 0.9 (2024) and 0.7 (2025). 0.8 sits inside both and beats pure projection
in both, which is what "robust" means here rather than squeezing the last
0.03%.

Before week 5 there is no season-to-date mean worth blending, and
`composite()` falls back to the projection alone.

## Not modelled, because it is not a model

Bye weeks and Out/IR/PUP/Suspended designations are handled as **hard zeroes**
in `lineup.py`, not as probabilities. A player on bye scores zero as a matter
of fact, and a fact does not need a coefficient. Those players are removed from
the selection pool entirely, so an empty slot surfaces as a waiver problem
rather than being quietly filled with a zero.

## Kept as context, not as coefficients

`model.durability()` measures the share of his team's games a player has
actually played, weighted toward recent seasons. It could not be shown to
improve weekly prediction, so it does not touch projections. It is still worth
reading before committing a roster spot:

    Derrick Henry        97.3% durable
    Nico Collins         82.3%
    Christian McCaffrey  75.2%  -> 56.4% availability with a Questionable tag

That gap is real, it is invisible in a season projection, and it is exactly the
kind of thing to weigh at a draft or in a trade even though it does not belong
in a weekly point estimate.

## The one change this made to the draft

The weekly bias correction failed (above) because MAE is minimised by the
median and weekly scoring is right-skewed. **Season totals are a different
question**, and VORP is built on season totals, where means are exactly what
matter. That test had never been run, so it was.

Comparing summed weekly projections against summed actuals, by position:

| pos | 2024 actual/proj | 2025 actual/proj | stable? |
|---|---:|---:|---|
| **QB** | 0.920 | 0.870 | **yes** |
| RB | 1.105 | 1.000 | no |
| WR | 1.111 | 1.032 | no |
| TE | 1.219 | 1.156 | no |
| K | 1.073 | 1.003 | no |
| **DEF** | 1.065 | 1.084 | **yes** |

Fitting on 2024 and applying blind to 2025 confirms which of those are real:

    QB    season-total MAE 38.7 -> 26.4   +31.8%
    DEF                  20.7 -> 19.7    +4.7%
    RB                   16.6 -> 21.5   -29.5%
    WR                   20.4 -> 23.5   -15.5%
    TE                   17.9 -> 19.2    -7.4%
    K                    17.2 -> 19.4   -12.6%

So `value.py` now applies `CALIBRATION = {"QB": 0.90, "DEF": 1.07}` and nothing
else. RB, WR, TE and K all looked correctable on one year and were not - their
factors collapsed toward 1.0 the following season. Leaving them alone is the
finding, not an omission.

An estimator note worth recording: the first attempt used the mean of per-player
ratios and produced nonsense (TE x1.327), because that statistic is dominated by
players with tiny projections. The ratio of sums is the right estimator and gave
x1.219 for the same data.

Effect on the board: Josh Allen falls from overall 22 to 27 (VORP 81 -> 73),
Lamar Jackson 51 -> 55, and every non-QB moves up a place. It reinforces the
existing "never reach for a quarterback" rule with a measured number rather than
an argument.

`simulate.py` still passes (76% first, 95% top-3), but that number cannot
validate this change either: the simulator scores teams with the same
projections it drafts from, so a calibration applied to both sides cancels out.
The validation is the out-of-sample season-total test above.

## Should a good kicker or defence be drafted early?

No, and the reason is not the one usually given. The spread is real - it is the
*predictability* that fails.

| | gap #1 to #12 | per week | preseason rank vs actual finish |
|---|---:|---:|---|
| K 2024 | 55 pts | 3.2 | **rho +0.01** |
| K 2025 | 57 pts | 3.4 | rho +0.25 |
| DEF 2024 | 57 pts | 3.4 | rho +0.18 |
| DEF 2025 | 57 pts | 3.4 | rho +0.37 |
| TE 2024 | 117 pts | 6.9 | rho +0.62 |
| TE 2025 | 151 pts | 8.9 | rho +0.71 |

Finishing as the top kicker rather than the twelfth is worth 3.4 points a week,
which is not nothing. But a preseason kicker ranking predicts the actual finish
at rho +0.01 in 2024 - literally uninformative - and +0.25 in 2025. Spending a
pick to secure "a good kicker" buys a coin flip at a real price.

Tight end is the contrast that proves the point: twice the spread AND rho +0.62
to +0.71. That position rewards a real pick, which is why the engine takes one
and why losing Andrews at pick 102 mattered.

So `legal_candidates` keeping K and DEF out until the final two rounds is
correct, and it is now correct for a measured reason rather than convention.

The actionable corollary: since the spread is large but unforecastable in August,
the value at those positions lives in **week-to-week streaming**, which is a
waiver activity. Defence carries slightly more signal than kicker (+0.37 against
+0.25), so if either is ever taken early it should be the defence.

## What would move the needle next

The honest ranking of remaining ideas, given what failed:

1. **Usage and role**, not matchup. Snap share and target share are in the
   store (`off_snp`) and change faster than projections update. Untested here.
2. **Vegas lines** - team implied totals are the strongest public predictor of
   fantasy scoring and are not in this dataset at all.
3. **Ceiling vs floor selection.** MAE rewards predicting the median; winning a
   given week sometimes calls for variance. A start/sit rule conditioned on
   whether we are favoured is a different objective than accuracy.
