"""The JSON Schemas for an env's values files (schema/*.schema.json). A schema is the one home of
its file's shape: keys, types, patterns and defaults. Its errors are the validator's own."""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator

from jarvis.paths import PRODUCT_ROOT

SCHEMA_DIR = PRODUCT_ROOT / "schema"
CLUSTER_VARS = "cluster-vars"


@cache
def schema(name: str) -> dict[str, Any]:
    """schema/<name>.schema.json."""
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


@cache
def _validator(name: str) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema(name))
    return Draft202012Validator(schema(name))


def defaults(name: str) -> dict[str, Any]:
    """Each top-level key's default, as the schema states it."""
    return {key: spec["default"] for key, spec in schema(name)["properties"].items() if "default" in spec}


def problems(name: str, data: Any, rel_path: str) -> list[str]:
    """Each way data breaks schema <name>, as '<rel_path>: <where>: <message>'."""
    return [
        f"{rel_path}: {'.'.join(map(str, error.absolute_path)) or '(top level)'}: {error.message}"
        for error in sorted(_validator(name).iter_errors(data), key=lambda item: list(map(str, item.absolute_path)))
    ]
