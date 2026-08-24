from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .hashing import content_hash

DECISION_ALGORITHM_VERSION = "FIELD_SERVICE_DECISION_V3"


@lru_cache(maxsize=1)
def decision_build_sha() -> str:
    injected = os.getenv("FIELDFLOW_BUILD_SHA", "").strip()
    if injected:
        return injected
    backend_root = Path(__file__).resolve().parent
    source = [
        {
            "path": path.name,
            "content": path.read_text(encoding="utf-8"),
        }
        for path in sorted(backend_root.glob("*.py"))
    ]
    return f"dev-{content_hash(source)[:16]}"
