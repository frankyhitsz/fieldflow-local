from __future__ import annotations

import math

from .models import Point


DAY_START = 8 * 60


def hhmm(minutes: int) -> str:
    minutes = max(0, int(minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def travel_minutes(a: Point, b: Point) -> int:
    """Deterministic offline travel time: 1 grid unit ~= 0.36 minutes."""
    return max(3, int(round(math.hypot(a.x - b.x, a.y - b.y) * 0.36)))
