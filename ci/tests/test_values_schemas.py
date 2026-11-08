"""The values-file schemas (schema/*.schema.json): valid JSON Schemas that every committed env file
and env shape passes, named by each env-00 file's yaml-language-server line."""

from __future__ import annotations

import json

import pytest
import yaml
from jsonschema import Draft202012Validator

from support import REPO_ROOT

_FILES = {
    "cluster-vars": "values/cluster-vars.yaml",
    "switches": "values/switches.yaml",
    "apps": "values/apps.yaml",
    "hosts": "inventory/hosts.yml",
}


def _validator(name: str) -> Draft202012Validator:
    doc = json.loads((REPO_ROOT / "schema" / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(doc)
    return Draft202012Validator(doc)


@pytest.mark.parametrize("name", sorted(_FILES))
def test_each_env_file_passes_its_schema_and_names_it(name: str) -> None:
    """Rule: every committed env's file passes its schema; env-00's names the schema for editors."""
    paths = sorted((REPO_ROOT / "envs").glob(f"*/{_FILES[name]}"))
    if name == "hosts":
        paths += sorted((REPO_ROOT / "ci/tests/fixtures/env-shapes").glob("*/hosts.yml"))
    assert paths
    for path in paths:
        errors = [error.message for error in _validator(name).iter_errors(yaml.safe_load(path.read_text(encoding="utf-8")) or {})]
        assert errors == [], f"{path}: {errors}"
    example = (REPO_ROOT / "envs/env-00" / _FILES[name]).read_text(encoding="utf-8")
    assert example.startswith(f"# yaml-language-server: $schema=../../../schema/{name}.schema.json\n")


@pytest.mark.parametrize(
    ("name", "doc"),
    [
        ("switches", {"optional": ["observability"]}),
        ("switches", {"extras": []}),
        ("apps", {"versions": {"notes": "1.2.3"}}),
        ("apps", {"backups": {"mode": "some"}}),
        ("hosts", {"all": {"hosts": {"cp-1": {"node_roles": ["master"]}}}}),
        ("hosts", {"all": {"hosts": {"cp-1": {"node_roles": ["control_plane"], "data_disk": "/dev/sdb"}}}}),
    ],
)
def test_a_wrong_shape_fails_its_schema(name: str, doc: dict) -> None:
    assert list(_validator(name).iter_errors(doc))
