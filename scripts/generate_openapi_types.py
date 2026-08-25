from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "docs" / "openapi.json"
OUTPUT = ROOT / "frontend" / "src" / "generated" / "openapi.ts"


def _property_name(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value) else json.dumps(value)


def _literal(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return json.dumps(value, ensure_ascii=False)


def _schema_type(schema: object, *, indent: int = 0) -> str:
    if not isinstance(schema, dict):
        return "unknown"
    reference = schema.get("$ref")
    if isinstance(reference, str):
        return f"components['schemas'][{json.dumps(reference.rsplit('/', 1)[-1])}]"
    if "const" in schema:
        return _literal(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list):
        return " | ".join(_literal(item) for item in enum) or "never"
    for composition, separator in (("allOf", " & "), ("oneOf", " | "), ("anyOf", " | ")):
        branches = schema.get(composition)
        if isinstance(branches, list):
            return separator.join(f"({_schema_type(item, indent=indent)})" for item in branches) or "unknown"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return " | ".join(
            "null" if item == "null" else _schema_type({**schema, "type": item}, indent=indent) for item in schema_type
        )
    if schema_type == "array":
        return f"Array<{_schema_type(schema.get('items', {}), indent=indent)}>"
    if schema_type in {"integer", "number"}:
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "string":
        return "string"
    if schema_type == "null":
        return "null"
    properties = schema.get("properties")
    if schema_type == "object" or isinstance(properties, dict):
        required = set(schema.get("required", []))
        lines = ["{"]
        if isinstance(properties, dict):
            for name, child in properties.items():
                optional = "" if name in required else "?"
                description = child.get("description") if isinstance(child, dict) else None
                if isinstance(description, str) and description.strip():
                    lines.append(" " * (indent + 2) + f"/** {description.strip()} */")
                lines.append(
                    " " * (indent + 2) + f"{_property_name(name)}{optional}: {_schema_type(child, indent=indent + 2)};"
                )
        additional = schema.get("additionalProperties")
        if additional is True:
            lines.append(" " * (indent + 2) + "[key: string]: unknown;")
        elif isinstance(additional, dict):
            lines.append(" " * (indent + 2) + f"[key: string]: {_schema_type(additional, indent=indent + 2)};")
        lines.append(" " * indent + "}")
        return "\n".join(lines)
    return "unknown"


def render() -> str:
    document = json.loads(OPENAPI.read_text(encoding="utf-8"))
    schemas = document.get("components", {}).get("schemas", {})
    lines = [
        "/* This file is generated from docs/openapi.json. Do not edit it by hand. */",
        "/* Run: python3 scripts/generate_openapi_types.py --write */",
        "",
        "export interface components {",
        "  schemas: {",
    ]
    for name in sorted(schemas):
        rendered = _schema_type(schemas[name], indent=4).replace("\n", "\n    ")
        lines.append(f"    {_property_name(name)}: {rendered};")
    lines.extend(["  };", "}", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate TypeScript component types from FieldFlow OpenAPI")
    parser.add_argument("--write", action="store_true", help="update the generated source file")
    arguments = parser.parse_args()
    expected = render()
    if arguments.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(expected, encoding="utf-8")
        print(f"updated {OUTPUT.relative_to(ROOT)}")
        return 0
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != expected:
        print("generated OpenAPI TypeScript types are stale; run scripts/generate_openapi_types.py --write")
        return 1
    print("generated OpenAPI TypeScript types are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
