"""An env's switches file: names render the Flux wiring, and an unknown name or stale wiring fails."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jarvis.env import switches as _switches
from jarvis.schema.env import ValidationError, validate_one_env
from support import ExampleCopy, _kustomize_build, copy_sample, flux_built, render_switches


def _copy(tmp_path: Path, *features: str) -> ExampleCopy:
    """A copy of the sample env-00 with features on (none: core only), at tmp_path."""
    return ExampleCopy(copy_sample(tmp_path, *features), "env-00")


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


def _edit(env_dir: Path, values: str, change: dict) -> None:
    """values/<values>.yaml with change made to its top-level keys, as an owner edits it; None removes a key."""
    path = env_dir / "values" / f"{values}.yaml"
    data = {**(yaml.safe_load(path.read_text(encoding="utf-8")) or {}), **change}
    path.write_text(yaml.safe_dump({key: value for key, value in data.items() if value is not None}, sort_keys=False), encoding="utf-8")


def test_a_listed_app_takes_its_name_and_its_pin(tmp_path: Path) -> None:
    """Rule: an app's entry in values/apps.yaml, its name and pinned version, enables it: only that
    file and its wiring change, and a second render changes nothing."""
    copy = _copy(tmp_path, "app-hosting")
    _edit(copy.env_dir, "apps", {"hello-web": None})
    render_switches(copy.env_dir, copy.root)
    validate_one_env(copy.root, copy.name)
    before = _tree(copy)
    assert "app-hello-web-wiring" not in before["flux/generated/apps.yaml"]

    _edit(copy.env_dir, "apps", {"hello-web": {"version": "v0.3.0"}})
    render_switches(copy.env_dir, copy.root)
    after = _tree(copy)
    assert "app-hello-web-wiring" in after["flux/generated/apps.yaml"]
    assert "refs/tags/hello-web/v0.3.0" in after["flux/generated/apps.yaml"]
    assert {path for path in after if after[path] != before.get(path)} == {"values/apps.yaml", "flux/generated/apps.yaml"}
    validate_one_env(copy.root, copy.name)
    render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == after


@pytest.mark.parametrize(
    ("features", "values", "change", "problem"),
    [
        pytest.param(("app-hosting",), "switches", {"features": ["app-hosting", "observability"]},
                     f"features: unknown name 'observability' (known: {', '.join(_switches.FEATURE_COMPONENTS)})", id="unknown-feature"),
        pytest.param((), "switches", {"addons": ["nope"]}, "addons: unknown name 'nope' (known: hello)", id="unknown-addon"),
        pytest.param((), "switches", {"apps": ["notes"]}, "(top level): Additional properties are not allowed ('apps' was unexpected)",
                     id="apps-listed"),
        pytest.param(("app-hosting",), "apps", {"hello-web": {"version": "v0.3.0", "grants": ["trust"]}},
                     "hello-web: grants: trust: unknown grant (known: privileged, root, host-network, host-path, lan-all,"
                     " hostname <value>, lan-port <value>, lan-target <value>)", id="unknown-grant"),
    ],
)  # fmt: skip
def test_an_unknown_name_fails_and_refuses_the_render(tmp_path: Path, features: tuple, values: str, change: dict, problem: str) -> None:
    """Rule: validate refuses a feature or add-on core does not hold, app names in the switches (the apps an env runs are
    values/apps.yaml's entries only) and an unknown grant; the render refuses each with that message before it writes."""
    copy = _copy(tmp_path, *features)
    _edit(copy.env_dir, values, change)
    message = f"envs/env-00/values/{values}.yaml: {problem}"
    with pytest.raises(ValidationError) as caught:
        validate_one_env(copy.root, copy.name)
    before = _tree(copy)
    with pytest.raises(SystemExit) as refused:
        render_switches(copy.env_dir, copy.root)
    assert (caught.value.messages, refused.value.code, _tree(copy)) == ([message], message, before)


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
    copy = _copy(tmp_path, "app-hosting", "offsite-backup-copy")
    assert ("CronJob", "restic-offsite") in _objects(copy, "platform-configs")
    (copy.env_dir / "values" / "apps.yaml").unlink()
    _edit(copy.env_dir, "switches", {"features": []})
    render_switches(copy.env_dir, copy.root)
    off = _objects(copy, "platform-configs")
    assert ("CronJob", "restic-offsite") not in off
    assert ("Deployment", "restic-apps") not in off
    assert not any(name.startswith("app-") for _kind, name in off)
    assert not (copy.env_dir / "flux" / "generated" / "apps.yaml").exists()


def test_a_sealed_app_credential_registers_on_the_apps_server(tmp_path: Path) -> None:
    """Rule: app-hosting brings the apps server, and each app with backups on and a sealed credential registers on it
    (notes, whose volume jarvis secrets minted one for); an app without one registers nothing."""
    copy = _copy(tmp_path, "app-hosting")
    validate_one_env(copy.root, copy.name)
    objects = _objects(copy, "platform-configs")
    assert {("Deployment", "restic-apps"), ("Secret", "backups-restic-app-notes")} <= objects
    assert not any(name.startswith("backups-restic-app-") and name != "backups-restic-app-notes" for _kind, name in objects)
