"""The jarvis-apps examples env-00 lists (a snapshot under fixtures/app-hosting/jarvis-apps; the
apps repository owns them and checks its own with the app-check Action) against jarvis's own tools.

env-00 lists hello-web, notes and lan-demo (a chart app) with lan-demo's grant, so:
- jarvis/apps/check.py passes each folder;
- the env's wiring renders, matches what is committed, and env-schema passes it;
- each app's objects (deploy/, or the chart's) pass the admission policies with the env's grants;
- lan-demo's LAN Service is refused without the env's line, and so are env values for its chart
  that ask for the host's network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import test_app_admission as admission
import yaml

from jarvis.apps import values as _apps
from jarvis.apps.app_file import chart_objects, check_folder
from jarvis.render.apps import render_env
from jarvis.schema import env as env_schema
from support import REPO_ROOT, copy_sample, sample_root

EXAMPLES = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"
APPS = ["hello-web", "notes", "lan-demo"]


def _env(tmp_path: Path, *, lan_line: bool = True) -> tuple[Path, Path, dict]:
    """An instance holding env-00, with or without lan-demo's grant, its wiring re-rendered."""
    instance = tmp_path / "instance"
    env_dir = copy_sample(instance) / "envs" / "env-00"
    values = {app: {"version": "v0.1.0"} for app in APPS}
    if lan_line:
        values["lan-demo"]["grants"] = ["lan-port 8080/TCP"]
    (env_dir / _apps.APPS_VALUES).write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    text = render_env(env_dir)
    assert text is not None
    (env_dir / "flux" / _apps.GENERATED_REL).write_text(text, encoding="utf-8")
    return instance, env_dir, values


def _objects(app: str) -> list[dict]:
    """An example's objects as the cluster gets them: deploy/'s, or its chart's."""
    chart = yaml.safe_load((EXAMPLES / app / "app.yaml").read_text(encoding="utf-8"))["spec"].get("chart")
    if not chart:
        return admission._deploy(app)
    docs, error = chart_objects(app, chart)
    assert error is None, error
    return docs


def _admission_env(monkeypatch: pytest.MonkeyPatch, values: dict) -> None:
    monkeypatch.setattr(admission, "APPS", EXAMPLES)
    monkeypatch.setattr(admission, "_ENV", {app: (admission._app(app), _apps.grants_for(values, app)) for app in APPS})


@pytest.mark.parametrize("app", APPS)
def test_app_check_passes_each_example(app: str) -> None:
    """Rule: each example app file and its objects pass app-check."""
    assert check_folder(EXAMPLES / app) == []


def test_committed_env_00_render_wires_the_examples_and_passes_env_schema() -> None:
    """Rule: env-00's committed render is the examples' wiring, and env-schema passes it."""
    env_dir = sample_root() / "envs" / "env-00"
    docs = yaml.safe_load_all((env_dir / "flux" / _apps.GENERATED_REL).read_text(encoding="utf-8"))
    docs = [doc for doc in docs if doc]
    subs = {d["spec"]["postBuild"]["substitute"]["app_name"]: d["spec"]["postBuild"]["substitute"] for d in docs if d["kind"] == "Kustomization"}
    assert list(subs) == APPS
    assert subs["lan-demo"]["app_grant_lan_ports"] == "8080/TCP"
    release = next(d["spec"] for d in docs if d["kind"] == "HelmRelease")
    assert release["serviceAccountName"] == release["targetNamespace"] == release["storageNamespace"] == "app-lan-demo"
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    assert env_schema._apps_problems(env_dir, sample_root(), cluster_vars, True) == []


def test_examples_pass_admission_with_the_env_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: every example object is admitted with env-00's grant, once app-priority has set its
    class (the examples set none)."""
    _instance, _env_dir, values = _env(tmp_path)
    _admission_env(monkeypatch, values)
    docs = [doc for app in APPS for doc in _objects(app)]
    cases = [(doc, admission.PASS) for doc in admission._mutated(tmp_path, docs)]
    admission._run(tmp_path, "granted", None, cases)


def test_lan_port_refused_without_the_env_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: a LAN port the app asks for is refused unless the env grants it."""
    _instance, _env_dir, values = _env(tmp_path, lan_line=False)
    _admission_env(monkeypatch, values)
    service = next(doc for doc in _objects("lan-demo") if doc["kind"] == "Service")
    admission._run(tmp_path, "ungranted", None, [(service, {"app-file": "pass", "app-grants": "fail"})])


def test_env_values_for_a_chart_are_held_to_the_app_rules(tmp_path: Path) -> None:
    """Rule: the env's values for lan-demo's chart render on top of the app's, and env-schema refuses
    the chart's output when they ask for the host's network, which the app file does not."""
    _instance, env_dir, values = _env(tmp_path)
    values["lan-demo"]["values"] = {"defaultPodOptions": {"hostNetwork": True}}
    (env_dir / _apps.APPS_VALUES).write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    cluster_vars = yaml.safe_load((env_dir / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    problems = env_schema.apps_problems(env_dir, env_dir.parent.parent, cluster_vars)
    assert len(problems) == 1 and "Deployment lan-demo: uses host network" in problems[0], problems
