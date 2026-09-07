from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, TypeAdapter

from .models import Point

TRAVEL_MODEL_VERSION = "EUCLIDEAN_GRID_V2"


class TravelTimeProvider(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def fingerprint(self) -> str: ...

    def minutes(self, origin: Point, destination: Point, departure_minute: int | None = None) -> int: ...


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


class EuclideanProviderSnapshot(BaseModel):
    kind: Literal["euclidean"] = "euclidean"
    version: str
    fingerprint: str
    minutes_per_grid_unit: int | float = Field(gt=0)
    minimum_nonzero_minutes: int = Field(ge=0)


class MatrixProviderSnapshot(BaseModel):
    kind: Literal["matrix"] = "matrix"
    version: str
    fingerprint: str
    matrix: list[tuple[str, str, int]]
    point_ids: list[tuple[int | float, int | float, str]]


def freeze_travel_provider(provider: TravelTimeProvider) -> dict[str, object]:
    if type(provider) is EuclideanTravelTimeProvider:
        return EuclideanProviderSnapshot(
            version=provider.version,
            fingerprint=provider.fingerprint,
            minutes_per_grid_unit=provider.minutes_per_grid_unit,
            minimum_nonzero_minutes=provider.minimum_nonzero_minutes,
        ).model_dump(mode="json")
    if type(provider) is MatrixTravelTimeProvider:
        return MatrixProviderSnapshot(
            version=provider.version,
            fingerprint=provider.fingerprint,
            matrix=[(a, b, value) for (a, b), value in sorted(provider.matrix.items())],
            point_ids=[(x, y, identifier) for (x, y), identifier in sorted(provider.point_ids.items())],
        ).model_dump(mode="json")
    raise ValueError("UNSUPPORTED_TRAVEL_PROVIDER: 后台任务只支持可冻结的欧氏或矩阵行程模型")


def restore_travel_provider(payload: dict[str, object]) -> TravelTimeProvider:
    adapter: TypeAdapter[EuclideanProviderSnapshot | MatrixProviderSnapshot] = TypeAdapter(
        EuclideanProviderSnapshot | MatrixProviderSnapshot
    )
    snapshot = adapter.validate_python(payload)
    provider: TravelTimeProvider
    if isinstance(snapshot, EuclideanProviderSnapshot):
        provider = EuclideanTravelTimeProvider(
            snapshot.minutes_per_grid_unit, snapshot.minimum_nonzero_minutes, snapshot.version
        )
    else:
        if any(value < 0 for _, _, value in snapshot.matrix):
            raise ValueError("travel time cannot be negative")
        if len({(a, b) for a, b, _ in snapshot.matrix}) != len(snapshot.matrix) or len(
            {(x, y) for x, y, _ in snapshot.point_ids}
        ) != len(snapshot.point_ids):
            raise ValueError("travel snapshot contains duplicate entries")
        provider = MatrixTravelTimeProvider(
            {(a, b): value for a, b, value in snapshot.matrix},
            {(x, y): identifier for x, y, identifier in snapshot.point_ids},
            snapshot.version,
        )
    if provider.fingerprint != snapshot.fingerprint:
        raise ValueError("TRAVEL_PROVIDER_FINGERPRINT_MISMATCH")
    return provider
