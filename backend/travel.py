from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .models import Point

TRAVEL_MODEL_VERSION = "EUCLIDEAN_GRID_V2"


class TravelTimeProvider(Protocol):
    version: str

    @property
    def fingerprint(self) -> str:
        ...

    def minutes(self, origin: Point, destination: Point, departure_minute: int | None = None) -> int:
        ...


@dataclass(frozen=True)
class EuclideanTravelTimeProvider:
    minutes_per_grid_unit: float = 0.36
    minimum_nonzero_minutes: int = 3
    version: str = TRAVEL_MODEL_VERSION

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "version": self.version,
                "minutes_per_grid_unit": self.minutes_per_grid_unit,
                "minimum_nonzero_minutes": self.minimum_nonzero_minutes,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def minutes(self, origin: Point, destination: Point, departure_minute: int | None = None) -> int:
        distance = math.hypot(origin.x - destination.x, origin.y - destination.y)
        if distance == 0:
            return 0
        return max(self.minimum_nonzero_minutes, int(round(distance * self.minutes_per_grid_unit)))


@dataclass(frozen=True)
class MatrixTravelTimeProvider:
    matrix: Mapping[tuple[str, str], int]
    point_ids: Mapping[tuple[float, float], str]
    version: str = "IMPORTED_MATRIX_V1"

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "version": self.version,
                "matrix": sorted((origin, destination, value) for (origin, destination), value in self.matrix.items()),
                "point_ids": sorted((x, y, point_id) for (x, y), point_id in self.point_ids.items()),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def minutes(self, origin: Point, destination: Point, departure_minute: int | None = None) -> int:
        origin_id = self.point_ids.get((origin.x, origin.y))
        destination_id = self.point_ids.get((destination.x, destination.y))
        if origin_id is None or destination_id is None:
            raise KeyError("travel matrix does not contain one of the requested locations")
        try:
            value = self.matrix[(origin_id, destination_id)]
        except KeyError as error:
            raise KeyError(f"travel matrix is missing {origin_id} -> {destination_id}") from error
        if value < 0:
            raise ValueError("travel time cannot be negative")
        return int(value)


DEFAULT_TRAVEL_PROVIDER = EuclideanTravelTimeProvider()
