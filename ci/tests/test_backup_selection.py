"""Backup selection in values/apps.yaml: modes, render, env-schema, and verify behavior."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))
if str(REPO_ROOT / "tools" / "lib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools" / "lib"))

import _backup_selection
from schema.env import ValidationError, validate_env_tree, validate_one_env
from support import ExampleCopy, _kustomize_build, bootstrap_example_copy, register_copy_app, set_switches
from verify.backups import backups_checks, check_backups
from verify.common import CheckError

NOTES = "app-notes"
FIXTURE_APPS = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"


def _write_apps(copy: ExampleCopy, section: dict) -> None:
    path = copy.env_dir / "values" / "apps.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["backups"] = section
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _render_selection(copy: ExampleCopy) -> None:
    from render_backup_selection import render_env

    set_switches(copy.env_dir, optional=["app-hosting", "offsite-copy", "apps-backup-server"])
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


def test_all_except_requires_credential_for_unexcluded_app(tmp_path: Path) -> None:
    """Rule: all_except enables an eligible app that exclude does not name, so its credential is required."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "all_except", "exclude": []})
    _render_selection(copy)
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name)
    assert "bootstrap-secrets APP=app-notes" in "\n".join(exc.value.messages)


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
    path.write_text(path.read_text(encoding="utf-8").replace("app-notes", "app-stale"), encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name)
    assert "stale wrapper" in "\n".join(exc.value.messages)


def test_apps_repo_resolves_checkout_beside_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: without APPS_REPO, eligibility reads the checkout of apps_git_repo_url beside the instance."""
    copy = bootstrap_example_copy(tmp_path)
    values = yaml.safe_load((copy.env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    beside = tmp_path / values["apps_git_repo_url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    shutil.copytree(FIXTURE_APPS, beside)
    monkeypatch.delenv("APPS_REPO", raising=False)
    assert _backup_selection.apps_repo_root(copy.root, copy.env_dir) == beside.resolve()


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
    """Rule: stateless apps cannot appear on the selected list."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": ["hello-web"]})
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name, pre_host=True)
    assert "no backup-eligible volume" in "\n".join(exc.value.messages)


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


def _override_copy(tmp_path: Path, overrides: dict) -> ExampleCopy:
    """A copy of the example env with notes selected and these overrides, rendered as an owner would."""
    copy = bootstrap_example_copy(tmp_path)
    register_copy_app(copy, NOTES)
    _write_apps(copy, {"mode": "selected", "selected": ["notes"], "overrides": overrides})
    _render_selection(copy)
    return copy


def test_overrides_land_on_the_replication_source(tmp_path: Path) -> None:
    """Rule: an app's override reaches its ReplicationSource, one the override leaves out stays the
    env's value, and an enabled app with no override and a core PVC keep every cluster-wide value.
    The objects platform-configs renders, with the kyverno CLI standing in only for the API read."""
    from test_app_admission import _generate_test, _pvc

    copy = _override_copy(tmp_path, {"notes": {"backup_schedule": "15 4 * * *", "backup_retention_daily": 0, "backup_retention_weekly": 9}})
    validate_one_env(copy.root, copy.name)
    rendered = _kustomize_build(f"envs/{copy.name}/flux/platform-configs", root=copy.root)
    docs = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
    data = next(doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "backup-overrides")["data"]
    policy = next(doc for doc in docs if doc["kind"] == "ClusterPolicy" and doc["metadata"]["name"] == "pvc-backup")
    env = yaml.safe_load((copy.env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    text = yaml.safe_dump(policy)
    for key in ("backup_schedule", "backup_retention_daily", "backup_retention_weekly", "backup_retention_monthly"):
        text = text.replace("${" + key + "}", str(env[key]))
    enrollment = tmp_path / "pvc-backup.yaml"
    enrollment.write_text(text, encoding="utf-8")

    def source(claim: dict, schedule: str, daily: int, weekly: int, monthly: int) -> dict:
        name, namespace = claim["metadata"]["name"], claim["metadata"]["namespace"]
        return {"apiVersion": "volsync.backube/v1alpha1", "kind": "ReplicationSource", "metadata": {"name": name, "namespace": namespace},
                "spec": {"paused": False, "sourcePVC": name, "trigger": {"schedule": schedule},
                         "restic": {"repository": f"restic-{name}", "copyMethod": "Direct", "cacheCapacity": "1Gi", "pruneIntervalDays": 7,
                                    "retain": {"daily": daily, "weekly": weekly, "monthly": monthly, "within": "7d"}}}}

    overridden, plain, core = _pvc("data", "notes"), _pvc("data", "web-demo"), _pvc("data", "x")
    core["metadata"]["namespace"] = "monitoring"
    for claim in (overridden, plain, core):
        claim["metadata"]["name"] = f"data-{claim['metadata']['namespace']}"
        claim["spec"]["volumeName"] = "pv-1"
    defaults = (env["backup_schedule"], env["backup_retention_daily"], env["backup_retention_weekly"], env["backup_retention_monthly"])
    # plain reads what a missing ConfigMap gives (the apiCall default); core reads one with no entry for it.
    values = {"policies": [{"name": "pvc-backup", "resources": [
        {"name": claim["metadata"]["name"], "values": {"appCredential": "true", "rebuildHold": "false", "backupOverrides": read}}
        for claim, read in ((overridden, data), (plain, {}), (core, data))
    ]}]}
    namespaces = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": claim["metadata"]["namespace"]}} for claim in (overridden, plain, core)]
    _generate_test(tmp_path, enrollment, [overridden, plain, core, *namespaces], values, [
        ("replication-source-app", (overridden, source(overridden, "15 4 * * *", 0, 9, env["backup_retention_monthly"]))),
        ("replication-source-app", (plain, source(plain, *defaults))),
        ("replication-source", (core, source(core, *defaults))),
    ])


def test_no_override_renders_no_configmap(tmp_path: Path) -> None:
    """Rule: an env with no override renders no overrides ConfigMap, so existing wrappers stay byte-identical."""
    copy = _override_copy(tmp_path, {})
    assert "configMapGenerator" not in (copy.env_dir / _backup_selection.GENERATED_KUSTOMIZATION).read_text(encoding="utf-8")


def test_wrapper_without_the_override_is_stale(tmp_path: Path) -> None:
    """Rule: env-schema refuses a committed wrapper that does not carry the env's overrides."""
    copy = _override_copy(tmp_path, {"notes": {"backup_schedule": "15 4 * * *"}})
    path = copy.env_dir / _backup_selection.GENERATED_KUSTOMIZATION
    path.write_text(path.read_text(encoding="utf-8").replace("15 4", "16 4"), encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name)
    assert "does not match" in "\n".join(exc.value.messages)


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


def test_env_schema_check_refuses_fixture_apps_repo_under_instance(tmp_path: Path) -> None:
    """Rule: ci/checks/env-schema refuses INSTANCE with APPS_REPO pointed at core's own fixtures."""
    instance = _instance_with_backups(tmp_path)
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "ci" / "checks" / "env-schema")],
        cwd=str(REPO_ROOT),
        env={**os.environ, "INSTANCE": str(instance), "APPS_REPO": str(FIXTURE_APPS)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "must not point at core test fixtures" in proc.stderr


_OVERRIDES = {"mode": "selected", "selected": ["notes"]}


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"mode": "bogus"}, "mode must be"),
        ({"mode": "selected", "junk": 1}, "unknown key 'junk'"),
        ({"mode": "selected", "exclude": []}, "exclude is only valid"),
        ({"mode": "all_except", "selected": []}, "selected is only valid"),
        ({"mode": "selected", "selected": 5}, "must be a list"),
        ({"mode": "selected", "selected": "notes"}, "must be a list"),
        ({"mode": "all_except", "exclude": 5}, "must be a list"),
        ({"mode": "all_except", "exclude": [["notes"]]}, "is not an app name"),
        ({"mode": "selected", "selected": ["Bad_Name"]}, "is not an app name"),
        ({"mode": "selected", "selected": ["unlisted"]}, "not on this env's app list"),
        ({"mode": "selected", "selected": ["notes", "notes"]}, "listed twice"),
        ({"mode": ["selected"]}, "mode must be"),
        ({"mode": "selected", 1: "x", "junk": "y"}, "unknown key 'junk'"),
        ({**_OVERRIDES, "overrides": 5}, "must be a mapping of app name"),
        ({**_OVERRIDES, "overrides": {"Bad_Name": {}}}, "not an app name"),
        ({**_OVERRIDES, "overrides": {"unlisted": {}}}, "not on this env's app list"),
        ({**_OVERRIDES, "overrides": {"notes": 5}}, "must be a mapping"),
        ({**_OVERRIDES, "overrides": {"notes": {1: 2, "junk": 3}}}, "unknown key 1"),
        ({**_OVERRIDES, "overrides": {"notes": {"backup_schedule": "daily"}}}, "five-field cron"),
        ({**_OVERRIDES, "overrides": {"notes": {"backup_retention_daily": -1}}}, "non-negative integer"),
    ],
)
def test_env_schema_rejects_bad_selection(tmp_path: Path, section: dict, message: str) -> None:
    """Rule: each malformed backups section is a schema error, never a crash on the way there."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, section)
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name, pre_host=True)
    assert message in "\n".join(exc.value.messages)


def test_env_schema_validates_selection_with_no_apps(tmp_path: Path) -> None:
    """Rule: a backups section is validated even when the env lists no app."""
    copy = bootstrap_example_copy(tmp_path)
    path = copy.env_dir / "values" / "apps.yaml"
    path.write_text(yaml.safe_dump({"backups": {"mode": "bogus"}}), encoding="utf-8")
    set_switches(copy.env_dir, apps=[])
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name, pre_host=True)
    assert "mode must be" in "\n".join(exc.value.messages)


def test_env_schema_rejects_unknown_component_with_selection(tmp_path: Path) -> None:
    """Rule: with backups declared, components holds only app hosting and the generated wrapper."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": []})
    validate_one_env(copy.root, copy.name, pre_host=True)
    path = copy.env_dir / "flux" / "platform-configs" / "kustomization.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["components"].append("../unknown")
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name, pre_host=True)
    assert "'../unknown' is not an app credential registration" in "\n".join(exc.value.messages)


def test_exclusion_never_reads_the_excluded_app_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: an intentional exclusion fails neither env-schema nor verify, whatever its app file now says."""
    apps = tmp_path / "apps-checkout"
    shutil.copytree(FIXTURE_APPS, apps)
    monkeypatch.setenv("APPS_REPO", str(apps))
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "all_except", "exclude": ["notes", "hello-web"]})
    (apps / "notes" / "app.yaml").write_text("spec: [\n", encoding="utf-8")
    validate_one_env(copy.root, copy.name, pre_host=True)
    assert "backup selection" not in _verify_checks(copy)


@pytest.mark.parametrize("damage", ["malformed", "undecodable", "missing"])
def test_unreadable_app_file_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str) -> None:
    """Rule: an app file the checkout cannot answer for fails; it never reads as stateless and drops the backup."""
    apps = tmp_path / "apps-checkout"
    shutil.copytree(FIXTURE_APPS, apps)
    monkeypatch.setenv("APPS_REPO", str(apps))
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "all_except", "exclude": []})
    _render_selection(copy)
    if damage == "malformed":
        (apps / "notes" / "app.yaml").write_text("spec: [\n", encoding="utf-8")
    elif damage == "undecodable":
        (apps / "notes" / "app.yaml").write_bytes(b"\xff\xfe")
    else:
        shutil.rmtree(apps / "notes")
    with pytest.raises(ValidationError) as exc:
        validate_one_env(copy.root, copy.name, pre_host=True)
    assert "app file is missing or unreadable" in "\n".join(exc.value.messages)
    with pytest.raises(_backup_selection.AppsRepoError):
        _render_selection(copy)
    assert (copy.env_dir / "flux" / "platform-configs" / "backups" / NOTES / "kustomization.yaml").is_file()


def _verify_checks(copy: ExampleCopy) -> dict[str, object]:
    ctx = SimpleNamespace(
        repo_root=copy.root, env=copy.name, kubectl=None, values={}, now=datetime.now(timezone.utc)
    )
    return {label: check for _order, label, check in backups_checks(ctx)}


def test_verify_builds_no_selection_failure_for_valid_selection(tmp_path: Path) -> None:
    """Rule: a valid selection adds no failing check to make verify."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": ["notes"]})
    assert "backup selection" not in _verify_checks(copy)


@pytest.mark.parametrize(
    ("section", "message"),
    [({"mode": "selected", "selected": ["notes"]}, "APPS_REPO"), ({"mode": "bogus"}, "mode must be")],
)
def test_verify_reports_unusable_selection_as_a_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: dict, message: str
) -> None:
    """Rule: no apps checkout or a bad section is one failed verify line, and the other checks still run."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, section)
    if message == "APPS_REPO":
        monkeypatch.delenv("APPS_REPO", raising=False)
    checks = _verify_checks(copy)
    assert "backups-restic present" in checks
    with pytest.raises(CheckError, match=message):
        checks["backup selection"]()


def test_render_cli_on_empty_selection_succeeds_and_keeps_env_comments(tmp_path: Path) -> None:
    """Rule: intentionally selecting nothing is success, and a render with nothing to change edits no env file."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": []})
    path = copy.env_dir / "flux" / "platform-configs" / "kustomization.yaml"
    before = path.read_text(encoding="utf-8")
    proc = _render_cli(copy, APPS_REPO=str(FIXTURE_APPS))
    assert proc.returncode == 0, proc.stderr
    assert path.read_text(encoding="utf-8") == before


def test_render_cli_without_checkout_refuses_cleanly(tmp_path: Path) -> None:
    """Rule: render without an apps checkout refuses with the reason, not a traceback."""
    copy = bootstrap_example_copy(tmp_path)
    _write_apps(copy, {"mode": "selected", "selected": ["notes"]})
    proc = _render_cli(copy)
    assert proc.returncode != 0
    assert "APPS_REPO" in proc.stderr
    assert "Traceback" not in proc.stderr


def _render_cli(copy: ExampleCopy, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {key: value for key, value in os.environ.items() if key != "APPS_REPO"}
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "lib" / "render_backup_selection.py"), "--env", copy.name],
        env={**clean, "INSTANCE": str(copy.root), **env},
        capture_output=True,
        text=True,
        check=False,
    )
