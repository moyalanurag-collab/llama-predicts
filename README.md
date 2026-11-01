# Llama Predicts (UFC Edge)

A UFC fight-prediction and value-betting system built around strict
bankroll protection. It estimates fight probabilities, compares them with
bookmaker odds to find bets with positive expected value, and manages a
fixed £40 bankroll. Realised profits are locked away automatically, and the
system stops permanently when the bankroll is exhausted.

> **Status: Stages 1–5 of 7 done.** The ledger, risk engine, data pipeline,
> prediction model, backtest, paper trading and dashboard are built and tested
> (see [docs/ROADMAP.md](docs/ROADMAP.md)). The system is
> **recommendation-only**. It never places real bets, and profits are not
> guaranteed.

## Results so far

Walk-forward test on 5,672 UFC fights with odds (2012–2026), each year predicted
by a model trained only on earlier years:

| Model | Log-loss (lower is better) | Accuracy |
|---|---:|---:|
| Bookmaker odds (margin removed) | 0.6076 | 66.3% |
| Stats model alone (gradient boosting) | 0.6561 | 61.7% |
| **Stats model blended with market** | **0.6071** | **66.6%** |

The blend matches or slightly beats the market, but the betting edge is not
proven: the 95% range on simulated ROI includes zero. Full report:
[docs/results/model_report.md](docs/results/model_report.md).

**Tech:** Python, pandas, scikit-learn, SQLite, a vanilla-JS dashboard, and a
`unittest` suite covering the ledger, risk rules, features and model.

## Everyday use (Windows)

| Double-click | What it does |
|---|---|
| `Run Fight Week.bat` | Updates results, settles finished bets, takes an odds snapshot, retrains if needed, records paper bets, then opens the dashboard |
| `Open Dashboard.bat` | Opens the dashboard in your browser |

The dashboard runs on your own computer only (`http://127.0.0.1:8765`) and has
six pages: **Overview** (status and the £40 capital boundary), **Fight card**
(model vs market for each fight), **Betting plan** (proposals checked by the
risk engine), **Bankroll** (the full ledger), **Performance** (model accuracy,
backtest and paper record) and **Settings** (limits, kill switch and audit log).

## Quick start (Windows, macOS or Linux)

You need Python 3.10 or newer. The ledger and risk engine use only the
standard library. The data pipeline and model need three packages:

```bash
python -m pip install -r requirements.txt
```

```bash
git clone https://github.com/moyalanurag-collab/llama-predicts.git
cd llama-predicts

# Run the test suite
python -m unittest discover -s tests -v

# See the profit-protection rules work on a throwaway database
python -m ufc_edge demo

# Create your paper-trading bankroll (asks you to type a confirmation)
python -m ufc_edge init --bankroll 40
python -m ufc_edge status
```

Fight data (Stage 2):

```bash
python -m ufc_edge update-data       # download the latest UFC fight history (~10 MB, under a minute)
python -m ufc_edge load-odds         # historical odds from Kaggle's ufc-master.csv (put it in data/raw/)
python -m ufc_edge build-features    # write data/features.csv
python -m ufc_edge odds              # snapshot current UK odds (needs a free key in .env)
python -m ufc_edge data-summary
```

Model (Stage 3):

```bash
python -m ufc_edge train      # walk-forward test vs the market, then train and save (~1-2 min)
python -m ufc_edge predict    # predictions for upcoming fights from the latest odds snapshot
```

`train` writes `data/reports/model_report.md`. `predict` writes
`data/reports/upcoming_predictions.csv`, which includes method-of-victory
probabilities and each fight's main factors.

Backtest, paper trading and dashboard (Stages 4–5):

```bash
python -m ufc_edge backtest    # replay 2014-2026 through the real risk engine and ledger
python -m ufc_edge weekly      # the whole fight-week routine in one command
python -m ufc_edge dashboard   # open the dashboard
```

Other operator commands: `kill --reason "..."`, `release-kill`,
`new-bankroll --deposit 40`, `verify`. All of them default to `--mode paper`.

## How the money rules work

| Event | Active bankroll | Protected profit |
|---|---:|---:|
| Initial deposit | £40 | £0 |
| Win: funds reach £50 | £40 | £10 |
| £5 net loss | £35 | £10 |
| £10 net profit | £40 | £15 |
| £15 net loss | £25 | £15 |

`python -m ufc_edge demo` reproduces this table from real ledger
transactions.

- Any realised profit above £40 is moved to **protected** as soon as a bet
  settles, and can never be staked.
- Open bets count towards the £40, so no more than £40 of your money can
  ever be at risk.
- Unsettled winnings are never counted.
- Once the bankroll is too small for a legal bet (about £2 with default
  limits) and nothing is open, the system latches to **BANKROLL EXHAUSTED —
  BETTING DISABLED**. Only a typed manual authorisation clears it. Restarting
  the app does not.

## Risk limits (defaults)

| Setting | Default |
|---|---|
| Stake sizing | ¼ Kelly on a 50/50 blend of model and market probability |
| Minimum edge | 3% expected value per £1 |
| Max stake | 5% of capital in play |
| Max exposure | 15% per event, 25% total |
| Max worst-case loss | 20% of bankroll per 24h, 15% per event |
| Max bets per event | 4 |
| Odds | 1.20–4.00, no older than 15 minutes |

These were set from general bankroll-management principles, not tuned on
past results. Changing them needs a manual authorisation.

## Data credits

Fight statistics are from [UFCStats.com](http://ufcstats.com), via the CSV export
maintained at [Greco1899/scrape_ufc_stats](https://github.com/Greco1899/scrape_ufc_stats).

## Docs

- [Architecture, schema and risk rules](docs/ARCHITECTURE.md)
- [Data sources and their limitations](docs/DATA_SOURCES.md)
- [Roadmap](docs/ROADMAP.md)

## Component status

| Component | Status |
|---|---|
| Ledger, profit protection, kill switch, exhaustion latch | ✅ Working, tested |
| Risk engine | ✅ Working, tested |
| EV / de-vig / staking maths | ✅ Working, tested |
| Operator CLI | ✅ Working |
| Fight history (UFCStats data, 1994 onwards) | ✅ Working: about 9,000 fights loaded from the public CSV export |
| Point-in-time features | ✅ Working, with leakage tests |
| Historical odds (Kaggle, 2010 to March 2026) | ✅ Working: 99.7% of fights with odds matched |
| Odds feed (The Odds API) | ✅ Built and tested; needs your free API key |
| Prediction model | ✅ Stats model + market blend + method model; walk-forward tested. Matches or slightly beats the market on log-loss; the edge is unproven |
| Backtest (real risk engine + ledger) | ✅ Your settings: 155 bets 2014–26, +11.2% on stakes (range −4.9% to +28.5%); trusting the blend: 840 bets, +4.2% (−2.0% to +10.7%). Not proven |
| Paper trading and settlement | ✅ Automatic in `weekly`, with closing-line value |
| Dashboard | ✅ Local web app, light and dark, no extra installs |
| Automated bet placement | ❌ Not possible with UK bookmakers (no public API). Recommendation-only by design |

## Data not included

Downloaded fight data, odds snapshots, trained models and betting databases live
in `data/`, which is git-ignored. Run `update-data`, `load-odds` and `train`
(above) to rebuild them. Your Odds API key goes in `.env` (copy `.env.example`);
it is never committed.

## Responsible gambling

This is a statistics project, not a way to make money. Most betting
strategies lose to the bookmaker's margin. Only stake what you can afford
to lose. If gambling stops being fun, [BeGambleAware](https://www.begambleaware.org)
and GamCare (0808 8020 133) offer free, confidential support.

## Licence

Copyright © 2026 Anurag Moyal. All rights reserved. The code is public to view
as a portfolio project; please ask before reusing it.
