"""Exception types used across the application.

Every rejection the risk engine produces is a ``RiskRejected`` carrying
machine-readable reason codes, so the dashboard can explain *why* a bet
was blocked rather than just that it was.
"""

from __future__ import annotations


class UfcEdgeError(Exception):
    """Base class for all application errors."""


class RiskRejected(UfcEdgeError):
    """A proposed wager failed one or more risk checks."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("Bet rejected by risk engine: " + "; ".join(self.reasons))


class AuthorisationError(UfcEdgeError):
    """A protected action was attempted without valid manual authorisation."""


class SettlementError(UfcEdgeError):
    """A settlement was invalid, unverified, premature or duplicated."""


class LedgerIntegrityError(UfcEdgeError):
    """The ledger failed reconciliation. The system halts when this occurs."""


class StateError(UfcEdgeError):
    """An action is not allowed in the system's current state."""
