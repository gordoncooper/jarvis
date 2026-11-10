"""docs/reference.md: every key of an env's values files, from their JSON Schemas (schema/).

The schemas are the one home for what a key means, its type, its default and when it is
required; this page is their render, and editors show the same descriptions as you type.

  python -m jarvis.schema.reference           write docs/reference.md
  python -m jarvis.schema.reference --check   exit 1 when it differs from the schemas
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from jarvis.paths import PRODUCT_ROOT
from jarvis.schema import values as _values

PAGE = PRODUCT_ROOT / "docs" / "reference.md"
# Each file, and the path to the object whose properties are its keys.
FILES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cluster-vars", "values/cluster-vars.yaml", ()),
    ("switches", "values/switches.yaml", ()),
    ("apps", "values/apps.yaml", ()),
    ("apps", "values/apps.yaml: backups", ("properties", "backups")),
    ("hosts", "inventory/hosts.yml: each host under all.hosts", ("properties", "all", "properties", "hosts", "additionalProperties")),
)


def _type(spec: dict[str, Any]) -> str:
    if "enum" in spec:
        return " or ".join(f"`{value}`" for value in spec["enum"])
    if "const" in spec:
        return f"`{str(spec['const']).lower()}`"
    kinds = spec.get("type")
    kinds = [kind for kind in (kinds if isinstance(kinds, list) else [kinds]) if kind and kind != "null"]
    # A key whose shape Python checks (a CIDR list, an address, known_hosts lines) has no type here.
    text = " or ".join(kinds) if kinds else "see Meaning"
    items = spec.get("items") or {}
    if "array" in kinds and "enum" in items:
        text += " of " + ", ".join(f"`{value}`" for value in items["enum"])
    return text


def _required(name: str, spec: dict[str, Any], required: list[str]) -> str:
    if name in required:
        return "yes"
    return f"no; yes {spec['x-required']}" if spec.get("x-required") else "no"


def _default(spec: dict[str, Any]) -> str:
    if "default" not in spec:
        return ""
    value = spec["default"]
    if isinstance(value, bool):
        value = str(value).lower()
    return f"`{value}`" if value != [] else "`[]`"


def _cell(text: str) -> str:
    """Table-safe: a pipe would end the cell, and <tag> would vanish as an HTML tag."""
    return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")


def _table(schema: dict[str, Any]) -> list[str]:
    required = list(schema.get("required") or [])
    rows = ["| Key | Required | Type | Default | Meaning |", "| --- | --- | --- | --- | --- |"]
    for name, spec in sorted((schema.get("properties") or {}).items()):
        rows.append(
            f"| `{name}` | {_required(name, spec, required)} | {_cell(_type(spec))} | {_default(spec)} | {_cell(spec.get('description', ''))} |"
        )
    return rows


def render() -> str:
    lines = [
        "# Reference: an env's values",
        "",
        "Generated from the JSON Schemas in `schema/` by `python -m jarvis.schema.reference`; edit the schemas, not this page.",
        "Each values file names its schema on its first line, so an editor shows the same descriptions as you type.",
        "Rules that span keys or files are in [schema/CONTRACT.md](../schema/CONTRACT.md), and `jarvis validate` checks them all.",
    ]
    for name, title, path in FILES:
        node: Any = _values.schema(name)
        for key in path:
            node = node[key]
        lines += ["", f"## `{title}`", ""]
        if not path and node.get("description"):
            lines += [str(node["description"]), ""]
        lines += _table(node)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Exit 1 when docs/reference.md differs from the schemas")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if not PAGE.is_file() or PAGE.read_text(encoding="utf-8") != text:
            print("FAIL reference: docs/reference.md is stale; run python -m jarvis.schema.reference", flush=True)
            return 1
        print("PASS reference: docs/reference.md matches the schemas", flush=True)
        return 0
    PAGE.write_text(text, encoding="utf-8")
    print(f"wrote {PAGE.relative_to(PRODUCT_ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
