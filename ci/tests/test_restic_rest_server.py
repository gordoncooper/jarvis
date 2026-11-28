"""Rules from the platform-backups contract for restic-check and the off-host copy: each CronJob as an env
builds it, and its script run over a fake restic."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from jarvis.schema.env import offsite_copy_listed, resolve_cluster_vars
from support import REPO_ROOT as ROOT
from support import copy_sample, flux_built, render_switches, sample_root, set_switches, yaml_docs

# App hosting registers app credentials in restic-check and restic-offsite.
_APPS_AND_OFFSITE = ("app-hosting", "offsite-backup-copy")


def _container(cronjob: dict) -> dict:
    return cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]


def _cronjob(root: Path, name: str) -> dict:
    return next(doc for doc in flux_built(root, "env-00") if doc["kind"] == "CronJob" and doc["metadata"]["name"] == name)


def _run(tmp_path: Path, container: dict, fake: str, repos: list[str], **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """The container's script over repos under tmp_path, fake as restic beside tmp_path/bin's tools: its run and calls."""
    data, bindir = tmp_path / "data", tmp_path / "bin"
    data.mkdir()
    for repo in repos:
        (data / repo).mkdir(parents=True)
    bindir.mkdir(exist_ok=True)
    (bindir / "restic").write_text(fake, encoding="utf-8")
    (bindir / "restic").chmod(0o755)
    values = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    script = container["command"][-1].replace("/repositories", str(data)).replace("/credentials", str(tmp_path / "credentials"))
    proc = subprocess.run(["sh", "-c", script], env={**values, "PATH": f"{bindir}:{os.environ['PATH']}", "LOG": str(tmp_path / "log"), **env},
                          capture_output=True, text=True, check=False)  # fmt: skip
    return proc, (tmp_path / "log").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(("name", "command"), [pytest.param("restic-check", "check", id="restic-check"),
                                               pytest.param("restic-offsite", "copy", id="restic-offsite")])
def test_restic_check_and_offsite_read_repositories_without_a_lock(name: str, command: str) -> None:
    """Rule: restic-check and restic-offsite run the etcd copy's pinned image with a memory cap on the backup node, take no
    local lock, have no API token or capability and mount the repositories read-only: never block, delete, prune or unlock."""
    root = sample_root(*_APPS_AND_OFFSITE)
    pod = _cronjob(root, name)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == _container(yaml_docs(ROOT / "core/platform/backups/etcd-snapshots.yaml")[0])["image"]
    assert re.fullmatch(r"restic/restic:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", container["image"])
    assert container["resources"]["requests"]["memory"] and container["resources"]["limits"]["memory"]
    assert "nodeSelector" not in pod
    assert pod["affinity"] == {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
        {"matchExpressions": [{"key": "node-role.jarvis.io/backup", "operator": "In", "values": ["true"]}]}]}}}
    assert pod["tolerations"] == [{"key": "node-role.jarvis.io/dedicated", "operator": "Equal", "value": "true", "effect": "NoSchedule"}]
    # No API token and no capabilities: root may only read files it owns.
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"] == {"runAsUser": 0, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    assert re.search(rf"\brestic\b[^\n]*--no-lock\b[^\n]*\b{command}\b", container["command"][-1])
    values = resolve_cluster_vars(yaml.safe_load((root / "envs" / "env-00" / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")))
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert [(volume["hostPath"]["path"], mounts[volume["name"]]["readOnly"]) for volume in pod["volumes"] if "hostPath" in volume] == [
        (f"{values['backup_path']}/restic", True)]


# Exits 10 for a missing repository, 1 for a broken one and 12 (a wrong password) for one another credential wrote;
# prints a file name it must not leak, on stdout and stderr.
_FAKE_CHECK = """#!/bin/sh
echo "$2 $5" >> "$LOG"
echo "tree 1: file private-file-name"; echo "tree 1: file private-file-name" >&2
[ -d "$2" ] || exit 10
case "$2" in */pvc-bad) exit 1 ;; */pvc-other) exit 12 ;; esac
"""


@pytest.mark.parametrize(
    ("repos", "code", "lines"),
    [
        pytest.param(["host-etcd-snapshots", "ns-a/pvc-ok", "ns-b/pvc-bad", "ns-c/pvc-other"], 1, [
            "PASS host-etcd-snapshots",
            "PASS ns-a/pvc-ok",
            "FAIL ns-b/pvc-bad: restic check exited 1",
            "FAIL ns-c/pvc-other: core's password does not open it; remove it once its data is not needed (docs/backups.md)",
        ], id="broken-and-foreign"),
        pytest.param(["host-etcd-snapshots"], 0, ["PASS host-etcd-snapshots"], id="no-pvc-repository"),
        pytest.param([], 1, ["FAIL host-etcd-snapshots: restic check exited 10"], id="no-etcd-repository"),
    ],
)  # fmt: skip
def test_restic_check_script_checks_every_repository_and_prints_only_results(
    tmp_path: Path, repos: list[str], code: int, lines: list[str]
) -> None:
    """Rule: every PVC repository and the etcd repository is checked on the week's slice of its data; a missing etcd
    repository fails, an empty PVC glob does not, one core's password doesn't open says so, and no restic output shows."""
    container = _container(yaml_docs(ROOT / "core/platform/backups/restic-check.yaml")[0])
    (tmp_path / "bin").mkdir()
    # A date in week 5 since the epoch, so the slice the week picks is known: 5 % READ_DATA_SLICES + 1.
    (tmp_path / "bin" / "date").write_text(f"#!/bin/sh\necho {5 * 604800}\n", encoding="utf-8")
    (tmp_path / "bin" / "date").chmod(0o755)
    proc, calls = _run(tmp_path, container, _FAKE_CHECK, repos)
    assert (proc.returncode, proc.stdout.splitlines()) == (code, lines)
    assert "private-file-name" not in proc.stdout + proc.stderr
    slices = int(next(item["value"] for item in container["env"] if item["name"] == "READ_DATA_SLICES"))
    assert {call.split()[1] for call in calls} == {f"--read-data-subset={5 % slices + 1}/{slices}"}, calls


# Exits 10 for a repository with no config; logs each call with the passwords it could read.
_FAKE_KEYED = """#!/bin/sh
echo "$* core=${RESTIC_PASSWORD-unset} from=${RESTIC_FROM_PASSWORD-unset}" >> "$LOG"
case "$3" in cat) [ -e "$2/config" ] || exit 10 ;; init) mkdir -p "$2"; touch "$2/config" ;; esac
"""


@pytest.mark.parametrize(
    ("name", "env", "missing"),
    [
        pytest.param("restic-check", {}, "no backup credential registered for it", id="restic-check"),
        pytest.param("restic-offsite", {"RESTIC_FROM_PASSWORD": "core-local"}, "no backup credential registered for its app",
                     id="restic-offsite"),
    ],
)  # fmt: skip
def test_app_repository_opens_only_with_its_own_password(tmp_path: Path, name: str, env: dict[str, str], missing: str) -> None:
    """Rule: with app hosting on, an app repository gets its app's password file and never core's password, core
    repositories keep core's, and an app repository with no registered credential fails alone."""
    key = tmp_path / "credentials" / "app-shop" / "RESTIC_PASSWORD"
    key.parent.mkdir(parents=True)
    key.write_text("throwaway", encoding="utf-8")
    repos = ["host-etcd-snapshots", "ns-a/pvc-x", "ns-app-shop/pvc-db", "ns-app-gone/pvc-db"]
    target = {"RESTIC_REPOSITORY": f"{tmp_path / 'offsite'}/"} if name == "restic-offsite" else {}
    proc, calls = _run(tmp_path, _container(_cronjob(sample_root(*_APPS_AND_OFFSITE), name)), _FAKE_KEYED, repos,
                       RESTIC_PASSWORD="core", **target, **env)  # fmt: skip
    assert (proc.returncode, proc.stdout.splitlines()) == (1, [
        "PASS host-etcd-snapshots", "PASS ns-a/pvc-x", f"FAIL ns-app-gone/pvc-db: {missing}", "PASS ns-app-shop/pvc-db"])
    app_calls = [call for call in calls if "ns-app-shop" in call]
    assert app_calls and all(f"--password-file {key}" in call and call.endswith(" core=unset from=unset") for call in app_calls), calls
    assert not any("ns-app-gone" in call for call in calls), calls
    assert all("--password-file" not in call and " core=core " in call for call in calls if "ns-app-" not in call), calls


@pytest.mark.parametrize("features", [pytest.param(["app-hosting"], id="unlisted"), pytest.param(list(_APPS_AND_OFFSITE), id="listed")])
def test_offsite_copy_renders_only_where_the_env_lists_it(tmp_path: Path, features: list[str]) -> None:
    """Rule: only an env that lists the off-host copy renders restic-offsite, which reads its sealed backups-offsite
    whole, and only there does verify read it as listed; unlisted, the sealed file it keeps renders nothing."""
    env_dir = copy_sample(tmp_path, *_APPS_AND_OFFSITE) / "envs" / "env-00"
    set_switches(env_dir, features=features)
    render_switches(env_dir, tmp_path)
    docs = flux_built(tmp_path, "env-00")
    listed = "offsite-backup-copy" in features
    assert offsite_copy_listed(env_dir) is listed
    offsite = {("CronJob", "restic-offsite"), ("Secret", "backups-offsite")}
    assert {(doc["kind"], doc["metadata"]["name"]) for doc in docs} & offsite == (offsite if listed else set())
    assert [_container(doc)["envFrom"] for doc in docs if doc["metadata"]["name"] == "restic-offsite"] == (
        [[{"secretRef": {"name": "backups-offsite"}}]] if listed else [])


# Exits 10 for a destination with no config and 1 for a copy into */pvc-bad; prints a file name it must not leak, on
# stdout and stderr.
_FAKE_COPY = """#!/bin/sh
echo "$*" >> "$LOG"
echo "tree 1: file private-file-name"; echo "tree 1: file private-file-name" >&2
case "$3" in cat) [ -e "$2/config" ] || exit 10 ;; init) mkdir -p "$2"; touch "$2/config" ;; esac
case "$3 $2" in "--no-lock "*/pvc-bad) exit 1 ;; esac
"""


@pytest.mark.parametrize(("repos", "code", "lines"), [
    pytest.param(["host-etcd-snapshots", "ns-a/pvc-x"], 0, ["PASS host-etcd-snapshots", "PASS ns-a/pvc-x"], id="every-repository"),
    pytest.param(["host-etcd-snapshots", "ns-a/pvc-x", "ns-b/pvc-bad"], 1,
                 ["PASS host-etcd-snapshots", "PASS ns-a/pvc-x", "FAIL ns-b/pvc-bad: restic exited 1"], id="a-copy-fails"),
])  # fmt: skip
def test_offsite_copy_script_inits_and_copies_every_repository(tmp_path: Path, repos: list[str], code: int, lines: list[str]) -> None:
    """Rule: after one probe of the target, each local repository is copied, lock-free, into prefix + its path, a missing
    destination initialised first with the local chunker parameters; one line per repository, and a failed copy fails."""
    offsite, data = tmp_path / "offsite", tmp_path / "data"
    container = _container(yaml_docs(ROOT / "core/feature/offsite-backup-copy/restic-offsite.yaml")[0])
    proc, calls = _run(tmp_path, container, _FAKE_COPY, repos, RESTIC_REPOSITORY=f"{offsite}/")
    assert (proc.returncode, proc.stdout.splitlines()) == (code, lines), proc.stdout
    assert "private-file-name" not in proc.stdout + proc.stderr
    assert calls == [f"--repo {offsite}/probe cat config", *(call for repo in repos for call in (
        f"--repo {offsite}/{repo} cat config",
        f"--repo {offsite}/{repo} init --from-repo {data}/{repo} --copy-chunker-params",
        f"--repo {offsite}/{repo} --no-lock copy --from-repo {data}/{repo}"))]  # fmt: skip


def test_offsite_copy_fails_every_repository_at_once_when_the_target_is_down(tmp_path: Path) -> None:
    """Rule: a target that does not answer the first probe fails each repository with one line, and nothing more runs."""
    container = _container(yaml_docs(ROOT / "core/feature/offsite-backup-copy/restic-offsite.yaml")[0])
    proc, calls = _run(tmp_path, container, '#!/bin/sh\necho "$*" >> "$LOG"\nexit 1\n', ["host-etcd-snapshots", "ns-a/pvc-x"],
                       RESTIC_REPOSITORY="rest:http://down/")  # fmt: skip
    assert (proc.returncode, proc.stdout.splitlines()) == (1, [
        "FAIL host-etcd-snapshots: off-host target not reachable (probe exited 1)",
        "FAIL ns-a/pvc-x: off-host target not reachable (probe exited 1)",
    ])
    assert calls == ["--repo rest:http://down/probe cat config"]
