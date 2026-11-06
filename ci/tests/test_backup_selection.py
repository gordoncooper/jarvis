"""Backup selection in values/apps.yaml: modes, render, env-schema, and verify behavior."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))
if str(REPO_ROOT / "tools" / "lib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools" / "lib"))

import _backup_selection
from schema.env import ValidationError, validate_one_env
from support import ExampleCopy, bootstrap_example_copy, register_copy_app
from verify.backups import check_backups
from verify.common import CheckError

NOTES = "app-notes"
SHOP = "app-shop"


def _write_apps(copy: ExampleCopy, section: dict) -> None:
    path = copy.env_dir / "values" / "apps.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["backups"] = section
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _render_selection(copy: ExampleCopy) -> None:
    from render_backup_selection import render_env

    render_env(copy.env_dir, copy.root)


def test_selected_mode_requires_credential_for_eligible_app(tmp_path: Path) -> None:
    """Rule: a selected backup-eligible app without its sealed credential fails env-schema once bootstrapped."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": ["notes"]})
    _render_selection(copy)
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name)
    assert "app-notes" in "\n".join(exc.value.messages)
    assert "bootstrap-secrets APP=app-notes" in "\n".join(exc.value.messages)


def test_excluded_app_does_not_require_credential(tmp_path: Path) -> None:
    """Rule: all_except exclusions are not backup-enabled; env-schema does not require their sealed file."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "all_except", "exclude": ["hello-web", "lan-demo"]})
    _render_selection(copy)
    register_copy_app(copy, NOTES)
    validate_one_env(copy.root, copy.name)


def test_selected_ineligible_app_rejected(tmp_path: Path) -> None:
    """Rule: stateless apps cannot appear on selected or exclude lists."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": ["hello-web"]})
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name, pre_host=True)
    assert "no backup-eligible volume" in "\n".join(exc.value.messages)


def test_verify_selected_eligible_without_credential_fails() -> None:
    """Rule: a required app namespace without a credential fails verify visibly."""

    def fake(cmd, **_k):
        if "pvc" in cmd:
            return type("P", (), {
                "returncode": 0,
                "stdout": __import__("json").dumps(
                    {
                        "items": [
                            {
                                "metadata": {"namespace": SHOP, "name": "db", "annotations": {}},
                                "spec": {"volumeName": "pv-1"},
                            }
                        ]
                    }
                ),
                "stderr": "",
            })()
        if "replicationsource" in cmd:
            return type("P", (), {"returncode": 0, "stdout": '{"items": []}', "stderr": ""})()
        raise AssertionError(cmd)

    with pytest.raises(CheckError, match="no registered backup credential"):
        check_backups(
            fake,
            zone_name="UTC",
            now=__import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc),
            registered=frozenset(),
            required=frozenset({SHOP}),
        )


def test_render_writes_registration_components(tmp_path: Path) -> None:
    """Rule: render generates the registration Component and wrapper from selection intent."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": ["notes"]})
    _render_selection(copy)
    component = copy.env_dir / "flux" / "platform-configs" / "backups" / NOTES / "kustomization.yaml"
    assert component.is_file()
    wrapper = copy.env_dir / _backup_selection.GENERATED_KUSTOMIZATION
    assert wrapper.is_file()
    assert "app-notes" in wrapper.read_text(encoding="utf-8")
