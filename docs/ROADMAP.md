# Roadmap

Each stage finishes with passing tests before the next one starts.

## Stage 1: Core safety layer ✅ (done)
- Double-entry ledger with an automatic profit sweep, the £40 capital boundary and append-only tables
- Independent risk engine: stake, exposure, daily/event loss, bet-count, stale-odds, edge and duplicate checks
- Kill switch, fail-closed integrity halt, exhaustion latch that survives restarts, manual-authorisation flow
- EV, de-vig (multiplicative and power), blended probability, fractional Kelly
- Operator CLI and the Section 11B demo
- 41 tests, including every Section 11E requirement

## Stage 2: Data pipeline ✅ (done)
- UFCStats history from the public CSV export (the site now blocks automated access, and we don't bypass that)
- Kaggle dataset loader as the historical odds benchmark (99.7% matched)
- The Odds API client: UK odds snapshots stored with timestamp and bookmaker
- **Point-in-time feature builder:** every feature uses only fights before the prediction date
- Data-quality flags (missing stats, debutants, short notice)

## Stage 3: Model ✅ (done)
- Baselines: "always the favourite", market de-vigged probability, Elo
- Logistic regression (interpretable), then gradient boosting as the challenger
- Calibration (isotonic or Platt), reliability curves, log-loss and Brier score
- **Pass/fail bar:** the model must match or beat the market's de-vigged
  probabilities on held-out data, or the strategy says "no bets"
- Method-of-victory probabilities (reported, not bet)
- **Result:** stats alone reach 61.7% accuracy vs the market's 66.3%. A walk-forward-fitted blend
  (market weight about 0.96, stats about 0.25) slightly beats the market on log-loss (0.6071 vs 0.6076).
  The flat-stake betting preview is +4.9% over 939 bets, but its 95% range (-1.4% to +11.2%) includes zero.

## Stage 4: Backtest and paper trading ✅ (done)
- **Built:** `strategy.py` (plan + dry run on a ledger copy), `settlement.py` (verified results, VOID for draws/NC, CLV), `backtest.py` (every event through the real risk engine and ledger), `weekly` command.
- **Backtest, £40 from 2014:** your settings, 155 bets, +£12.27; trusting the blended model, 840 bets, +£25.76. Both ranges include zero. Never exhausted.
- **Decision needed first:** the risk engine's `model_weight = 0.5` blend was a placeholder. The
  model now outputs an already market-blended probability. Shrinking it a second time removes
  nearly all bets. The backtest will compare options before any setting changes, and changes
  need manual authorisation.
- Walk-forward backtest: train only on the past, step through events
  chronologically, run the full risk engine and ledger
- Reports: ROI, yield on turnover, max drawdown, losing streaks, closing-line
  value, performance by odds band, year and division, all with sample sizes
- Paper trading: snapshot odds before each card, record proposals, settle them
  for real against verified results. Aim for at least a few months, ideally
  100+ bets, before trusting it.

## Stage 5: API and dashboard ✅ (done)
- Built with the Python standard library (no FastAPI or React needed): a localhost-only server, CSRF header, typed-phrase actions, and hand-drawn SVG charts with the bundled Archivo font.
- Pages: Overview, Fight card, Betting plan (record-a-bet with confirmation), Bankroll (full ledger),
  Performance (model vs market, calibration, backtest, paper record with CLV), Settings (limits, kill switch, audit log)
- Live mode is ready to use (a separate ledger, with bets you place yourself and then record), which covers most of Stage 6

## Stage 6 (optional): Live tracking
- A `live` database, separate from `paper`
- Still recommendation-only: you place bets manually and log them
- Only if paper trading shows a credible edge

## Stage 7 (optional, later): Authorised execution
- Betfair Exchange only, behind its own module, approval gates and a
  separate kill switch, and only after the Stage 1 tests and a long paper
  record
