from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("FIELDFLOW_DB", "/tmp/fieldflow-openapi.db")

from backend.main import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "openapi.json"


def rendered_schema() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the committed OpenAPI contract")
    parser.add_argument("--write", action="store_true", help="replace the committed snapshot")
    args = parser.parse_args()
    rendered = rendered_schema()
    if args.write:
        SNAPSHOT.write_text(rendered, encoding="utf-8")
        print(f"updated {SNAPSHOT.relative_to(ROOT)}")
        return 0
    if not SNAPSHOT.exists():
        print("docs/openapi.json is missing; run scripts/check_openapi.py --write")
        return 1
    if SNAPSHOT.read_text(encoding="utf-8") != rendered:
        print("OpenAPI contract changed; review it and run scripts/check_openapi.py --write")
        return 1
    print("OpenAPI contract matches docs/openapi.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
