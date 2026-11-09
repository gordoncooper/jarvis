"""Rules from the platform-backups contract for the rest-server and etcd copy."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from support import REPO_ROOT as ROOT
from support import ExampleCopy, _kustomize_build, real_and_copy_envs

_FLAGS = ("--append-only", "--no-auth", "--private-repos")
_DEDICATED_TOLERATION = {
    "key": "node-role.jarvis.io/dedicated",
    "operator": "Equal",
    "value": "true",
    "effect": "NoSchedule",
}


def _assert_dedicated_placement(pod: dict, role: str) -> None:
    """Required affinity matches the role label, and tolerates one taint."""
    assert "nodeSelector" not in pod
    required = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert required["nodeSelectorTerms"] == [
        {
            "matchExpressions": [
                {
                    "key": f"node-role.jarvis.io/{role}",
                    "operator": "In",
                    "values": ["true"],
                }
            ]
        },
    ]
    assert pod["tolerations"] == [_DEDICATED_TOLERATION]


def _docs(path: Path) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def test_rest_server_contract_rules() -> None:
    docs: list[dict] = []
    for path in [*(ROOT / "core/platform/backups").rglob("*.yaml"), *(ROOT / "core/feature").rglob("*.yaml")]:
        docs.extend(_docs(path))
    assert all(doc.get("kind") != "Ingress" for doc in docs)

    namespace = next(
        doc
        for doc in docs
        if doc.get("kind") == "Namespace" and doc["metadata"]["name"] == "platform-backups"
    )
    annotations = namespace["metadata"].get("annotations") or {}
    assert "volsync.backube/privileged-movers" not in annotations

    deploy = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "restic"
    )
    pod = deploy["spec"]["template"]["spec"]
    _assert_dedicated_placement(pod, "backup")
    container = pod["containers"][0]
    assert re.fullmatch(
        r"restic/rest-server:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", container["image"]
    )
    rendered = "\n".join(
        [
            *(container.get("command") or []),
            *(container.get("args") or []),
            *(
                f"{item.get('name')}={item.get('value')}"
                for item in container.get("env") or []
            ),
        ]
    )
    for flag in _FLAGS:
        assert flag not in rendered


def test_volsync_controller_tz_comes_from_time_zone() -> None:
    text = (ROOT / "core/platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
    assert "name: TZ" in text
    assert "value: ${time_zone}" in text
    assert "mountPath: /usr/share/zoneinfo" in text
    assert "path: /usr/share/zoneinfo" in text
    resources = _docs(ROOT / "core/platform/backups/controller/helmrelease.yaml")[0]["spec"]["values"][
        "resources"
    ]
    assert resources["limits"]["cpu"] is None
    assert resources["limits"]["memory"]
    assert resources["requests"]["memory"]


def test_renovate_tracks_the_rest_server_digest() -> None:
    image_line = next(
        line.strip()
        for line in (ROOT / "core/platform/backups/rest-server.yaml").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("image:")
    )
    assert re.fullmatch(
        r"image: restic/rest-server:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", image_line
    )
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("rest-server" in pattern for pattern in manager.get("managerFilePatterns", []))
    ]
    assert len(managers) == 1
    manager = managers[0]
    assert manager["datasourceTemplate"] == "docker"
    assert any(
        "restic/rest-server" in pattern
        and "currentDigest" in pattern
        and "currentValue" in pattern
        for pattern in manager["matchStrings"]
    )


def _cron_fields(expr: str) -> list[str]:
    fields = expr.split()
    assert len(fields) == 5, expr
    return fields


def test_etcd_snapshot_cronjob_copies_hourly_from_the_k3s_directory() -> None:
    """The copy runs hourly on its own clock, reads the k3s snapshot directory, makes a restic
    snapshot only when that directory changed, prunes only when forget removed one, and keeps one
    restic group."""
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/k3s/defaults/main.yml").read_text(encoding="utf-8")
    )
    assert _cron_fields(defaults["k3s_etcd_snapshot_schedule_cron"])
    k3s_config = (ROOT / "infra/ansible/roles/k3s/templates/config.yaml.j2").read_text(
        encoding="utf-8"
    )

    docs: list[dict] = []
    for path in sorted((ROOT / "core/platform/backups").rglob("*.yaml")):
        docs.extend(_docs(path))
    snapshot_path = "${backup_path}/etcd-snapshots"
    job = next(
        doc
        for doc in docs
        if doc.get("kind") == "CronJob"
        and any(
            vol.get("hostPath", {}).get("path") == snapshot_path
            for vol in doc["spec"]["jobTemplate"]["spec"]["template"]["spec"].get("volumes")
            or []
        )
    )
    assert f"etcd-snapshot-dir: {snapshot_path.replace('${backup_path}', '{{ backup_path }}')}" in k3s_config

    spec = job["spec"]
    assert spec["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 21600
    assert spec["timeZone"] == "${time_zone}"
    schedule = _cron_fields(spec["schedule"])
    assert schedule[0].isdigit() and schedule[1:] == ["*", "*", "*", "*"]

    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    _assert_dedicated_placement(pod, "control_plane")
    container = pod["containers"][0]
    assert re.fullmatch(r"restic/restic:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", container["image"])
    volume = next(item for item in pod["volumes"] if item.get("hostPath", {}).get("path") == snapshot_path)
    mount = next(item for item in container["volumeMounts"] if item["name"] == volume["name"])
    assert mount["readOnly"] is True

    script = "\n".join(str(part) for part in container["command"])
    assert "restic backup --skip-if-unchanged /snapshots" in script
    assert "restic forget --prune" in script and "restic prune" not in script
    forget = script.split("restic forget", 1)[1]
    for key in (
        "backup_retention_daily",
        "backup_retention_weekly",
        "backup_retention_monthly",
    ):
        assert f"${{{key}}}" in forget

    env = {item["name"]: item for item in container["env"]}
    prefix = env["RESTIC_REPOSITORY_PREFIX"]["valueFrom"]["secretKeyRef"]
    assert (prefix["name"], prefix["key"]) == ("backups-restic", "RESTIC_REPOSITORY")
    assert env["RESTIC_REPOSITORY"]["value"] == "$(RESTIC_REPOSITORY_PREFIX)host-etcd-snapshots"
    host = env["RESTIC_HOST"]["value"]
    assert isinstance(host, str) and host and "$" not in host

    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any(
            "etcd-snapshots" in pattern and "config" not in pattern
            for pattern in manager.get("managerFilePatterns", [])
        )
    ]
    assert len(managers) == 1
    assert managers[0]["datasourceTemplate"] == "docker"
    assert any(
        "restic/restic" in pattern and "currentDigest" in pattern and "currentValue" in pattern
        for pattern in managers[0]["matchStrings"]
    )


def _etcd_kubectl(image: str):
    from jarvis.lib.etcd_image import etcd_snapshot_image

    body = {
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {"spec": {"containers": [{"image": image}]}},
                }
            }
        }
    }

    def kubectl(args: list[str]) -> CompletedProcess[str]:
        return CompletedProcess(args, 0, json.dumps(body), "")

    return etcd_snapshot_image, kubectl


def test_versioned_etcd_image_is_accepted() -> None:
    """restore and drill accept a release tag or a digest-only restic image."""
    digest = "sha256:" + ("ab" * 32)
    for image in (
        f"restic/restic:0.19.1@{digest}",
        f"restic/restic:v0.19.1@{digest}",
        f"restic/restic@{digest}",
    ):
        read, kubectl = _etcd_kubectl(image)
        assert read(kubectl) == image
    committed = next(
        line.strip().removeprefix("image:").strip()
        for line in (ROOT / "core/platform/backups/etcd-snapshots.yaml")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip().startswith("image:") and "restic/restic" in line
    )
    read, kubectl = _etcd_kubectl(committed)
    assert read(kubectl) == committed


@pytest.mark.parametrize("tag", ["latest", "edge", "Latest", "0.18"])
def test_nonrelease_etcd_tag_is_rejected(tag: str) -> None:
    """latest, edge, Latest, and a partial tag are not a restic release."""
    image = f"restic/restic:{tag}@sha256:" + ("ab" * 32)
    read, kubectl = _etcd_kubectl(image)
    from jarvis.lib.etcd_image import ImageError

    with pytest.raises(ImageError, match="must be restic/restic@sha256:"):
        read(kubectl)


def _restic_check(root: Path, env: str) -> dict:
    build = _kustomize_build(f"envs/{env}/flux/platform-configs", root=root)
    return next(
        doc
        for doc in yaml.safe_load_all(build)
        if isinstance(doc, dict)
        and doc.get("kind") == "CronJob"
        and doc["metadata"]["name"] == "restic-check"
    )


def test_restic_check_reads_repositories_without_a_lock(example_copy: ExampleCopy) -> None:
    """The check takes no lock and mounts the repositories read-only, so it never blocks,
    deletes, prunes or unlocks anything; it has no API token or capabilities, and runs the
    etcd copy's pinned image with a memory cap."""
    etcd = _docs(ROOT / "core/platform/backups/etcd-snapshots.yaml")[0]
    etcd_image = etcd["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
    for root, env in real_and_copy_envs(example_copy):
        pod = _restic_check(root, env)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert container["image"] == etcd_image
        assert re.fullmatch(r"restic/restic:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}", container["image"])
        assert container["resources"]["requests"]["memory"]
        assert container["resources"]["limits"]["memory"]
        _assert_dedicated_placement(pod, "backup")
        # No API token and no capabilities: root may only read files it owns.
        assert pod["automountServiceAccountToken"] is False
        security = container["securityContext"]
        assert security["capabilities"]["drop"] == ["ALL"]
        assert "add" not in security["capabilities"]
        assert security["allowPrivilegeEscalation"] is False
        assert re.search(r"\brestic\b[^\n]*--no-lock\b[^\n]*\bcheck\b", container["command"][-1])
        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        for volume in pod["volumes"]:
            if "hostPath" in volume:
                assert volume["hostPath"]["path"] == "${backup_path}/restic"
                assert mounts[volume["name"]]["readOnly"] is True


# Exits 10 for a missing repository and 1 for one marked broken; prints a file name it must not leak.
_FAKE_CHECK = """#!/bin/sh
echo "$2 $5" >> "$LOG"
echo "tree 1: file private-file-name"
[ -d "$2" ] || exit 10
[ ! -e "$2/broken" ] || exit 1
"""


@pytest.mark.parametrize(
    ("repos", "code", "lines"),
    [
        (
            ["host-etcd-snapshots", "ns-a/pvc-ok", "ns-b/pvc-bad"],
            1,
            [
                "PASS host-etcd-snapshots",
                "PASS ns-a/pvc-ok",
                "FAIL ns-b/pvc-bad: restic check exited 1",
            ],
        ),
        (["host-etcd-snapshots"], 0, ["PASS host-etcd-snapshots"]),
        ([], 1, ["FAIL host-etcd-snapshots: restic check exited 10"]),
    ],
)
def test_restic_check_script_checks_every_repository_and_prints_only_results(
    tmp_path: Path, repos: list[str], code: int, lines: list[str]
) -> None:
    """Every PVC repository and the etcd repository is checked on a rotating slice; a missing
    etcd repository fails, an empty PVC glob does not, and restic's own output never reaches the log."""
    data, bindir = tmp_path / "data", tmp_path / "bin"
    bindir.mkdir()
    data.mkdir()
    for repo in repos:
        (data / repo).mkdir(parents=True)
    if "ns-b/pvc-bad" in repos:
        (data / "ns-b/pvc-bad/broken").touch()
    (bindir / "restic").write_text(_FAKE_CHECK, encoding="utf-8")
    (bindir / "restic").chmod(0o755)
    job = _docs(ROOT / "core/platform/backups/restic-check.yaml")[0]["spec"]["jobTemplate"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    env ={item["name"]: item["value"] for item in container["env"] if "value" in item}
    env.update(PATH=f"{bindir}:{os.environ['PATH']}", LOG=str(tmp_path / "log"))
    script = container["command"][-1].replace("/repositories", str(data))
    proc = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == code
    assert proc.stdout.splitlines() == lines
    assert "private-file-name" not in proc.stdout + proc.stderr
    subsets = {line.split()[1] for line in (tmp_path / "log").read_text().splitlines()}
    assert len(subsets) == 1
    slice_, total = subsets.pop().removeprefix("--read-data-subset=").split("/")
    assert 1 <= int(slice_) <= int(total) == int(env["READ_DATA_SLICES"])


_FAKE_KEYED = """#!/bin/sh
echo "$* core=${RESTIC_PASSWORD-unset} from=${RESTIC_FROM_PASSWORD-unset}" >> "$LOG"
case "$3" in cat) [ -e "$2/config" ] || exit 10 ;; init) mkdir -p "$2"; touch "$2/config" ;; esac
"""


def _run_keyed(tmp_path: Path, manifest: str, extra_env: dict[str, str]) -> tuple[CompletedProcess[str], list[str]]:
    """Runs a CronJob script over one core and two app repositories; only app-shop is registered."""
    data, bindir, creds = tmp_path / "data", tmp_path / "bin", tmp_path / "credentials"
    bindir.mkdir()
    for repo in ("host-etcd-snapshots", "ns-a/pvc-x", "ns-app-shop/pvc-db", "ns-app-gone/pvc-db"):
        (data / repo).mkdir(parents=True)
    (creds / "app-shop").mkdir(parents=True)
    (creds / "app-shop" / "RESTIC_PASSWORD").write_text("throwaway", encoding="utf-8")
    (bindir / "restic").write_text(_FAKE_KEYED, encoding="utf-8")
    (bindir / "restic").chmod(0o755)
    container = _docs(ROOT / manifest)[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    env.update(PATH=f"{bindir}:{os.environ['PATH']}", LOG=str(tmp_path / "log"), RESTIC_PASSWORD="core", **extra_env)
    script = container["command"][-1].replace("/repositories", str(data)).replace("/credentials", str(creds))
    proc = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=False)
    return proc, (tmp_path / "log").read_text().splitlines()


@pytest.mark.parametrize(
    ("manifest", "extra_env"),
    [
        ("core/platform/backups/restic-check.yaml", {}),
        ("core/feature/offsite-backup-copy/restic-offsite.yaml", {"RESTIC_FROM_PASSWORD": "core-local"}),
    ],
)
def test_app_repository_opens_only_with_its_own_password(
    tmp_path: Path, manifest: str, extra_env: dict[str, str]
) -> None:
    """An app repository gets that app's password file and never core's password; core
    repositories keep core's; an app repository with no registered credential fails alone."""
    if "offsite" in manifest:
        extra_env = {**extra_env, "RESTIC_REPOSITORY": f"{tmp_path / 'offsite'}/"}
    proc, calls = _run_keyed(tmp_path, manifest, extra_env)
    assert proc.returncode == 1
    assert proc.stdout.splitlines() == [
        "PASS host-etcd-snapshots",
        "PASS ns-a/pvc-x",
        "FAIL ns-app-gone/pvc-db: no backup credential registered for its app",
        "PASS ns-app-shop/pvc-db",
    ]
    key = f"--password-file {tmp_path / 'credentials' / 'app-shop' / 'RESTIC_PASSWORD'}"
    app_calls = [call for call in calls if "ns-app-shop" in call]
    assert app_calls and all(key in call and "core=unset from=unset" in call for call in app_calls)
    assert not any("ns-app-gone" in call for call in calls)
    assert all("--password-file" not in call for call in calls if "ns-app-" not in call)


def test_restic_check_starts_clear_of_backups_and_the_etcd_copy(
    example_copy: ExampleCopy,
) -> None:
    """In every env, its backup_check_schedule starts at least 3 hours from every nightly
    backup start and every etcd copy prune, which runs within the hour after a k3s snapshot."""
    from jarvis.schema.env import resolve_cluster_vars
    from jarvis.verify.common import scheduled_instants

    k3s_schedule = yaml.safe_load(
        (ROOT / "infra/ansible/roles/k3s/defaults/main.yml").read_text(encoding="utf-8")
    )["k3s_etcd_snapshot_schedule_cron"]
    for root, env in real_and_copy_envs(example_copy):
        assert _restic_check(root, env)["spec"]["schedule"] == "${backup_check_schedule}"
        values = resolve_cluster_vars(
            yaml.safe_load(
                (root / "envs" / env / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")
            )
        )
        check = values["backup_check_schedule"]
        run = scheduled_instants(check, "Etc/UTC", datetime(2026, 11, 2, tzinfo=UTC), 1)[0]
        for instant in scheduled_instants(values["backup_schedule"], "Etc/UTC", run + timedelta(days=2), 8):
            assert abs(instant - run) >= timedelta(hours=3), (env, "backup_schedule")
        for snapshot in scheduled_instants(k3s_schedule, "Etc/UTC", run + timedelta(days=2), 8):
            for prune in (snapshot, snapshot + timedelta(hours=1)):
                assert abs(prune - run) >= timedelta(hours=3), (env, "etcd copy prune")


def test_offsite_copy_renders_only_where_the_env_lists_it(example_copy: ExampleCopy) -> None:
    """The example copy turns the off-host copy on with its own backups-offsite secret; an env
    that does not list it renders no off-host CronJob or secret."""
    from jarvis.schema.env import offsite_copy_listed

    for root, env in real_and_copy_envs(example_copy):
        docs = [
            doc
            for doc in yaml.safe_load_all(
                _kustomize_build(f"envs/{env}/flux/platform-configs", root=root)
            )
            if isinstance(doc, dict)
        ]
        names = {(doc["kind"], doc["metadata"]["name"]) for doc in docs}
        listed = offsite_copy_listed(root / "envs" / env, root)
        assert listed or env != example_copy.name
        assert (("CronJob", "restic-offsite") in names) is listed
        assert (("Secret", "backups-offsite") in names) is listed
        if listed:
            cronjob = next(doc for doc in docs if doc["metadata"]["name"] == "restic-offsite")
            container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            assert container["envFrom"] == [{"secretRef": {"name": "backups-offsite"}}]


# Exits 10 for a missing destination; prints a file name it must not leak.
_FAKE_COPY = """#!/bin/sh
echo "$*" >> "$LOG"
echo "tree 1: file private-file-name"
dest=$2
case "$3" in
  cat) [ -e "$dest/config" ] || exit 10 ;;
  init) [ -d "$5" ] || exit 1; mkdir -p "$dest"; touch "$dest/config" ;;
  --no-lock) [ "$4" = copy ] && [ -d "$6" ] && [ -e "$dest/config" ] || exit 1; touch "$dest/copied" ;;
  *) exit 1 ;;
esac
"""


def test_offsite_copy_script_inits_and_copies_every_repository(tmp_path: Path) -> None:
    """Each local repository is copied into prefix + its path, a missing destination is
    initialised first, the local side takes no lock, and only PASS lines reach the log."""
    data, bindir, offsite = tmp_path / "data", tmp_path / "bin", tmp_path / "offsite"
    bindir.mkdir()
    for repo in ("host-etcd-snapshots", "ns-a/pvc-x"):
        (data / repo).mkdir(parents=True)
    (bindir / "restic").write_text(_FAKE_COPY, encoding="utf-8")
    (bindir / "restic").chmod(0o755)
    job = _docs(ROOT / "core/feature/offsite-backup-copy/restic-offsite.yaml")[0]["spec"]["jobTemplate"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    env.update(
        PATH=f"{bindir}:{os.environ['PATH']}",
        LOG=str(tmp_path / "log"),
        RESTIC_REPOSITORY=f"{offsite}/",
    )
    script = container["command"][-1].replace("/repositories", str(data))
    proc = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout
    assert proc.stdout.splitlines() == ["PASS host-etcd-snapshots", "PASS ns-a/pvc-x"]
    assert "private-file-name" not in proc.stdout + proc.stderr
    for repo in ("host-etcd-snapshots", "ns-a/pvc-x"):
        assert (offsite / repo / "copied").is_file()
    copies = [line for line in (tmp_path / "log").read_text().splitlines() if " copy " in line]
    assert copies == [
        f"--repo {offsite}/{repo} --no-lock copy --from-repo {data}/{repo}"
        for repo in ("host-etcd-snapshots", "ns-a/pvc-x")
    ]


def test_offsite_copy_fails_every_repository_at_once_when_the_target_is_down(tmp_path: Path) -> None:
    """A target that does not answer the first probe fails each repository with one line and
    runs no init or copy."""
    data, bindir = tmp_path / "data", tmp_path / "bin"
    bindir.mkdir()
    for repo in ("host-etcd-snapshots", "ns-a/pvc-x"):
        (data / repo).mkdir(parents=True)
    (bindir / "restic").write_text('#!/bin/sh\necho "$*" >> "$LOG"\nexit 1\n', encoding="utf-8")
    (bindir / "restic").chmod(0o755)
    job = _docs(ROOT / "core/feature/offsite-backup-copy/restic-offsite.yaml")[0]["spec"]["jobTemplate"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
    env.update(
        PATH=f"{bindir}:{os.environ['PATH']}", LOG=str(tmp_path / "log"), RESTIC_REPOSITORY="rest:http://down/"
    )
    script = container["command"][-1].replace("/repositories", str(data))
    proc = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 1
    assert proc.stdout.splitlines() == [
        "FAIL host-etcd-snapshots: off-host target not reachable (probe exited 1)",
        "FAIL ns-a/pvc-x: off-host target not reachable (probe exited 1)",
    ]
    assert len((tmp_path / "log").read_text().splitlines()) == 1


def test_offsite_copy_reads_the_source_password_from_backups_restic() -> None:
    """The local-side password is key RESTIC_PASSWORD of Secret backups-restic."""
    job = _docs(ROOT / "core/feature/offsite-backup-copy/restic-offsite.yaml")[0]["spec"]["jobTemplate"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    source = next(item for item in container["env"] if item["name"] == "RESTIC_FROM_PASSWORD")
    assert source["valueFrom"]["secretKeyRef"] == {"name": "backups-restic", "key": "RESTIC_PASSWORD"}
