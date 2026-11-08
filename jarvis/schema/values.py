"""The JSON Schemas for an env's values files (schema/*.schema.json), and their errors in the words
the operator reads. A schema is the authority for a file's shape: its keys, their types and simple
patterns. Rules that span fields or need logic stay in jarvis.schema.env."""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
CLUSTER_VARS = "cluster-vars"


@cache
def schema(name: str) -> dict[str, Any]:
    """schema/<name>.schema.json."""
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


@cache
def _validator(name: str) -> Draft202012Validator:
    doc = schema(name)
    Draft202012Validator.check_schema(doc)
    return Draft202012Validator(doc)


def required_keys(name: str) -> frozenset[str]:
    return frozenset(schema(name).get("required", ()))


def known_keys(name: str) -> frozenset[str]:
    return frozenset(schema(name).get("properties", {}))


def problems(name: str, data: dict[str, Any], rel_path: str) -> list[str]:
    """Each way data breaks schema <name>, as '<rel_path>: <message>'. A required key set to null
    counts as missing."""
    required = required_keys(name)
    instance = {key: value for key, value in data.items() if not (key in required and value is None)}
    found: list[str] = []
    for error in sorted(_validator(name).iter_errors(instance), key=lambda item: (list(item.absolute_path), item.validator)):
        for message in _messages(name, error, instance):
            if message not in found:
                found.append(message)
    return [f"{rel_path}: {message}" for message in found]


def _messages(name: str, error: SchemaError, instance: dict[str, Any]) -> list[str]:
    doc = schema(name)
    validator = str(error.validator)
    if not error.absolute_path:
        if validator == "required":
            match = re.match(r"'(.+)' is a required property", error.message)
            return [f"missing required key: {match.group(1) if match else error.message}"]
        if validator == "propertyNames" or "propertyNames" in error.schema_path:
            return [f"key must be lowercase snake_case: {error.instance}"]
        if validator == "additionalProperties":
            pattern = (doc.get("propertyNames") or {}).get("pattern", "")
            allowed = ", ".join(sorted(doc.get("properties", {})))
            # A key that is not snake_case is reported once, as that.
            return [
                f"unknown key '{key}' (allowed: {allowed})"
                for key in sorted(instance, key=str)
                if key not in doc.get("properties", {}) and re.search(pattern, str(key))
            ]
        return [(doc.get("x-messages") or {}).get(validator) or error.message]
    key = str(error.absolute_path[0])
    messages = ((doc.get("properties") or {}).get(key) or {}).get("x-messages") or {}
    return [messages.get(validator) or f"{key}: {error.message}"]
