# Model evaluation report

Walk-forward test, 2012–2026: each year is predicted by models trained only on earlier years.
Features use only information from before each fight. Odds are approximate closing lines from the
Kaggle dataset (no timestamp or bookmaker), so treat every betting number here as indicative.

## Headline

- The blended model is **slightly better than the market** on log-loss: 0.6071 vs 0.6076 (lower is better; a coin flip scores 0.6931).
- Learned blend weights (most recent year): market 0.96, stats model 0.25. The market carries most of the information; the stats model adds a little.
- Betting preview (flat £1 stakes, min edge 3%, odds 1.20–4.00): 940 bets from 5672 fights, ROI +5.3% (95% range -1.3% to +11.7%).
- **This is not proof of an edge.** The range includes zero, so luck can't be ruled out.
- For contrast, betting on the stats model *without* the market blend: 4316 bets, ROI -1.6%. Trusting the model too much loses money.

## Accuracy against the market

Fights that have market odds:

| model | fights | log_loss | brier | accuracy |
|---|---|---|---|---|
| Market (bookmaker odds, margin removed) | 5,672 | 0.6076 | 0.2104 | 66.3% |
| Elo rating only | 5,672 | 0.6839 | 0.2454 | 55.9% |
| Logistic regression (stats) | 5,672 | 0.6564 | 0.2320 | 61.7% |
| Gradient boosting (stats) | 5,672 | 0.6561 | 0.2320 | 61.7% |
| **Final: stats blended with market** | 5,672 | 0.6071 | 0.2101 | 66.6% |

All fights (stats models only; includes fights with no odds):

| model | fights | log_loss | brier | accuracy |
|---|---|---|---|---|
| Elo rating only | 7,011 | 0.6841 | 0.2455 | 55.8% |
| Logistic regression (stats) | 7,011 | 0.6576 | 0.2324 | 61.8% |
| Gradient boosting (stats) | 7,011 | 0.6566 | 0.2323 | 61.6% |

## Calibration of the final model

When the model says the favourite wins X% of the time, how often did they?

| bin | fights | predicted | actual |
|---|---|---|---|
| (0.499, 0.55] | 822 | 52.5% | 52.6% |
| (0.55, 0.6] | 933 | 57.5% | 57.7% |
| (0.6, 0.65] | 940 | 62.5% | 59.8% |
| (0.65, 0.7] | 858 | 67.4% | 67.1% |
| (0.7, 0.75] | 783 | 72.4% | 72.0% |
| (0.75, 0.8] | 571 | 77.4% | 76.2% |
| (0.8, 0.9] | 659 | 84.2% | 87.1% |
| (0.9, 1.0] | 106 | 91.8% | 91.5% |

## Betting preview by period

| period | bets | roi |
|---|---|---|
| 2014-16 | 434 | +4.7% |
| 2017-19 | 290 | +4.9% |
| 2020-22 | 115 | +7.2% |
| 2023+ | 101 | +7.4% |

## Method of victory

Six outcomes (each fighter by KO/TKO, submission or decision), 6,993 fights: log-loss 1.662 vs 1.727 for historical base rates. It's reported for context only; method markets carry bigger margins and aren't bet in v1.

## Saved model

Trained 2026-10-27 16:01 UTC on 8,806 fights up to 2026-10-24.

Past results don't guarantee future performance. Backtests can overstate edges, so
paper trading against real, timestamped UK odds is the real test.
