from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel

HASH_SCHEMA_VERSION = "FIELD_SERVICE_CANONICAL_JSON_V2"


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        return {unicodedata.normalize("NFC", str(key)): _jsonable(item) for key, item in mapping.items()}
    if isinstance(value, list | tuple):
        sequence = cast(list[object] | tuple[object, ...], value)
        return [_jsonable(item) for item in sequence]
    if isinstance(value, set):
        members = cast(set[object], value)
        return sorted((_jsonable(item) for item in members), key=repr)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    payload = f"{HASH_SCHEMA_VERSION}\n{canonical_json(value)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
