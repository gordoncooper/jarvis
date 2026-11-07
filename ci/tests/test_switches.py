"""An env's switches file: names render the Flux wiring, and an unknown name or stale wiring fails."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from schema.env import ValidationError, validate_one_env
from support import ExampleCopy, _kustomize_build, bootstrap_example_copy, register_copy_app, render_switches, set_switches
import _switches


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
    rendered = _kustomize_build(f"envs/{copy.name}/flux/{rel}", root=copy.root)
    return {(doc["kind"], doc["metadata"]["name"]) for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)}


def test_a_listed_app_takes_one_name_and_no_other_env_file(tmp_path: Path) -> None:
    """Rule: one line under apps enables an app: only its wiring changes, with no per-env settings file,
    and a second render changes nothing."""
    copy = bootstrap_example_copy(tmp_path)
    set_switches(copy.env_dir, apps=["notes", "lan-demo"])
    render_switches(copy.env_dir, copy.root)
    validate_one_env(copy.root, copy.name)
    before = _tree(copy)
    assert "app-hello-web-wiring" not in before["flux/generated/apps.yaml"]

    set_switches(copy.env_dir, apps=["notes", "lan-demo", "hello-web"])
    render_switches(copy.env_dir, copy.root)
    after = _tree(copy)
    assert "app-hello-web-wiring" in after["flux/generated/apps.yaml"]
    assert {path for path in after if after[path] != before.get(path)} == {
        "values/switches.yaml",
        "flux/generated/apps.yaml",
    }
    validate_one_env(copy.root, copy.name)
    render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == after


@pytest.mark.parametrize(
    ("section", "bad"),
    [("optional", "observability"), ("addons", "nope"), ("apps", "not-in-the-apps-repo")],
)
def test_an_unknown_name_fails_and_refuses_the_render(tmp_path: Path, section: str, bad: str) -> None:
    """Rule: a name core or the apps repository does not hold fails, before the render writes anything."""
    copy = bootstrap_example_copy(tmp_path)
    data = yaml.safe_load((copy.env_dir / _switches.SWITCHES_FILE).read_text(encoding="utf-8"))
    set_switches(copy.env_dir, **{section: [*data[section], bad]})
    assert f"unknown name {bad!r}" in _errors(copy)
    before = _tree(copy)
    with pytest.raises(SystemExit, match=bad):
        render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == before


def test_core_has_no_switch(tmp_path: Path) -> None:
    """Rule: core is on in every env, so a core section is an unknown key rather than a list that reads as a choice."""
    copy = bootstrap_example_copy(tmp_path)
    set_switches(copy.env_dir, core=["traefik"])
    assert "unknown key 'core'" in _errors(copy)


def test_an_unlisted_optional_component_is_not_rendered(tmp_path: Path) -> None:
    """Rule: an optional component is off until named; one name turns it on and brings its objects."""
    copy = bootstrap_example_copy(tmp_path)
    assert ("CronJob", "restic-offsite") in _objects(copy, "platform-configs")
    (copy.env_dir / "values" / "apps.yaml").unlink()
    set_switches(copy.env_dir, optional=[], apps=[])
    render_switches(copy.env_dir, copy.root)
    off = _objects(copy, "platform-configs")
    assert ("CronJob", "restic-offsite") not in off
    assert ("Deployment", "restic-apps") not in off
    assert not any(name.startswith("app-") for _kind, name in off)
    assert not (copy.env_dir / "flux" / "generated" / "apps.yaml").exists()
    set_switches(copy.env_dir, optional=["apps-backup-server"])
    render_switches(copy.env_dir, copy.root)
    assert ("Deployment", "restic-apps") in _objects(copy, "platform-configs")


def test_app_hosting_with_selected_backups_renders_the_apps_server_once(tmp_path: Path) -> None:
    """Rule: env-01's shape renders: the selected app's credential on the server its switch turns on."""
    copy = bootstrap_example_copy(tmp_path)
    register_copy_app(copy, "app-notes")
    validate_one_env(copy.root, copy.name)
    objects = _objects(copy, "platform-configs")
    assert {("Deployment", "restic-apps"), ("Secret", "backups-restic-app-notes")} <= objects


def test_selected_backups_need_the_apps_server_switch(tmp_path: Path) -> None:
    """Rule: the render refuses a backup selection with the apps server off, before it writes."""
    copy = bootstrap_example_copy(tmp_path)
    register_copy_app(copy, "app-notes")
    set_switches(copy.env_dir, optional=["app-hosting", "offsite-copy"])
    before = _tree(copy)
    with pytest.raises(SystemExit, match="apps-backup-server"):
        render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == before


@pytest.mark.parametrize("rel", [_switches.ROOT_REL, _switches.SERVICES_REL, _switches.PLATFORM_CONFIGS_REL])
def test_hand_edited_wiring_fails(tmp_path: Path, rel: str) -> None:
    """Rule: the committed wiring is exactly the render, so a switch cannot be flipped outside the file."""
    copy = bootstrap_example_copy(tmp_path)
    path = copy.env_dir / "flux" / rel
    path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert "does not match values/switches.yaml" in _errors(copy)


def test_an_env_with_a_flux_tree_needs_the_file(tmp_path: Path) -> None:
    """Rule: the switches file is required, so a missing one never reads as nothing enabled."""
    copy = bootstrap_example_copy(tmp_path)
    (copy.env_dir / _switches.SWITCHES_FILE).unlink()
    assert "missing required file" in _errors(copy) and _switches.SWITCHES_FILE in _errors(copy)


def test_app_names_are_refused_in_the_app_file(tmp_path: Path) -> None:
    """Rule: one source of truth: app names live only in the switches file."""
    copy = bootstrap_example_copy(tmp_path)
    path = copy.env_dir / "values" / "apps.yaml"
    path.write_text("apps:\n  - notes\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert f"app names live in {_switches.SWITCHES_FILE}" in _errors(copy)


def test_deleting_the_backups_section_does_not_drop_a_registered_app(tmp_path: Path) -> None:
    """Rule: deleting backups: fails while apps register, so it never reads as turning their backups off."""
    copy = bootstrap_example_copy(tmp_path)
    register_copy_app(copy, "app-notes")
    path = copy.env_dir / "values" / "apps.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["backups"]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert "backups is gone from values/apps.yaml" in _errors(copy)
    before = _tree(copy)
    with pytest.raises(SystemExit, match="backups is gone"):
        render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == before


def test_an_exception_line_for_an_unlisted_app_refuses_the_render(tmp_path: Path) -> None:
    """Rule: the render checks exception lines before it writes, even with no app left to wire."""
    copy = bootstrap_example_copy(tmp_path)
    set_switches(copy.env_dir, apps=[])
    before = _tree(copy)
    with pytest.raises(SystemExit, match="not on this env's app list"):
        render_switches(copy.env_dir, copy.root)
    assert _tree(copy) == before
