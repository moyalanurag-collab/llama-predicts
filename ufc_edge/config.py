"""Risk configuration.

Defaults are deliberately conservative and were chosen from general
bankroll-management principles, *not* tuned on historical results. Tuning
risk limits on a small backtest is a classic way to overfit.

Changing the configuration requires a manual authorisation (see
``BankrollService.update_risk_config``); every version is stored
append-only in the database.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class RiskConfig:
    # --- Staking -----------------------------------------------------------
    # Fraction of full Kelly to stake. Full Kelly assumes the probability is
    # exactly right; it never is, so a quarter is the usual cautious choice.
    kelly_fraction: float = 0.25
    # Weight given to the model when blending with the de-vigged market
    # probability (1.0 = trust the model completely, 0.0 = trust the market).
    model_weight: float = 0.5
    # Minimum expected value per £1 staked, *after* blending, for a bet to qualify.
    min_edge: float = 0.03

    # --- Hard limits (enforced by the risk engine) -------------------------
    # Max single stake as a fraction of capital in play (active + open stakes).
    max_stake_fraction: float = 0.05
    # Smallest stake the bookmaker accepts. Check your bookmaker; exchanges are often higher.
    min_stake_pence: int = 10
    # Max open stakes on one event, as a fraction of capital in play.
    max_event_exposure_fraction: float = 0.15
    # Max open stakes across everything, as a fraction of capital in play.
    max_total_exposure_fraction: float = 0.25
    # Worst-case loss limits, as a fraction of the authorised bankroll cap.
    max_daily_loss_fraction: float = 0.20   # rolling 24 hours
    max_event_loss_fraction: float = 0.15
    max_bets_per_event: int = 4
    # Below this active balance no new bets are allowed. Once no bets are
    # open, the system latches into BANKROLL EXHAUSTED. It also latches if
    # the largest permitted stake would be under min_stake_pence, because no
    # legal bet would then be possible.
    min_active_bankroll_pence: int = 200
    # Odds older than this are treated as stale.
    max_odds_age_seconds: int = 900
    min_decimal_odds: str = "1.20"
    max_decimal_odds: str = "4.00"
    allowed_markets: tuple[str, ...] = field(default=("moneyline",))
    # Only "manual" exists in v1: bets are recommendations that a human places.
    execution_mode: str = "manual"

    def validate(self) -> None:
        """Raise ValueError if any setting is outside a sane range."""
        problems: list[str] = []
        if not 0 < self.kelly_fraction <= 0.5:
            problems.append("kelly_fraction must be in (0, 0.5]")
        if not 0 <= self.model_weight <= 1:
            problems.append("model_weight must be in [0, 1]")
        if not 0 <= self.min_edge <= 1:
            problems.append("min_edge must be in [0, 1]")
        if not 0 < self.max_stake_fraction <= 0.10:
            problems.append("max_stake_fraction must be in (0, 0.10]")
        if self.min_stake_pence < 1:
            problems.append("min_stake_pence must be at least 1")
        if not 0 < self.max_event_exposure_fraction <= 0.5:
            problems.append("max_event_exposure_fraction must be in (0, 0.5]")
        if not 0 < self.max_total_exposure_fraction <= 0.5:
            problems.append("max_total_exposure_fraction must be in (0, 0.5]")
        if self.max_event_exposure_fraction > self.max_total_exposure_fraction:
            problems.append("event exposure cannot exceed total exposure")
        if not 0 < self.max_daily_loss_fraction <= 1:
            problems.append("max_daily_loss_fraction must be in (0, 1]")
        if not 0 < self.max_event_loss_fraction <= 1:
            problems.append("max_event_loss_fraction must be in (0, 1]")
        if self.max_bets_per_event < 1:
            problems.append("max_bets_per_event must be at least 1")
        if self.min_active_bankroll_pence < 1:
            problems.append("min_active_bankroll_pence must be at least 1")
        if self.max_odds_age_seconds < 1:
            problems.append("max_odds_age_seconds must be positive")
        if not Decimal("1") < Decimal(self.min_decimal_odds) < Decimal(self.max_decimal_odds):
            problems.append("need 1 < min_decimal_odds < max_decimal_odds")
        if not self.allowed_markets:
            problems.append("allowed_markets cannot be empty")
        if self.execution_mode != "manual":
            problems.append("only execution_mode='manual' is implemented")
        if problems:
            raise ValueError("Invalid risk config: " + "; ".join(problems))

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "RiskConfig":
        data = json.loads(raw)
        data["allowed_markets"] = tuple(data["allowed_markets"])
        return cls(**data)
