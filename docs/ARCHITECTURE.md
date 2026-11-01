# Architecture

## Principle

**The model recommends. The risk engine decides. The ledger records.**
Nothing the prediction side produces can move money or bypass a limit. Every
bankroll rule is enforced by deterministic Python code, by database
constraints and triggers, and by automated tests.

```
 ┌────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │ Data        │──▶│ Features &   │──▶│ Strategy     │──▶│ RISK ENGINE  │──┐
 │ collection  │   │ model        │   │ (EV, staking)│   │ (risk.py)    │  │
 └────────────┘   └──────────────┘   └──────────────┘   └──────────────┘  │
   stats, odds,     calibrated          proposes a bet     approves or      │
   results          probabilities                          rejects, with    │
   (timestamped)                                           reason codes     ▼
                                                                    ┌──────────────┐
 ┌────────────┐   ┌──────────────┐   ┌──────────────┐               │ LEDGER       │
 │ Dashboard   │◀──│ FastAPI      │◀──│ Settlement   │──────────────▶│ (ledger.py)  │
 │ (React)     │   │ (read-mostly)│   │ (verified    │               │ SQLite,      │
 └────────────┘   └──────────────┘   │  results)    │               │ append-only  │
                                       └──────────────┘               └──────────────┘
```

## Technology choices

| Layer | Choice | Why |
|---|---|---|
| Core, ledger, risk | Python 3.10+ standard library only | No dependencies to install or break. Easy to audit, and it runs anywhere. |
| Storage | SQLite (WAL mode) | A single file, so there's no server to run. It supports triggers for append-only enforcement, and `BEGIN IMMEDIATE` gives serialised writes for the exposure checks. Paper and live use **separate files**. |
| Money | Integer pence | No floating-point drift in balances. |
| Modelling (stage 3) | pandas, scikit-learn, LightGBM | Logistic regression as the interpretable baseline, gradient boosting as the challenger, isotonic/Platt calibration. |
| API (stage 5) | FastAPI | Typed, fast, auto-generated docs. Exposes read endpoints, plus approve/reject for proposals. |
| Dashboard (stage 5) | React + TypeScript + Vite, Recharts | Standard and portfolio-friendly, with good charting for calibration and bankroll curves. |
| Scheduling | APScheduler, or Windows Task Scheduler | Pulls the card and odds before events. |

PostgreSQL would only be worth it if the app were hosted for several users.
For one person on one machine, SQLite is the right tool.

## Folder structure

```
ufc_edge/                  Python package
  config.py                RiskConfig: limits and validation          [built]
  db.py                    schema, triggers, connections              [built]
  ledger.py                BankrollService: the only way money moves  [built]
  risk.py                  independent risk engine (pure function)    [built]
  ev.py                    odds, de-vig, EV, Kelly staking            [built]
  money.py / timeutil.py   pence and UTC helpers                      [built]
  cli.py                   operator console (manual authorisations)   [built]
  data/                    collectors: UFCStats, odds, results        [stage 2]
  features/                point-in-time feature builder              [stage 2]
  model/                   training, calibration, prediction          [stage 3]
  backtest/                walk-forward backtester                    [stage 4]
  paper/                   paper-trading runner                       [stage 4]
  api/                     FastAPI app                                [stage 5]
dashboard/                 React + TypeScript frontend                [stage 5]
tests/                     unittest suite
docs/                      this documentation
data/                      local databases (git-ignored)
```

## Bankroll ledger

It's a double-entry ledger in integer pence. Each transaction's lines sum to
zero, so balances are always derived from the transaction history and never
stored as editable numbers.

| Account | Meaning |
|---|---|
| `OWNER` | Your money coming in (negative = contributed) |
| `ACTIVE` | Betting bankroll available to stake |
| `OPEN_STAKES` | Stakes on unsettled bets |
| `PROTECTED` | Realised profit. **Never stakeable** |
| `WITHDRAWN` | Protected profit confirmed as moved out |
| `MARKET` | Bookmaker side. `-MARKET` = net betting P&L |

**Capital boundary.** After every settlement, if `ACTIVE + OPEN_STAKES` is
above the authorised cap (£40), the excess is swept to `PROTECTED`
immediately. The cap covers open stakes too, so the money at risk can never
exceed £40. Potential winnings on open bets appear nowhere.

**Accounting- vs physically-protected.** Bookmakers hold one balance, so
`PROTECTED` is a ledger protection. It becomes `WITHDRAWN` (physically
separate) only when you confirm a transfer actually went through. The app
never claims otherwise.

## Database schema

| Table | Purpose | Protection |
|---|---|---|
| `system_state` | Single row: mode, status (`UNINITIALISED`/`ACTIVE`/`EXHAUSTED`), cap, kill switch, halt | Trigger: can't leave `EXHAUSTED` without a *new* authorisation id |
| `authorisations` | Every manual sign-off (operator, reason, payload) | Append-only |
| `risk_config_history` | Every version of the risk settings | Append-only, each tied to an authorisation |
| `events` | UFC events and start times | n/a |
| `bets` | Every recorded bet, with the odds snapshot, source, timestamp and model probability | Unique idempotency key; one non-void bet per selection; terms immutable; settle once; no deletes |
| `ledger_transactions` / `ledger_lines` | Double-entry ledger | Append-only |
| `audit_log` | Every proposal, rejection (with reasons and snapshot), settlement and state change | Append-only |

Stages 2–4 add `fighters`, `fights`, `fighter_fight_stats`, `odds_snapshots`
(timestamped quotes) and `predictions` (model version, features hash,
probabilities, created_at).

## Risk engine rules

`risk.evaluate()` returns every rule a proposal breaks:

- **Stop conditions:** not initialised, `BANKROLL_EXHAUSTED`, `KILL_SWITCH`, `SYSTEM_HALTED`.
- **Validity:** market allowed, event known and not started, odds fresh (≤ 15 min) and in range (1.20–4.00), source present.
- **Edge:** recomputed by the engine itself. It blends model and market probability 50/50 and needs EV ≥ 3%.
- **Stake:** ≥ the minimum stake, ≤ the active bankroll (`WOULD_USE_PROTECTED_FUNDS` if it would dip into profits), and ≤ 5% of capital in play.
- **Exposure:** ≤ 15% per event and ≤ 25% in total. Worst-case loss is ≤ 20% of the cap over a rolling 24 hours and ≤ 15% per event. There are at most 4 bets per event.
- **Duplicates:** idempotency key, plus one bet per selection.

The checks and the insert run in one `BEGIN IMMEDIATE` transaction, so
parallel requests are serialised. If reconciliation fails at any point, the
system **halts** (fails closed) and a manual authorisation is needed to
clear the halt.

**Exhaustion.** When no bets are open and the active bankroll is below £2,
or too small for any legal stake, status latches to `EXHAUSTED` and
survives restarts. Only `authorise_new_bankroll` (operator CLI, exact typed
phrase) clears it. With the default limits (5% max stake, 10p minimum), this
triggers at about £2 remaining.

## What "manual authorisation" means here

In a single-user local app, the guarantee is structural. The authorisation
methods are exposed only through the operator CLI, and later through
operator-only API endpoints. They need an exact typed phrase for the
specific amount or action, and each one is recorded permanently. The
prediction and agent code has no route to them. The database trigger means
even a direct SQL update can't clear the exhaustion latch without a new
authorisation record.
