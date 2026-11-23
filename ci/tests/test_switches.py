"""An env's switches file: names render the Flux wiring, and an unknown name or stale wiring fails."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jarvis.env import switches as _switches
from jarvis.schema.env import ValidationError, validate_one_env
from support import (
    ExampleCopy,
    _kustomize_build,
    bootstrap_example_copy,
    flux_built,
    render_switches,
    set_switches,
)


def _errors(copy: ExampleCopy) -> str:
    with pytest.raises(ValidationError) as caught:
        validate_one_env(copy.root, copy.name)
    return "\n".join(caught.value.messages)


def _tree(copy: ExampleCopy) -> dict[str, str]:
    """Every file under the env's values and flux trees, by path."""
    return {
        path.relative_to(copy.env_dir).as_posix(): path.read_text(encoding="utf-8")
        for top in ("values", "flux")
        for path in (copy.env_dir / top).rglob("*")
        if path.is_file()
    }


def _objects(copy: ExampleCopy, rel: str) -> set[tuple[str, str]]:
    return {(doc["kind"], doc["metadata"]["name"]) for doc in flux_built(copy.root, copy.name, rel)}


def _set_app(env_dir: Path, app: str, entry: dict | None) -> None:
    path = env_dir / "values" / "apps.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if entry is None:
        data.pop(app, None)
    else:
        data[app] = entry
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_a_listed_app_takes_its_name_and_its_pin(tmp_path: Path) -> None:
    """Rule: an app's entry in values/apps.yaml, its name and pinned version, enables it: only that
    file and its wiring change, and a second render changes nothing."""
    copy = bootstrap_example_copy(tmp_path)
    _set_app(copy.env_dir, "hello-web", None)
    render_switches(copy.env_dir, copy.root)
    validate_one_env(copy.root, copy.name)
    before = _tree(copy)
    assert "app-hello-web-wiring" not in before["flux/generated/apps.yaml"]

    _set_app(copy.env_dir, "hello-web", {"version": "v0.3.0"})
    render_switches(copy.env_dir, copy.root)
    after = _tree(copy)
    assert "app-hello-web-wiring" in after["flux/generated/apps.yaml"]
    assert "refs/tags/hello-web/v0.3.0" in after["flux/generated/apps.yaml"]
    assert {path for path in after if after[path] != before.get(path)} == {"values/apps.yaml", "flux/generated/apps.yaml"}
    validate_one_env(copy.root, copy.name)
    render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == after


@pytest.mark.parametrize(
    ("section", "bad"),
    [("features", "observability"), ("addons", "nope")],
)
def test_an_unknown_name_fails_and_refuses_the_render(tmp_path: Path, section: str, bad: str) -> None:
    """Rule: a name core does not hold fails, before the render writes anything."""
    copy = bootstrap_example_copy(tmp_path)
    data = yaml.safe_load((copy.env_dir / _switches.SWITCHES_FILE).read_text(encoding="utf-8"))
    set_switches(copy.env_dir, **{section: [*data[section], bad]})
    assert repr(bad) in _errors(copy)
    before = _tree(copy)
    with pytest.raises(SystemExit, match=bad):
        render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == before


@pytest.mark.parametrize(
    ("name", "folder", "kind", "object_name"),
    [
        (_switches.OFFSITE_COPY, _switches.OFFSITE_COPY_DIR, "CronJob", "restic-offsite"),
        (_switches.ALERTS, _switches.ALERTS_DIR, "Alert", "alerts"),
    ],
)
def test_an_optional_piece_renders_only_from_its_name(name: str, folder: str, kind: str, object_name: str) -> None:
    """Rule: always-on core carries no optional piece. Its folder sits under core/feature, the
    always-on list does not build it, and the env's wiring lists that folder only once the env names it."""
    def has_object(rel: str) -> bool:
        return any(
            isinstance(doc, dict) and (doc["kind"], doc["metadata"]["name"]) == (kind, object_name)
            for doc in yaml.safe_load_all(_kustomize_build(rel))
        )

    assert folder.startswith("core/feature/") and has_object(folder)
    assert not has_object("core/platform/configs")
    assert folder in _switches.platform_configs({"features": [name]})
    assert folder not in _switches.platform_configs({"features": []})


def test_an_unlisted_optional_component_is_not_rendered(tmp_path: Path) -> None:
    """Rule: a feature is off until named; one name turns it on and brings its objects."""
    copy = bootstrap_example_copy(tmp_path)
    assert ("CronJob", "restic-offsite") in _objects(copy, "platform-configs")
    (copy.env_dir / "values" / "apps.yaml").unlink()
    set_switches(copy.env_dir, features=[])
    render_switches(copy.env_dir, copy.root)
    off = _objects(copy, "platform-configs")
    assert ("CronJob", "restic-offsite") not in off
    assert ("Deployment", "restic-apps") not in off
    assert not any(name.startswith("app-") for _kind, name in off)
    assert not (copy.env_dir / "flux" / "generated" / "apps.yaml").exists()


def test_a_sealed_app_credential_registers_on_the_apps_server(tmp_path: Path) -> None:
    """Rule: app-hosting brings the apps server, and each app with backups on whose credential is
    sealed registers on it: notes, whose volume jarvis secrets minted one for. An app without
    one registers nothing."""
    copy = bootstrap_example_copy(tmp_path)
    validate_one_env(copy.root, copy.name)
    objects = _objects(copy, "platform-configs")
    assert {("Deployment", "restic-apps"), ("Secret", "backups-restic-app-notes")} <= objects
    assert not any(name.startswith("backups-restic-app-") and name != "backups-restic-app-notes" for _kind, name in objects)


def test_an_env_with_a_flux_tree_needs_the_file(tmp_path: Path) -> None:
    """Rule: the switches file is required, so a missing one never reads as nothing enabled."""
    copy = bootstrap_example_copy(tmp_path)
    (copy.env_dir / _switches.SWITCHES_FILE).unlink()
    assert "missing required file" in _errors(copy) and _switches.SWITCHES_FILE in _errors(copy)


def test_app_names_are_refused_in_the_switches_file(tmp_path: Path) -> None:
    """Rule: one source of truth: the apps an env runs are values/apps.yaml's entries only."""
    copy = bootstrap_example_copy(tmp_path)
    set_switches(copy.env_dir, apps=["notes"])
    assert "('apps' was unexpected)" in _errors(copy)


def test_a_bad_grant_refuses_the_render(tmp_path: Path) -> None:
    """Rule: the render checks the env's grants before it writes."""
    copy = bootstrap_example_copy(tmp_path)
    _set_app(copy.env_dir, "hello-web", {"version": "v0.3.0", "grants": ["trust"]})
    before = _tree(copy)
    with pytest.raises(SystemExit, match="unknown grant"):
        render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == before
