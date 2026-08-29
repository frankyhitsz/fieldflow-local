from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
LOCKS = (ROOT / "requirements-runtime.lock", ROOT / "requirements-dev.lock")
REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


class Component(TypedDict):
    name: str
    version: str
    hashes: list[str]


def parse_lock(path: Path) -> list[Component]:
    components: list[Component] = []
    current: Component | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        requirement = REQUIREMENT.match(raw_line)
        if requirement:
            if current is not None and not current["hashes"]:
                raise ValueError(f"{path.name}: {current['name']} has no distribution hash")
            current = {
                "name": requirement.group(1).lower().replace("_", "-"),
                "version": requirement.group(2),
                "hashes": [],
            }
            components.append(current)
        if current is not None:
            current["hashes"].extend(HASH.findall(raw_line))
    if current is not None and not current["hashes"]:
        raise ValueError(f"{path.name}: {current['name']} has no distribution hash")
    if not components:
        raise ValueError(f"{path.name}: no pinned requirements found")
    return components


def write_runtime_sbom(components: list[Component]) -> None:
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "fieldflow-local"}},
        "components": [
            {
                "type": "library",
                "name": item["name"],
                "version": item["version"],
                "purl": f"pkg:pypi/{item['name']}@{item['version']}",
                "properties": [
                    {"name": "fieldflow:allowed-distribution-sha256", "value": digest} for digest in item["hashes"]
                ],
            }
            for item in components
        ],
    }
    destination = ROOT / "docs" / "sbom-runtime.cdx.json"
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    try:
        parsed = {path: parse_lock(path) for path in LOCKS}
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    write_runtime_sbom(parsed[LOCKS[0]])
    print(f"validated {len(parsed[LOCKS[0]])} runtime and {len(parsed[LOCKS[1]])} development packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
