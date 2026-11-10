"""docs/reference.md is the render of the env schemas: schema/cluster-vars.schema.json,
schema/switches.schema.json, schema/apps.schema.json and schema/hosts.schema.json."""

from __future__ import annotations

from typing import Any

from jarvis.schema import reference
from jarvis.schema import values as _values


def _keys() -> list[tuple[str, str, dict[str, Any]]]:
    found = []
    for name, title, path in reference.FILES:
        node: Any = _values.schema(name)
        for key in path:
            node = node[key]
        found += [(title, key, spec) for key, spec in (node.get("properties") or {}).items()]
    return found


def test_the_reference_page_is_the_schemas_render() -> None:
    """Rule: the page is generated; a schema change re-renders it (python -m jarvis.schema.reference)."""
    assert reference.PAGE.read_text(encoding="utf-8") == reference.render()
    assert reference.main(["--check"]) == 0


def test_every_key_says_what_it_means() -> None:
    """Rule: a key an env can set has a description in its schema, the one place it is explained."""
    keys = _keys()
    assert len(keys) > 30
    assert [f"{title}: {key}" for title, key, spec in keys if not str(spec.get("description", "")).strip()] == []


def test_a_conditionally_required_key_says_when() -> None:
    page = reference.render()
    assert "| `app_dns_cluster_ip` | no; yes when switches.yaml lists app-dns |" in page
    assert "refs/tags/&lt;tag&gt;" in page and "| `ssh_user` | no | string | `ubuntu` |" in page
