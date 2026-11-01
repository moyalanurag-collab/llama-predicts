# Data sources: what's available, and the limits

Checked October 2026. Terms and pricing change, so re-check each one before
relying on it.

## Fight statistics

| Source | What it gives | Access | Limits |
|---|---|---|---|
| **UFCStats.com** via the **scrape_ufc_stats CSV export** (github.com/Greco1899/scrape_ufc_stats) | Official UFC stats partner data. Every fight since 1994: results, method, round/time, round-by-round strikes (by target and position), takedowns, submission attempts, control time; fighter height, reach, stance, DOB | Public CSV files on GitHub, refreshed automatically after each event. `python -m ufc_edge update-data` downloads them | Fighters are named, not given ids, so names are matched (spelling variants normalised; namesakes told apart by weight class; unmatched fighters get a placeholder id). Physical data is current-only, not point-in-time. Older fights have thinner stats. |
| **Kaggle "Ultimate UFC Dataset"** (mdabbert) | Pre-built fight-level table with fighter stats, rankings **and odds** (moneyline, plus method props for recent years) | CSV download | Odds are one snapshot per fight with **no timestamp**, so treat them as roughly closing odds. Update frequency is uncertain. Stats columns may be cumulative and include the fight itself. **Check for leakage** before using its features. |
| UFC.com / Tapology / Sherdog | Cards, late changes, fighter records | Public pages | Use to confirm cards and catch replacements, not as a model input. |

**Why not scrape UFCStats directly?** Since October 2026 the site shows
automated visitors a JavaScript "Checking your browser" challenge. Getting
around it would mean bypassing an access control, so this project doesn't.
The direct scraper (`ufc_edge/data/ufcstats.py`) is kept but disabled.

**Upcoming cards** come from the odds feed: The Odds API lists every
upcoming MMA fight with odds, and fighters are matched to the history by
name. Only fights with odds can be bet anyway.

**Plan:** rebuild features from UFCStats per-fight data so every feature is
computed only from fights *before* the one being predicted. That's the
single most important thing for an honest backtest.

## Odds

| Source | What it gives | Access | Limits |
|---|---|---|---|
| **The Odds API** | Live MMA fight-winner (h2h) odds from many bookmakers, with a **UK region**. Historical odds from **June 2020** | REST API. Free key for current odds; **historical needs a paid plan** | Fight-winner market only (no method-of-victory odds). Free-tier request quota is limited. |
| **BestFightOdds.com** | Opening and closing lines plus line movement for most UFC fights, many books | Public pages, no API | Scraping may breach the terms. Use only if the terms allow it, otherwise for manual spot checks. |
| **Kaggle dataset odds** | One price per fighter per fight, back to 2010 | CSV | No timestamp or bookmaker, so it's only usable as a "closing line" benchmark. |
| **Betfair Exchange API** | Live exchange prices and volume, plus historical data (paid) | Free **delayed** app key for development; **live** key needs an activation request | A UK account is needed. Commission is charged on net winnings, and the EV maths already supports a `commission` parameter. |

**Practical v1 path:** use Kaggle odds as an approximate closing line for the
2010–2020s backtest. Use The Odds API (free) to snapshot UK odds before
every event from now on. Every quote is stored with its timestamp and
bookmaker, so paper trading builds its own clean, leakage-free odds
history. Paid historical odds are optional for later.

## Results (for settlement)

UFCStats event pages, cross-checked against UFC.com. A bet settles only on a
result marked `verified`. Draws and no contests settle as `VOID` in
draw-no-bet moneyline markets. Check your bookmaker's rules, because some
settle a draw as a loss on a three-way market.

## News, injuries, weight cuts

There's no reliable structured, timestamped source for these. In v1 they're
**not model inputs**: injecting them by hand would leak hindsight into
backtests. They appear on the dashboard as flagged, sourced context (for
example "short-notice replacement, 9 days"). Short notice, missed weight and
layoff length *can* be derived from structured data and will be features.

## Bet execution

UK fixed-odds bookmakers (Bet365, Sky Bet, William Hill and others) offer
**no public bet-placement API**. Automating their websites would breach
their terms. So v1 is **recommendation-only**:

1. The app proposes bets and the risk engine approves them.
2. You place them by hand at your bookmaker.
3. You record them, and they are settled against verified results.

The Betfair Exchange is the only realistic route to authorised automated
execution, and it's an optional later stage behind its own approval gates.

## What can't be done honestly

- **Line-movement features for older fights.** There are no timestamped
  historical snapshots before you start collecting them (or before 2020
  with paid data).
- **Method-of-victory betting.** Those odds aren't in The Odds API, and the
  margins are bigger. The model will predict method, but the strategy won't
  bet it in v1.
- **Guaranteed edge.** UFC moneylines are fairly efficient by fight time.
  The model has to beat the *market's own probabilities*, not just "pick the
  favourite", before any bet is justified.
