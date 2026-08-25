from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def canonical(requirement: str) -> tuple[str, tuple[str, ...], str]:
    parsed = Requirement(requirement)
    return parsed.name.lower(), tuple(sorted(parsed.extras)), str(parsed.specifier)


def main() -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = [*project["dependencies"], *project["optional-dependencies"]["dev"]]
    pinned = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    declared_set = {canonical(item) for item in declared}
    pinned_set = {canonical(item) for item in pinned}
    if declared_set != pinned_set or len(declared_set) != len(declared) or len(pinned_set) != len(pinned):
        missing = sorted(declared_set - pinned_set)
        extra = sorted(pinned_set - declared_set)
        print(f"dependency declarations differ; missing={missing}, extra={extra}")
        return 1
    print(f"dependency declarations match ({len(declared_set)} pinned packages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
