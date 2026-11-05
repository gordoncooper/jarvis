"""The jarvis-apps examples (copied under fixtures/app-hosting/jarvis-apps, held equal to the apps
repository by its CI) against jarvis's own tools.

env-00 lists hello-web, notes and lan-demo with lan-demo's exception line, so:
- tools/app-check passes each folder;
- the env's wiring renders, matches what is committed, and env-schema passes it;
- each app's deploy/ objects pass the admission policies with the env's grants;
- lan-demo's home-network Service is refused without the env's line;
- a hostPath edit of an example is refused without a host-path line.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT

import _apps
from _app_file import check_folder
from render_apps import render_env
from schema import env as env_schema
import test_app_admission as admission

EXAMPLES = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"
APPS = ["hello-web", "notes", "lan-demo"]


def _env(tmp_path: Path, *, lan_line: bool = True) -> tuple[Path, Path, dict]:
    """An instance holding env-00, with or without lan-demo's line, its wiring re-rendered."""
    instance = tmp_path / "instance"
    env_dir = instance / "envs" / "env-00"
    shutil.copytree(REPO_ROOT / "envs" / "env-00", env_dir)
    (instance / "core").symlink_to(REPO_ROOT)
    values = {"apps": APPS, "exceptions": [{"lan-demo": "lan-port 8080/TCP"}] if lan_line else []}
    (env_dir / _apps.APPS_VALUES).write_text(yaml.safe_dump(values), encoding="utf-8")
    text = render_env(env_dir)
    assert text is not None
    (env_dir / "flux" / _apps.GENERATED_REL).write_text(text, encoding="utf-8")
    return instance, env_dir, values


def _admission_env(monkeypatch: pytest.MonkeyPatch, values: dict) -> None:
    monkeypatch.setattr(admission, "APPS", EXAMPLES)
    monkeypatch.setattr(admission, "_ENV", {app: (admission._app(app), _apps.grants_for(values, app)) for app in APPS})


@pytest.mark.parametrize("app", APPS)
def test_app_check_passes_each_example(app: str) -> None:
    """Rule: each example app file and its objects pass app-check."""
    assert check_folder(EXAMPLES / app) == []


def test_committed_env_00_render_wires_the_examples_and_passes_env_schema() -> None:
    """Rule: env-00's committed render is the examples' wiring, and env-schema passes it."""
    env_dir = REPO_ROOT / "envs" / "env-00"
    docs = yaml.safe_load_all((env_dir / "flux" / _apps.GENERATED_REL).read_text(encoding="utf-8"))
    subs = {d["spec"]["postBuild"]["substitute"]["app_name"]: d["spec"]["postBuild"]["substitute"] for d in docs if d}
    assert list(subs) == APPS
    assert subs["lan-demo"]["app_grant_lan_ports"] == "8080/TCP"
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    assert env_schema._validate_apps(env_dir, REPO_ROOT, cluster_vars, pre_host=False) == []


def test_examples_pass_admission_with_the_env_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: every example object is admitted with env-00's exception line."""
    _instance, _env_dir, values = _env(tmp_path)
    _admission_env(monkeypatch, values)
    cases = [(doc, admission.PASS) for app in APPS for doc in admission._deploy(app)]
    admission._run(tmp_path, "granted", None, cases)


def test_lan_port_refused_without_the_env_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: a home-network port the app asks for is refused unless the env grants it."""
    _instance, _env_dir, values = _env(tmp_path, lan_line=False)
    _admission_env(monkeypatch, values)
    service = next(doc for doc in admission._deploy("lan-demo") if doc["kind"] == "Service")
    admission._run(tmp_path, "ungranted", None, [(service, {"app-file": "pass", "app-grants": "fail"})])


def test_host_path_refused_without_a_host_path_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: hello-web with a hostPath volume added is refused; the app file does not ask, the env does not grant."""
    _instance, _env_dir, values = _env(tmp_path)
    _admission_env(monkeypatch, values)
    deployment = next(doc for doc in admission._deploy("hello-web") if doc["kind"] == "Deployment")
    deployment["spec"]["template"]["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/srv"}})
    admission._run(tmp_path, "host-path", None, [(deployment, admission.BOTH_FAIL)])
