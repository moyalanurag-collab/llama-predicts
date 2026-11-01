"""Independent risk engine.

The AI/model *proposes* a bet; this module decides whether it is allowed.
``evaluate`` is a pure function. It takes the proposal, a snapshot of the
ledger read inside the same database transaction that would record the bet,
the active config and the current time, and returns every rule the proposal
breaks. An empty list means approved.

The engine does not trust anything it can recompute. It re-derives the
edge from the stated probability and odds rather than accepting an EV
figure, and it re-checks the stake against hard limits rather than
accepting the strategy's sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from .config import RiskConfig
from .ev import blend_probability, expected_value

# Allow a little clock skew between the odds feed and this machine.
CLOCK_SKEW = timedelta(seconds=60)


@dataclass(frozen=True)
class BetProposal:
    idempotency_key: str
    event_id: str
    fight_id: str
    market: str
    selection: str
    decimal_odds: Decimal
    stake_pence: int
    odds_source: str
    odds_captured_at: datetime
    model_probability: float
    # De-vigged fair probability for this selection from the same odds quote.
    market_probability: float
    commission: float = 0.0


@dataclass(frozen=True)
class RiskSnapshot:
    status: str
    kill_switch: bool
    halted: bool
    bankroll_cap_pence: int
    active_pence: int
    open_stakes_pence: int
    protected_pence: int
    event_starts_at: datetime | None  # None = event not found
    event_open_stakes_pence: int
    event_bet_count: int
    event_realised_pnl_pence: int
    realised_pnl_24h_pence: int
    selection_already_bet: bool


def _frac(fraction: float, pence: int) -> int:
    return int((Decimal(str(fraction)) * pence).to_integral_value(rounding=ROUND_FLOOR))


def evaluate(p: BetProposal, s: RiskSnapshot, cfg: RiskConfig, now: datetime) -> list[str]:
    """Return a list of reason codes; empty means the bet is permitted."""
    reasons: list[str] = []

    # --- Stop conditions --------------------------------------------------
    if s.status == "UNINITIALISED":
        reasons.append("NOT_INITIALISED: no bankroll has been authorised")
    if s.status == "EXHAUSTED":
        reasons.append("BANKROLL_EXHAUSTED: manual restart required")
    if s.kill_switch:
        reasons.append("KILL_SWITCH: emergency stop is engaged")
    if s.halted:
        reasons.append("SYSTEM_HALTED: integrity problem must be resolved first")
    if cfg.execution_mode != "manual":
        reasons.append("EXECUTION_MODE: only manual mode is permitted")

    # --- Market, event and odds validity ----------------------------------
    if p.market not in cfg.allowed_markets:
        reasons.append(f"MARKET_NOT_ALLOWED: {p.market!r}")
    if s.event_starts_at is None:
        reasons.append(f"UNKNOWN_EVENT: {p.event_id!r}")
    elif now >= s.event_starts_at:
        reasons.append("EVENT_STARTED: no bets after the event start time")
    if p.odds_captured_at.tzinfo is None:
        reasons.append("ODDS_TIMESTAMP_INVALID: timestamp must be timezone-aware")
    else:
        age = now - p.odds_captured_at
        if age > timedelta(seconds=cfg.max_odds_age_seconds):
            reasons.append(f"STALE_ODDS: quote is {int(age.total_seconds())}s old")
        if p.odds_captured_at > now + CLOCK_SKEW:
            reasons.append("ODDS_TIMESTAMP_IN_FUTURE: cannot verify quote")
    if not p.odds_source.strip():
        reasons.append("ODDS_SOURCE_MISSING")
    if not Decimal(cfg.min_decimal_odds) <= p.decimal_odds <= Decimal(cfg.max_decimal_odds):
        reasons.append(
            f"ODDS_OUT_OF_RANGE: {p.decimal_odds} not in "
            f"[{cfg.min_decimal_odds}, {cfg.max_decimal_odds}]"
        )

    # --- Edge (recomputed, never taken on trust) ---------------------------
    probs_ok = 0 < p.model_probability < 1 and 0 < p.market_probability < 1
    if not probs_ok:
        reasons.append("INVALID_PROBABILITY")
    elif p.decimal_odds > 1:
        blended = blend_probability(p.model_probability, p.market_probability, cfg.model_weight)
        ev = expected_value(blended, float(p.decimal_odds), p.commission)
        if ev < cfg.min_edge:
            reasons.append(f"INSUFFICIENT_EDGE: EV {ev:.4f} < {cfg.min_edge}")

    # --- Stake and capital ---------------------------------------------------
    capital = s.active_pence + s.open_stakes_pence  # protected funds deliberately excluded
    if p.stake_pence <= 0:
        reasons.append("INVALID_STAKE")
    if p.stake_pence < cfg.min_stake_pence:
        reasons.append(f"STAKE_BELOW_MINIMUM: {p.stake_pence}p < {cfg.min_stake_pence}p")
    if p.stake_pence > s.active_pence:
        if p.stake_pence <= s.active_pence + s.protected_pence:
            reasons.append("WOULD_USE_PROTECTED_FUNDS: protected profits are never stakeable")
        else:
            reasons.append("STAKE_EXCEEDS_ACTIVE_BANKROLL")
    if s.active_pence < cfg.min_active_bankroll_pence:
        reasons.append("BELOW_MIN_BANKROLL: active bankroll under the minimum threshold")
    if p.stake_pence > _frac(cfg.max_stake_fraction, capital):
        reasons.append("STAKE_EXCEEDS_PER_BET_LIMIT")

    # --- Exposure and loss limits (worst case: every open bet loses) ------
    if s.event_open_stakes_pence + p.stake_pence > _frac(cfg.max_event_exposure_fraction, capital):
        reasons.append("EVENT_EXPOSURE_LIMIT")
    if s.open_stakes_pence + p.stake_pence > _frac(cfg.max_total_exposure_fraction, capital):
        reasons.append("TOTAL_EXPOSURE_LIMIT")
    worst_day = max(0, -s.realised_pnl_24h_pence) + s.open_stakes_pence + p.stake_pence
    if worst_day > _frac(cfg.max_daily_loss_fraction, s.bankroll_cap_pence):
        reasons.append("DAILY_LOSS_LIMIT: worst-case 24h loss would exceed limit")
    worst_event = max(0, -s.event_realised_pnl_pence) + s.event_open_stakes_pence + p.stake_pence
    if worst_event > _frac(cfg.max_event_loss_fraction, s.bankroll_cap_pence):
        reasons.append("EVENT_LOSS_LIMIT: worst-case event loss would exceed limit")
    if s.event_bet_count >= cfg.max_bets_per_event:
        reasons.append("MAX_BETS_PER_EVENT")
    if s.selection_already_bet:
        reasons.append("DUPLICATE_SELECTION: a bet on this selection already exists")

    return reasons
