"""Time helpers.

All timestamps are stored as UTC ISO-8601 strings with a fixed format, so
string comparison in SQL matches chronological order. Using UTC only also
avoids needing a timezone database (which Windows does not ship).
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """Format a timezone-aware datetime as a sortable UTC string."""
    if dt.tzinfo is None:
        raise ValueError("Naive datetimes are not allowed; pass a timezone-aware value.")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
