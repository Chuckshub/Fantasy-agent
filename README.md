# fantasy-agent

An autonomous fantasy football manager. It sets your lineup, works the waiver
wire, publishes calibrated probabilities before each week, and grades itself
afterwards.

It is not a projection site. There are plenty of those, and their numbers are
good. This is the layer above them: the part that reads the projections every
week without fail, notices that your tight end is on bye, makes the change
before kickoff, and then keeps score of how often it was right.

**Works with Sleeper and ESPN, and can learn a platform it has never seen** by
watching your own browser — see [Adding a platform](#adding-a-platform).

---

## The one number that matters

Every clever weighting idea in this project was backtested, and almost all of
them failed. The full record is in [MODEL.md](MODEL.md), including a dozen
ideas that feel obviously right and demonstrably are not — defence-vs-position,
the betting market, target share, variance-chasing, preseason production.

What survived was boring:

```
Availability hygiene — 90 simulated leagues on real outcomes

  naive   (starts players who have no game) : 0.4357 win rate, 117.9 pts/wk
  careful (benches them)                    : 0.5641 win rate, 128.2 pts/wk

  difference: +0.128 win rate  ->  +2.18 wins per season
```

Never starting a player who has no game is worth more than every projection
refinement combined. It requires no prediction at all — byes and inactives are
facts, knowable in advance. It just requires *checking, every week, without
fail*, and then actually making the change.

That is what this is for. The agent doesn't email you a recommendation; it sets
the lineup itself and verifies the result against the platform's own API.

## Calibrated forecasts, and the scoreboard for them

A projection cannot be graded. Whatever a player scores, "13.2" was close, or
unlucky. So the agent also publishes **probabilities**, before kickoff, written
down with a timestamp — and then grades them.

Out of sample (fitted on one season, graded blind on the next, 23,531
propositions):

```
Brier score        0.1255   (0.25 = a coin flip)
Climatology        0.1865   (always predict the base rate)
Brier skill        +0.327
reliability        0.0005    resolution 0.0609
```

Brier alone means little — a set of easy questions scores well however lazily
you answer — so it is always reported against climatology and as a skill score,
with the Murphy decomposition, because a model can be perfectly calibrated and
useless. `engine/grade.py` renders the reliability diagram.

## What it actually does

| mode | when | what happens |
|---|---|---|
| `--cycle` | every 4h | refresh, re-sync, **fix the lineup**, scan waivers and trades |
| `--brief` | daily | who is starting, who is benched, why, and where the edge is |
| `--pregame` | before each kickoff window | availability check, then **sets the lineup** |
| `--rebalance` | after each slate | a player who left injured must not still be starting |
| `--depth` | twice a week | finds weeks you cannot field a legal lineup, and **signs someone** |
| `--forecast` | before the week | publishes probabilities for every team in the league |
| `--grade` | after the week | Brier, reliability plot, and a recap written by a local LLM |

Every mode that can change your roster verifies the result against the
platform's own API afterwards, because a page can render a change that never
committed.

## Install

```bash
git clone <this repo> && cd fantasy-agent
python3 -m venv .venv && .venv/bin/pip install duckdb numpy matplotlib
python3 engine/setup.py          # asks which platform, reads your league
```

`setup.py` reads roster slots, scoring rules, team count and flex eligibility
from your league rather than asking you to type them. That is deliberate: a
hand-typed scoring table is a silent source of wrong answers for a whole
season, and this project has already been bitten once by scoring that looked
right and was not.

Optional, for the weekly recap: [Ollama](https://ollama.com) with a 7B model.
Nothing leaves your machine and nothing is billed.

## Adding a platform

No major fantasy host offers a write API. Setting a lineup or making a claim
means driving the real site in a browser you are logged into — and that is the
only part that differs between platforms. The technique is identical; only the
*selectors* change.

So they are learned, not hand-written:

```bash
./run_chrome.sh                  # a browser the agent can attach to
# log into your platform in that window, open your team page

python3 engine/explore.py --platform espn \
    --url 'https://fantasy.espn.com/football/team?leagueId=...' \
    --players 'Player One,Player Two,Player Three'
```

**Nothing is guessed.** You give it a few players you know are on your roster;
it finds them on the rendered page, walks up from each match to work out the
repeating row structure, and identifies the name and lineup-slot columns by
checking candidates against what it already knows — a slot column has to
contain things like `QB`, `FLEX`, `BN`, not numbers that merely look similar.
It prints what it learned and saves nothing until you pass `--save`.

The exploration pass **only reads**. Before it starts it disables every control
whose text looks destructive — drop, release, trade, propose, commissioner,
start draft — and forces `window.confirm` to refuse, so a stray click cannot
commit anything.

It was validated by pointing it at Sleeper, whose DOM mapping was originally
written by hand, and checking that it independently rediscovered all three
selectors.

## Layout

```
engine/
  platforms/       adapters: sleeper, espn, generic + learned DOM profiles
  setup.py         first-run wizard
  explore.py       learns a platform from your browser
  agent.py         the operator - all the modes in the table above
  lineup.py        weekly optimiser, effective points, bye/injury handling
  setlineup.py     applies the lineup and verifies it
  claim.py         waiver claims and free-agent adds
  depth.py         finds unfillable weeks ahead and fixes them
  forecast.py      publishes probabilities before kickoff
  grade.py         Brier, skill, Murphy decomposition, reliability plot
  calib.py         fits the outcome distribution from history
  signals.py       tests whether a candidate signal earns its place
  nflverse.py      play-by-play into DuckDB
  recap.py         weekly recap, written locally
MODEL.md           every idea tested, including the ones that failed
```

## A note on the failures

[MODEL.md](MODEL.md) keeps the rejected ideas, because they are the more useful
half. The most instructive is recent: a signal that looked strongly predictive
in one season — a clean monotone relationship across deciles — and turned out
to correlate **+0.05** with the same measurement in the previous season. It was
noise, and it would have shipped if the test had not been run out of sample.

`engine/signals.py` exists so the next candidate gets a test rather than an
argument.

## Licence

MIT. No warranty; it manages a real roster, so read what it does before you let
it loose on yours.
