"""Backup selection in values/apps.yaml: modes, render, env-schema, and verify behavior."""

from __future__ import annotations

import os
import shutil
import subprocess
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
from schema.env import ValidationError, validate_env_tree, validate_one_env
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
    """Rule: all_except excluding a backup-eligible app does not register it or require its sealed file."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "all_except", "exclude": ["notes"]})
    _render_selection(copy)
    assert not (copy.env_dir / _backup_selection.GENERATED_KUSTOMIZATION).is_file()
    validate_one_env(copy.root, copy.name)


def test_all_except_exclusion_guard_requires_notes_credential_when_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: ignoring all_except exclude leaves notes enabled and env-schema requires its credential."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "all_except", "exclude": ["notes"]})
    real = _backup_selection.enabled_app_names

    def ignore_exclude(section, *, apps_list, apps_root):
        if section.get("mode") == _backup_selection.MODE_ALL_EXCEPT:
            return [app for app in apps_list if _backup_selection.backup_eligible(apps_root, app)]
        return real(section, apps_list=apps_list, apps_root=apps_root)

    monkeypatch.setattr(_backup_selection, "enabled_app_names", ignore_exclude)
    _render_selection(copy)
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name)
    assert "app-notes" in "\n".join(exc.value.messages)


def test_overrides_on_disabled_app_rejected(tmp_path: Path) -> None:
    """Rule: overrides apply only to backup-enabled apps."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(
        copy,
        {
            "mode": "selected",
            "selected": [],
            "overrides": {"notes": {"backup_schedule": "0 4 * * *"}},
        },
    )
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name, pre_host=True)
    assert "overrides apply only to backup-enabled apps" in "\n".join(exc.value.messages)


def test_stale_wrapper_rejected(tmp_path: Path) -> None:
    """Rule: generated backup-selection must list exactly the enabled registration Components."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": ["notes"]})
    _render_selection(copy)
    register_copy_app(copy, NOTES)
    path = copy.env_dir / _backup_selection.GENERATED_KUSTOMIZATION
    path.write_text(path.read_text(encoding="utf-8").replace("app-notes", "app-shop"), encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name)
    assert "stale wrapper" in "\n".join(exc.value.messages)


def test_eligibility_reads_apps_repo_not_product_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: backup eligibility comes from APPS_REPO / apps_git_repo_url checkout, not jarvis fixtures by default."""
    apps = tmp_path / "apps-checkout"
    (apps / "only-app").mkdir(parents=True)
    (apps / "only-app" / "app.yaml").write_text(
        "spec:\n  volumes:\n  - name: data\n    backup: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("APPS_REPO", str(apps))
    assert _backup_selection.backup_eligible(apps, "only-app")
    assert not _backup_selection.backup_eligible(apps, "notes")


def test_verify_skips_excluded_eligible_app_pvc() -> None:
    """Rule: verify does not fail an excluded backup-eligible app for a missing credential."""
    import json
    from datetime import datetime, timezone

    def fake(cmd, **_k):
        if "pvc" in cmd:
            return type("P", (), {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"namespace": NOTES, "name": "data", "annotations": {}},
                                "spec": {"volumeName": "pv-1"},
                            }
                        ]
                    }
                ),
                "stderr": "",
            })()
        if "replicationsource" in cmd:
            return type("P", (), {"returncode": 0, "stdout": '{"items": []}', "stderr": ""})()
        if "etcd-snapshots" in cmd:
            return type("P", (), {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "spec": {"schedule": "5 */12 * * *", "timeZone": "UTC"},
                        "status": {"lastSuccessfulTime": "2020-01-02T11:00:00Z"},
                    }
                ),
                "stderr": "",
            })()
        if "backup-rebuild-hold" in cmd:
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "not found"})()
        raise AssertionError(cmd)

    check_backups(
        fake,
        zone_name="UTC",
        now=datetime(2020, 1, 2, tzinfo=timezone.utc),
        registered=frozenset(),
        required=frozenset(),
    )


def test_verify_guard_fails_when_excluded_app_still_required() -> None:
    """Rule: treating an excluded app as required fails verify on its PVC."""
    import json
    from datetime import datetime, timezone

    def fake(cmd, **_k):
        if "pvc" in cmd:
            return type("P", (), {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"namespace": NOTES, "name": "data", "annotations": {}},
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
            now=datetime(2020, 1, 1, tzinfo=timezone.utc),
            registered=frozenset(),
            required=frozenset({NOTES}),
        )


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


def _instance_with_backups(tmp_path: Path) -> Path:
    """Instance checkout (core → product) with env-00 copy and backups all_except excluding notes."""
    product, instance = tmp_path / "product", tmp_path / "instance"
    for name in ("platform", "services", "apps"):
        shutil.copytree(REPO_ROOT / name, product / name)
    (instance / "envs").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "envs" / "env-00", instance / "envs" / "env-00")
    (instance / "core").symlink_to(product)
    apps_path = instance / "envs" / "env-00" / "values" / "apps.yaml"
    data = yaml.safe_load(apps_path.read_text(encoding="utf-8"))
    data["backups"] = {"mode": "all_except", "exclude": ["notes"]}
    apps_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return instance


def test_m9_apps_repo_resolver_fails_without_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: without APPS_REPO or a local apps_git_repo_url checkout, eligibility resolution fails."""
    copy = bootstrap_example_copy(tmp_path)
    monkeypatch.delenv("APPS_REPO", raising=False)
    with pytest.raises(_backup_selection.AppsRepoError, match="APPS_REPO"):
        _backup_selection.apps_repo_root(copy.root, copy.env_dir)


def test_m10_rejects_invalid_apps_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: APPS_REPO must point at a directory of app folders with app.yaml files."""
    copy = bootstrap_example_copy(tmp_path)
    bad = tmp_path / "not-apps"
    bad.mkdir()
    monkeypatch.setenv("APPS_REPO", str(bad))
    with pytest.raises(_backup_selection.AppsRepoError, match="not an apps repository"):
        _backup_selection.apps_repo_root(copy.root, copy.env_dir)


def test_m7b_wrapper_rejected_when_no_app_enabled(tmp_path: Path) -> None:
    """Rule: generated backup-selection must not remain when no app is backup-enabled."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "all_except", "exclude": ["notes"]})
    _render_selection(copy)
    wrapper = copy.env_dir / _backup_selection.GENERATED_KUSTOMIZATION
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\ncomponents: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name)
    assert "no app is backup-enabled" in "\n".join(exc.value.messages)


def test_instance_env_schema_without_apps_repo_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: INSTANCE validation with backups must not use core fixtures or pass without APPS_REPO."""
    instance = _instance_with_backups(tmp_path)
    monkeypatch.setenv("INSTANCE", str(instance))
    monkeypatch.delenv("APPS_REPO", raising=False)
    with pytest.raises(ValidationError) as exc:
        validate_env_tree(instance)
    joined = "\n".join(exc.value.messages).lower()
    assert "apps_repo" in joined or "checkout" in joined or "apps_git_repo_url" in joined


def test_env_schema_check_fails_under_instance_without_apps_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule: ci/checks/env-schema refuses INSTANCE without a real APPS_REPO."""
    instance = _instance_with_backups(tmp_path)
    env = os.environ.copy()
    env.pop("APPS_REPO", None)
    env["INSTANCE"] = str(instance)
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "ci" / "checks" / "env-schema")],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "APPS_REPO" in proc.stdout + proc.stderr
