from __future__ import annotations

from .models import Point
from .travel import DEFAULT_TRAVEL_PROVIDER

DAY_START = 8 * 60


def hhmm(minutes: int) -> str:
    minutes = max(0, int(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def travel_minutes(a: Point, b: Point) -> int:
    """Deterministic offline travel time shared by solver, verifier and reports."""
    return DEFAULT_TRAVEL_PROVIDER.minutes(a, b)
