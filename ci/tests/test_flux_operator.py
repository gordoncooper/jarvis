"""flux-operator Ansible role, against a real API server: a throwaway kube-apiserver on loopback
(apiserver.py), with stand-ins for the Flux Operator's and Flux's CRDs. Each test starts from an
empty flux-system and reads what the role left in the API."""

from __future__ import annotations

import base64
import getpass
import grp
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import NamedTuple

import apiserver
import jinja2
import pytest
import yaml
from ansible_role_proof import (
    ANSIBLE_DIR,
    FLUX_OPERATOR_ROLE_DIR,
    FLUX_OPERATOR_ROLE_TASKS,
    LOCALHOST,
    changed,
    env_name,
    failed_task,
    fake_tool,
    load_cluster_vars,
    load_role_defaults,
    local_play,
    playbook,
    ran,
    role_tasks,
)
from conftest import require_cli

from jarvis.schema.values import defaults
from support import REPO_ROOT, free_port, sample_root, yaml_docs

# Core's FluxInstance, the one group_vars gives every env.
FLUX_INSTANCE = REPO_ROOT / "core/flux/flux-instance.yaml"


def _task_documents() -> list[dict]:
    """Each task file's top-level tasks. A block's no_log covers the tasks in it."""
    return [task for path in sorted(FLUX_OPERATOR_ROLE_TASKS.glob("*.yml"))
            for task in yaml.safe_load(path.read_text(encoding="utf-8"))]


def test_sops_age_tasks_use_no_log() -> None:
    """Rule: every task that touches the age key, a Secret's data or a Secret template hides its output, and the key
    file is stat'ed once, on the controller, as the runner."""
    secret_templates = [path.name for path in (FLUX_OPERATOR_ROLE_DIR / "templates").glob("*.j2")
                        if "kind: Secret" in path.read_text(encoding="utf-8")]
    needles = [needle.lower() for needle in (
        "SOPS_AGE_KEY_FILE",
        "flux_sops_age_key",
        "age private key",
        "age.agekey",
        # A lookup of a Secret returns its data.
        "kind: secret",
        "flux_sops_age_file_identities",
        "flux_sops_age_publics",
        *secret_templates,
    )]
    tasks = [(task, yaml.safe_dump(task, sort_keys=False).lower()) for task in _task_documents()]
    # Each needle still names a task, so a rename can't empty the check.
    assert [needle for needle in needles if not any(needle in text for _, text in tasks)] == []
    touching = [task for task, text in tasks if any(needle in text for needle in needles)]
    assert [task["name"] for task in touching if task.get("no_log") is not True] == []
    stat = next(task for task, _ in tasks if task["name"] == "Stat the age private key file")
    assert {key: stat.get(key) for key in ("delegate_to", "become", "run_once")} == {
        "delegate_to": "localhost", "become": False, "run_once": True}


@pytest.mark.parametrize(
    ("check_mode", "tags", "runs"),
    [
        pytest.param(True, ["all"], False, id="check-untagged"),
        pytest.param(True, ["flux-operator"], True, id="check-tagged-flux-operator"),
        pytest.param(False, ["all"], True, id="not-a-check"),
    ],
)
def test_untagged_check_run_skips_the_flux_operator_play(check_mode: bool, tags: list[str], runs: bool) -> None:
    """Rule: an untagged --check run (run tags ['all']) skips the flux-operator role, and its pre-task says so;
    TAGS=flux-operator, or a run that is not a check, runs the role."""
    plays = yaml.safe_load((ANSIBLE_DIR / "site.yml").read_text(encoding="utf-8"))
    play, role = next((play, role) for play in plays for role in play.get("roles", [])
                      if isinstance(role, dict) and role["role"] == "flux-operator")
    says = next(task for task in play["pre_tasks"] if "ansible.builtin.debug" in task)
    facts = {"ansible_check_mode": check_mode, "ansible_run_tags": tags}
    when = jinja2.Environment().compile_expression
    assert (when(role["when"])(**facts), when(says["when"])(**facts)) == (runs, not runs)


# What the role reads and writes, by API path.
NS = "/api/v1/namespaces/flux-system"
OPERATOR = "/apis/apps/v1/namespaces/flux-system/deployments/flux-operator"
HOLD = "/api/v1/namespaces/kube-system/configmaps/backup-rebuild-hold"
INSTANCE = "/apis/fluxcd.controlplane.io/v1/namespaces/flux-system/fluxinstances/flux"
SOURCES = "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories"
ROOT = "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/flux-system"


def _secret_path(name: str) -> str:
    return f"{NS}/secrets/{name}"


def _crd(group: str, kind: str, plural: str) -> dict:
    """A stand-in CRD that keeps any field: the operator's chart and the FluxInstance install the real ones."""
    return {
        "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition", "metadata": {"name": f"{plural}.{group}"},
        "spec": {"group": group, "scope": "Namespaced",
                 "names": {"kind": kind, "plural": plural, "singular": kind.lower(), "listKind": f"{kind}List"},
                 "versions": [{"name": "v1", "served": True, "storage": True,
                               "schema": {"openAPIV3Schema": {"type": "object", "x-kubernetes-preserve-unknown-fields": True}}}]},
    }


_CRDS = (
    _crd("fluxcd.controlplane.io", "FluxInstance", "fluxinstances"),
    _crd("source.toolkit.fluxcd.io", "GitRepository", "gitrepositories"),
    _crd("kustomize.toolkit.fluxcd.io", "Kustomization", "kustomizations"),
)
_PLURALS = {crd["spec"]["names"]["kind"]: crd["spec"]["names"]["plural"] for crd in _CRDS}


def _get(api: apiserver.Api, path: str) -> dict | None:
    status, answer = api.request("GET", path)
    if status == 404:
        return None
    assert status == 200, answer
    return json.loads(answer)


def _post(api: apiserver.Api, path: str, doc: dict) -> dict:
    status, answer = api.request("POST", path, doc)
    assert status == 201, answer
    return json.loads(answer)


@pytest.fixture(scope="module")
def server():
    admin = apiserver.token()
    with apiserver.serve({admin: "proof,proof,system:masters"}) as (port, work):
        api = apiserver.Api(port, admin)
        for crd in _CRDS:
            _post(api, "/apis/apiextensions.k8s.io/v1/customresourcedefinitions", crd)
        apiserver.eventually(lambda: api.request("GET", INSTANCE)[0] == 404, "the stand-in CRDs to be served")
        api.config = api.kubeconfig(work / "kubeconfig")
        yield api


@pytest.fixture
def api(server: apiserver.Api) -> apiserver.Api:
    """The server with flux-system present and nothing the role writes in it."""
    for path in (OPERATOR, HOLD, INSTANCE, ROOT, f"{SOURCES}/flux-system", f"{SOURCES}/core",
                 _secret_path("sops-age"), _secret_path("flux-deploy-key")):
        assert server.request("DELETE", path)[0] in (200, 202, 404)
    if _get(server, NS) is None:
        _post(server, "/api/v1/namespaces", {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "flux-system"}})
    return server


def _unreachable(tmp_path: Path) -> Path:
    """A kubeconfig whose server does not answer."""
    return apiserver.Api(free_port(), "none").kubeconfig(tmp_path / "unreachable.kubeconfig")


def _operator_running(api: apiserver.Api) -> None:
    """The operator's Deployment, Available, as the chart and a kubelet would leave it."""
    labels = {"app.kubernetes.io/name": "flux-operator"}
    doc = _post(api, OPERATOR.rsplit("/", 1)[0], {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "flux-operator", "namespace": "flux-system"},
        "spec": {"selector": {"matchLabels": labels},
                 "template": {"metadata": {"labels": labels}, "spec": {"containers": [{"name": "manager", "image": "operator"}]}}},
    })
    doc["status"] = {"conditions": [{"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"}]}
    status, answer = api.request("PUT", OPERATOR + "/status", doc)
    assert status == 200, answer


def _root_source(api: apiserver.Api) -> None:
    _post(api, SOURCES, {"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
                         "metadata": {"name": "flux-system", "namespace": "flux-system"}, "spec": {}})


def _seal_sops_age(api: apiserver.Api, content: str) -> int:
    """flux-system/sops-age holding content; returns its resourceVersion."""
    doc = _post(api, f"{NS}/secrets", {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "sops-age", "namespace": "flux-system"},
        "data": {"age.agekey": base64.b64encode(content.encode()).decode()},
    })
    return int(doc["metadata"]["resourceVersion"])


def _secret_data(api: apiserver.Api, name: str) -> dict[str, str] | None:
    doc = _get(api, _secret_path(name))
    if doc is None:
        return None
    return {key: base64.b64decode(value).decode() for key, value in (doc.get("data") or {}).items()}


def _version(api: apiserver.Api, path: str) -> int:
    """The object's resourceVersion: etcd's revision when it was last written, so later writes count higher."""
    doc = _get(api, path)
    assert doc is not None, path
    return int(doc["metadata"]["resourceVersion"])


def _role(tmp_path: Path, kubeconfig: Path | None, *args: object, tasks_from: tuple[str, ...] = (),
          env: dict[str, str] | None = None, json: bool = True, shown: bool = False) -> subprocess.CompletedProcess[str]:
    """The flux-operator role, or only its tasks_from files in order, on localhost with the venv's Python, which holds
    kubernetes.core's client. kubeconfig's server is the only one it can reach (none when no task needs one). shown
    runs it again as jarvis site prints it, and adds that output."""
    require_cli("ansible-playbook")
    tasks = "".join(role_tasks("flux-operator", name) for name in tasks_from) or role_tasks("flux-operator")
    config = str(kubeconfig or "/nonexistent/kubeconfig")
    # kubernetes.core reads these when a task names no kubeconfig; the full role names it.
    reach = {"K8S_AUTH_KUBECONFIG": config, "KUBECONFIG": config, "SOPS_AGE_KEY_FILE": None,
             **{key: None for key in os.environ if key.lower() in ("https_proxy", "http_proxy", "all_proxy")},
             **(env or {})}
    argv = (local_play(tmp_path, tasks), *LOCALHOST, "-e", f"ansible_python_interpreter={sys.executable}", *args)
    run = playbook(*argv, env=reach, json=json)
    if shown:
        # A refusal changes nothing, so this run meets the same state. Its output prints a failed no_log task's
        # fail_msg (ansible-core 2.22), which the JSON result censors.
        again = playbook(*argv, env=reach)
        assert again.returncode == run.returncode, again.output
        run.output += again.output
    return run


def _flux_operator_extra(kubeconfig: Path) -> list[str]:
    """What jarvis site passes the role from the sample env's cluster-vars; a wait that cannot pass fails in seconds."""
    cluster = load_cluster_vars()
    return ["-e", json.dumps({
        "k3s_kubeconfig_path": str(kubeconfig),
        **{key: cluster[key] for key in ("jarvis_env", "flux_git_repo_url", "flux_git_ref")},
        "flux_operator_crd_wait_timeout": 20, "flux_operator_rollout_wait_timeout": 20, "flux_crd_wait_timeout": 20,
    })]


def _backup_inventory(tmp_path: Path) -> Path:
    path = tmp_path / "backup-hosts.yml"
    path.write_text(
        "all:\n"
        "  children:\n"
        "    backup:\n"
        "      hosts:\n"
        "        backup-host:\n"
        "          ansible_connection: local\n"
        f"          ansible_python_interpreter: {sys.executable}\n",
        encoding="utf-8",
    )
    return path


def _key_vars(keyfile: Path, *recipients: str) -> list[str]:
    """What jarvis site resolves for the role: the working key's path and the env's recipients."""
    return ["-e", json.dumps({"sops_age_key_file": str(keyfile), "sops_age_recipients": list(recipients)})]


def test_a_converged_cluster_changes_nothing(api: apiserver.Api, tmp_path: Path) -> None:
    """Rule: with the operator Available, sops-age present and Flux syncing, a check run and a real run of the whole
    role need no age key and change nothing (fix a3b13f4)."""
    _operator_running(api)
    _seal_sops_age(api, "an existing key")
    _root_source(api)
    for flags in (("--check",), ()):
        run = _role(tmp_path, api.config, *flags, "-i", _backup_inventory(tmp_path), *_flux_operator_extra(api.config),
                    *_key_vars(tmp_path / "missing-age-key"))
        assert run.returncode == 0 and changed(run) == {"localhost": 0}, run.output
    assert _get(api, INSTANCE) is None


_SOPS_ENV = "env-test"
# no_log censors a refusal's JSON result, msg included.
_CENSORED = ""
# The task that reads the key file's identities. A refusal checks it never ran; a new sops-age, that it did.
_DERIVE = "Derive the public key of each identity in the age private key file"


def _sops_age(kubeconfig: Path, tmp_path: Path, keyfile: Path, *recipients: str,
              env: dict[str, str] | None = None, shown: bool = False) -> subprocess.CompletedProcess[str]:
    """sops_age.yml's checks, then sops_age_secret.yml's create."""
    return _role(tmp_path, kubeconfig, "-e", f"jarvis_env={_SOPS_ENV}", *_key_vars(keyfile, *recipients),
                 tasks_from=("sops_age.yml", "sops_age_secret.yml"), env=env, shown=shown)


@pytest.mark.parametrize(
    ("tasks_from", "refusal"),
    [
        pytest.param(("sops_age.yml", "sops_age_secret.yml"), "Require an age private key when the sops-age secret is absent",
                     id="sops-age"),
        pytest.param(("sops_age_key_file.yml", "deploy_key.yml"), "Require the age private key to decrypt the deploy key",
                     id="deploy-key"),
    ],
)
def test_a_run_without_the_age_key_refuses_while_its_secret_is_absent(
    api: apiserver.Api, tmp_path: Path, tasks_from: tuple[str, ...], refusal: str
) -> None:
    """Rule: while sops-age or the deploy key is absent, a run without the age key file refuses before any change."""
    run = _role(tmp_path, api.config, "-e", json.dumps({"jarvis_env": _SOPS_ENV, "flux_deploy_key_file": str(tmp_path / "sealed")}),
                *_key_vars(tmp_path / "missing-age-key"), tasks_from=tasks_from)
    assert run.returncode != 0 and failed_task(run) == (refusal, _CENSORED), run.output
    assert (_get(api, _secret_path("sops-age")), _get(api, _secret_path("flux-deploy-key"))) == (None, None)


class _Identity(NamedTuple):
    line: str  # AGE-SECRET-KEY-1..., as the key file holds it
    public: str  # age1..., the recipient


def _identity() -> _Identity:
    """A throwaway age identity, read from age-keygen's own layout. Nothing is printed."""
    require_cli("age-keygen")
    lines = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout.splitlines()
    return _Identity(lines[-1], next(line.removeprefix("# public key: ") for line in lines if line.startswith("# public key: ")))


def _key_file(tmp_path: Path, *identities: _Identity) -> Path:
    """A key file in age-keygen's layout: comment lines, then each identity."""
    path = tmp_path / "keys.txt"
    body = "".join(f"# created: 2026-01-01T00:00:00Z\n# public key: {public}\n{line}\n" for line, public in identities)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    ("keys", "recipients", "held"),
    [
        pytest.param(("other", "env"), ("recovery", "env"), ("env",), id="one-env-identity"),
        pytest.param(("other", "first", "second"), ("first", "second"), ("first", "second"), id="two-env-identities"),
    ],
)
def test_new_sops_age_holds_only_the_env_identities_one_per_line(
    api: apiserver.Api, tmp_path: Path, keys: tuple[str, ...], recipients: tuple[str, ...], held: tuple[str, ...]
) -> None:
    """Rule: a new flux-system/sops-age holds the key file's identities for the env's recipients and no other, one per
    line (fix 6395612), with Flux's reconcile off, and none is printed."""
    ids = {name: _identity() for name in {*keys, *recipients}}
    run = _sops_age(api.config, tmp_path, _key_file(tmp_path, *[ids[name] for name in keys]),
                    *[ids[name].public for name in recipients])
    assert run.returncode == 0 and changed(run) == {"localhost": 1}, run.output
    assert ran(run, _DERIVE) == {"localhost": "ok"}
    assert _secret_data(api, "sops-age") == {"age.agekey": "\n".join(ids[name].line for name in held)}
    assert _get(api, _secret_path("sops-age"))["metadata"]["annotations"] == {"kustomize.toolkit.fluxcd.io/reconcile": "disabled"}
    assert [name for name, identity in ids.items() if identity.line in run.output] == []


_OVERWRITE = ("Refuse to overwrite an existing sops-age secret with different content", _CENSORED)


@pytest.mark.parametrize(
    ("keys", "recipients", "existing", "refusal", "shown"),
    [
        pytest.param(("other", "first", "second"), ("first", "second"), "{keyfile}", None, False, id="whole-key-file"),
        pytest.param(("other", "env"), ("env",), "{env.line}", None, False, id="env-identity-only"),
        pytest.param(("other", "first", "second"), ("first", "second"), "{first.line}", _OVERWRITE, False,
                     id="one-of-two"),
        pytest.param(("env",), ("env",), "# public key: {other.public}\n{other.line}", _OVERWRITE, True,
                     id="another-identity"),
    ],
)
def test_existing_sops_age_passes_untouched_only_when_it_holds_the_env_identities(
    api: apiserver.Api, tmp_path: Path, keys: tuple[str, ...], recipients: tuple[str, ...], existing: str,
    refusal: tuple[str, str] | None, shown: bool,
) -> None:
    """Rule: an existing flux-system/sops-age that holds every env identity passes untouched, whatever else it holds;
    one that does not may be the only copy of another key, so the run refuses before any change."""
    ids = {name: _identity() for name in ("other", "env", "first", "second")}
    keyfile = _key_file(tmp_path, *[ids[name] for name in keys])
    version = _seal_sops_age(api, existing.format(keyfile=keyfile.read_text(encoding="utf-8"), **ids))
    run = _sops_age(api.config, tmp_path, keyfile, *[ids[name].public for name in recipients], shown=shown)
    assert (run.returncode != 0) is (refusal is not None) and failed_task(run) == refusal, run.output
    assert changed(run) == {"localhost": 0} and _version(api, _secret_path("sops-age")) == version
    assert [name for name, identity in ids.items() if identity.line in run.output] == []


def test_key_file_without_the_env_identity_refuses_before_the_operator_install(api: apiserver.Api, tmp_path: Path) -> None:
    """Rule: the whole role stops at the age key check, before the operator lookup, Helm or any cluster write."""
    other, recipient = _identity(), _identity()
    helm_log = tmp_path / "helm.log"
    helm = fake_tool(tmp_path / "bin", "helm", f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> {helm_log}\n")
    run = _role(tmp_path, api.config, *_flux_operator_extra(api.config), "-e", f"helm_binary_path={helm}",
                *_key_vars(_key_file(tmp_path, other), recipient.public), shown=True)
    assert run.returncode != 0 and failed_task(run) == (
        "Refuse a key file with no identity for the env's age recipient", _CENSORED), run.output
    assert changed(run) == {"localhost": 0} and ran(run, "Check whether Flux Operator deployment exists") == {}
    assert (helm_log.exists(), _get(api, _secret_path("sops-age")), other.line in run.output) == (False, None, False)


def test_missing_age_keygen_is_named_before_no_log(api: apiserver.Api, tmp_path: Path) -> None:
    """Rule: a missing age-keygen fails in a visible task that names it, before the no_log block reads the key
    (fix bd1d2be)."""
    ansible_playbook = Path(require_cli("ansible-playbook"))
    keyfile = tmp_path / "age.key"
    keyfile.write_text("not-read\n", encoding="utf-8")
    keyfile.chmod(0o600)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Hide age-keygen only. ansible-playbook stays, including when it lives
    # outside /bin and /usr/bin, which is where CI's venv puts it.
    for directory in (Path("/bin"), Path("/usr/bin"), ansible_playbook.parent):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            dest = bindir / entry.name
            # /bin is often /usr/bin, so each entry comes twice; is_symlink, unlike exists, also
            # sees a link already made to a dangling one (ubuntu-26.04's grub-ntldr-img).
            if entry.name == "age-keygen" or dest.is_symlink():
                continue
            dest.symlink_to(entry)
    if not (bindir / "ansible-playbook").exists():
        (bindir / "ansible-playbook").symlink_to(ansible_playbook)
    run = _role(tmp_path, api.config, *_key_vars(keyfile), tasks_from=("sops_age.yml",), env={"PATH": str(bindir)})
    assert run.returncode != 0 and failed_task(run) == (
        "Require age-keygen on the controller",
        "age-keygen is not on PATH. Install age-keygen before flux-operator. A missing binary would otherwise fail"
        " inside a no_log task.",
    ), run.output
    assert ran(run, _DERIVE) == {}
    assert "not-read" not in run.output


@pytest.mark.parametrize(
    ("mode", "owner", "runner", "shown"),
    [
        pytest.param(0o644, os.getuid(), os.getuid(), True, id="mode-0644"),
        pytest.param(0o600, os.getuid(), os.getuid() + 1, False, id="another-owner"),
        # Root may read any file, but the key must still be its own. As root, the file keeps root's group.
        pytest.param(0o600, os.getuid() or 1, 0, False, id="root-runner"),
    ],
)
def test_sops_age_refuses_a_key_file_not_0600_or_not_the_runners(
    api: apiserver.Api, tmp_path: Path, mode: int, owner: int, runner: int, shown: bool
) -> None:
    """Rule: an age key file that is not mode 0600 and owned by the runner is refused before it is read, so no
    sops-age is made from it."""
    identity = _identity()
    keyfile = _key_file(tmp_path, identity)
    keyfile.chmod(mode)
    # Only root can give a file to another uid; unprivileged, owner is the runner's own uid and this changes nothing.
    os.chown(keyfile, owner, -1)
    # The role takes the runner's uid from id -u.
    fake_tool(tmp_path / "bin", "id", f"#!/bin/sh\necho {runner}\n")
    run = _sops_age(api.config, tmp_path, keyfile, identity.public,
                    env={"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"}, shown=shown)
    assert run.returncode != 0 and failed_task(run) == (
        "Refuse an age private key that is not mode 0600 and owned by the runner", _CENSORED), run.output
    assert ran(run, _DERIVE) == {}
    assert (_get(api, _secret_path("sops-age")), identity.line in run.output) == (None, False)


def test_sops_age_lookup_error_does_not_bootstrap(tmp_path: Path) -> None:
    """Rule: an API that does not answer is not an absent secret: the role stops at its first call, which reads no
    secret, so its error shows and the key is never read (fix d191dcd)."""
    sentinel = "SENTINEL-AGE-KEY-NOT-READ"
    keyfile = tmp_path / "age.key"
    keyfile.write_text(sentinel + "\n", encoding="utf-8")
    keyfile.chmod(0o600)
    run = _sops_age(_unreachable(tmp_path), tmp_path, keyfile)
    assert run.returncode != 0 and ran(run, "Reach the Kubernetes API") == {"localhost": "failed"}, run.output
    assert ran(run, "Check whether flux-system sops-age secret exists") == {} and sentinel not in run.output


# Ansible's become runs sudo -H -S -n -u root <command>: this one runs the command as this user.
_SUDO = '#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do [ "$1" = -u ] && shift; shift; done\nexec "$@"\n'


def _sync_file() -> Path:
    return sample_root() / "envs" / env_name() / "flux" / "sync.yaml"


# A backup host's restic tree: one PVC repository and one that is not.
_RESTIC = ("ns-demo/pvc-drill", "ns-decoy/notpvc")


def _instance_extra(kubeconfig: Path, tmp_path: Path, *repos: str) -> list[str]:
    """instance.yml's inputs: core's FluxInstance, the sample env's sync.yaml, and a backup host whose restic tree
    holds repos."""
    for repo in repos:
        (tmp_path / "backup" / "restic" / repo).mkdir(parents=True)
        (tmp_path / "backup" / "restic" / repo / "config").write_text("repo\n", encoding="utf-8")
    return [
        *_flux_operator_extra(kubeconfig), "-i", _backup_inventory(tmp_path),
        "-e", f"backup_path={tmp_path / 'backup'}", "-e", f"ansible_become_exe={fake_tool(tmp_path / 'bin', 'sudo', _SUDO)}",
        "-e", f"flux_instance_file={FLUX_INSTANCE}", "-e", f"flux_sync_file={_sync_file()}",
    ]


def _as_applied(api: apiserver.Api, doc: dict) -> dict | None:
    """The API's copy of a Flux object, with only the fields doc sets."""
    meta = doc["metadata"]
    found = _get(api, f"/apis/{doc['apiVersion']}/namespaces/{meta['namespace']}/{_PLURALS[doc['kind']]}/{meta['name']}")
    return found and {**{key: found[key] for key in doc}, "metadata": {key: found["metadata"].get(key) for key in meta}}


def test_rerun_with_root_source_present_does_not_set_rebuild_hold(api: apiserver.Api, tmp_path: Path) -> None:
    """Rule: once Flux syncs (the root source exists), restic data on the backup host holds nothing back: no hold, and
    nothing applied or changed."""
    _root_source(api)
    run = _role(tmp_path, api.config, *_instance_extra(api.config, tmp_path, *_RESTIC), tasks_from=("instance.yml",))
    assert run.returncode == 0 and changed(run) == {"localhost": 0}, run.output
    assert (_get(api, HOLD), _get(api, INSTANCE)) == (None, None)


def test_fresh_cluster_sets_the_hold_then_applies_the_flux_instance_and_root_source(api: apiserver.Api, tmp_path: Path) -> None:
    """Rule: on a new cluster the role sets the rebuild hold for the backup host's PVC repositories, then applies core's
    FluxInstance, then the env's sync.yaml, each as written."""
    run = _role(tmp_path, api.config, *_instance_extra(api.config, tmp_path, *_RESTIC), tasks_from=("instance.yml",))
    assert run.returncode == 0, run.output
    assert _get(api, HOLD)["data"] == {"repos": "ns-demo/pvc-drill\n"}
    docs = [*yaml_docs(FLUX_INSTANCE), *yaml_docs(_sync_file())]
    assert [_as_applied(api, doc) for doc in docs] == docs
    assert _version(api, HOLD) < _version(api, INSTANCE) < _version(api, f"{SOURCES}/flux-system") <= _version(api, ROOT)


def test_fresh_cluster_over_a_backup_host_without_restic_data_sets_no_hold(api: apiserver.Api, tmp_path: Path) -> None:
    """Rule: a new cluster whose backup host holds no restic data gets no rebuild hold, which would pause every
    backup; the FluxInstance and sync.yaml still apply."""
    run = _role(tmp_path, api.config, *_instance_extra(api.config, tmp_path), tasks_from=("instance.yml",))
    assert run.returncode == 0, run.output
    assert (_get(api, HOLD), _get(api, ROOT) is not None) == (None, True)


def test_root_source_lookup_error_does_not_set_the_hold(tmp_path: Path) -> None:
    """Rule: an API that does not answer is not a new cluster: the root source lookup fails before the hold role runs."""
    unreachable = _unreachable(tmp_path)
    run = _role(tmp_path, unreachable, *_instance_extra(unreachable, tmp_path, *_RESTIC), tasks_from=("instance.yml",))
    assert run.returncode != 0 and ran(run, "Check whether the root GitRepository exists") == {"localhost": "failed"}, run.output
    assert ran(run, "Stat restic data on the backup host") == {}


# The schema's default for flux_known_hosts, which jarvis site passes.
_GITHUB_KEY = defaults("cluster-vars")["flux_known_hosts"]


def test_fresh_cluster_creates_the_deploy_key_secret_once(api: apiserver.Api, tmp_path: Path) -> None:
    """Rule: an ssh:// root's sealed deploy key becomes flux-system/flux-deploy-key once, with GitHub's host key and
    Flux's reconcile off; a later run needs no age key and leaves it, and the key is never printed."""
    require_cli("sops")
    from jarvis.secrets.sops import encrypt_secret_to_file

    identity = _identity()
    instance = tmp_path / "instance"
    (instance / "envs" / _SOPS_ENV).mkdir(parents=True)
    rule = {"path_regex": f"envs/{_SOPS_ENV}/secrets/.*\\.sops\\.yaml$", "encrypted_regex": "^(data|stringData)$",
            "age": identity.public}
    (instance / ".sops.yaml").write_text(yaml.safe_dump({"creation_rules": [rule]}), encoding="utf-8")
    private = "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----"
    encrypt_secret_to_file(
        repo_root=instance, env=_SOPS_ENV, name="flux-deploy-key", sops_bin="sops",
        document={"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": "flux-deploy-key", "namespace": "flux-system"},
                  "stringData": {"identity": private}},
    )
    sealed = instance / "envs" / _SOPS_ENV / "secrets" / "flux-deploy-key.sops.yaml"
    deploy_key = ["-e", json.dumps({"flux_deploy_key_file": str(sealed), "flux_known_hosts": _GITHUB_KEY})]
    versions = []
    # Human output: the key is multi-line, and JSON would print it escaped. The second run has no age key, so a second
    # create would fail to decrypt, where one from the same sealed file would change nothing.
    for keyfile in (_key_file(tmp_path, identity), tmp_path / "missing-age-key"):
        run = _role(tmp_path, api.config, *deploy_key, *_key_vars(keyfile), tasks_from=("sops_age_key_file.yml", "deploy_key.yml"),
                    json=False)
        assert run.returncode == 0 and private not in run.output, run.output
        versions.append(_version(api, _secret_path("flux-deploy-key")))
    secret = _get(api, _secret_path("flux-deploy-key"))
    assert (versions[1], secret["metadata"]["annotations"]) == (versions[0], {"kustomize.toolkit.fluxcd.io/reconcile": "disabled"})
    assert _secret_data(api, "flux-deploy-key") == {"identity": private, "known_hosts": _GITHUB_KEY}


def test_known_hosts_is_a_github_published_host_key() -> None:
    """Rule: flux_known_hosts' default pins a key GitHub publishes (api.github.com/meta), so Flux trusts github.com only."""
    import urllib.error
    import urllib.request

    # Unauthenticated calls share the runner's address limit; CI passes the workflow's read-only token.
    request = urllib.request.Request("https://api.github.com/meta")
    if token := os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            published = json.load(response)["ssh_keys"]
    except urllib.error.HTTPError as error:
        if error.code < 500:
            raise
        pytest.skip(f"api.github.com answers {error.code}; GitHub is down, not the pin")
    host, key = _GITHUB_KEY.split(" ", 1)
    assert host == "github.com"
    assert key in published


def _pinned_helm_version() -> str:
    return str(load_role_defaults(FLUX_OPERATOR_ROLE_DIR)["helm_version"])


def _fake_helm(log: Path, version: str) -> str:
    """A helm that reports version like `helm version --short` and logs every call."""
    return f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> {log}\nif [[ \"$1\" == version ]]; then echo \"v{version}+gfixture\"; fi\n"


def _helm_archive(tmp_path: Path, binary: str) -> tuple[Path, str]:
    """A release-shaped archive holding linux-amd64/helm, and its sha256."""
    path = tmp_path / "helm.tar.gz"
    data = binary.encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("linux-amd64/helm")
        member.size = len(data)
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(data))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_helm_cli_missing_or_another_version_is_replaced_then_left_alone(tmp_path: Path) -> None:
    """Rule: a helm that is missing or reports another version is replaced from the pinned archive; one at the pin is
    left alone, and nothing is downloaded (fixed seed 11)."""
    log = tmp_path / "helm.log"
    installed = tmp_path / "bin" / "helm"
    installed.parent.mkdir()
    pinned = _fake_helm(log, _pinned_helm_version())
    archive, digest = _helm_archive(tmp_path, pinned)

    def run(url: str) -> subprocess.CompletedProcess[str]:
        return _role(tmp_path, None, "-e", json.dumps({
            "helm_binary_path": str(installed), "helm_download_url": url, "helm_linux_amd64_tarball_sha256": digest,
            # Defaults are root:root. This play does not become root.
            "helm_binary_owner": getpass.getuser(), "helm_binary_group": grp.getgrgid(os.getgid()).gr_name,
        }), tasks_from=("helm_cli.yml",))

    # A fresh node has no helm yet.
    fresh = run(archive.as_uri())
    assert fresh.returncode == 0 and installed.read_text(encoding="utf-8") == pinned, fresh.output
    fake_tool(installed.parent, "helm", _fake_helm(log, "3.0.0"))
    replaced = run(archive.as_uri())
    assert replaced.returncode == 0 and installed.read_text(encoding="utf-8") == pinned, replaced.output
    # The archive URL now points at nothing, so a download would fail the run.
    again = run((tmp_path / "absent.tar.gz").as_uri())
    assert again.returncode == 0 and changed(again) == {"localhost": 0}, again.output


def _installing_helm(api: apiserver.Api, log: Path) -> str:
    """A helm at the pinned version whose install leaves the operator's Deployment Available, as
    the chart and a kubelet would."""
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "flux-operator", "namespace": "flux-system"},
        "spec": {"selector": {"matchLabels": {"app": "op"}},
                 "template": {"metadata": {"labels": {"app": "op"}}, "spec": {"containers": [{"name": "m", "image": "op"}]}}},
    }
    return f"""#!{sys.executable}
import json, ssl, sys, urllib.request
with open({str(log)!r}, "a") as out:
    out.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["version"]:
    print("v{_pinned_helm_version()}+gfixture")
    sys.exit(0)
context = ssl._create_unverified_context()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}), urllib.request.HTTPSHandler(context=context))
def call(method, path, body):
    request = urllib.request.Request({api.url!r} + path, method=method, data=json.dumps(body).encode(),
                                     headers={{"Authorization": "Bearer {api.token}", "Content-Type": "application/json"}})
    return json.load(opener.open(request))
doc = call("POST", {OPERATOR.rsplit("/", 1)[0]!r}, {deployment!r})
doc["status"] = {{"conditions": [{{"type": "Available", "status": "True"}}]}}
call("PUT", {OPERATOR + "/status"!r}, doc)
"""


@pytest.mark.parametrize(
    ("reachable", "lookup", "calls"),
    [
        pytest.param(True, "ok", [
            "version --short",
            "upgrade --install flux-operator oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator --version 0.0.0-test"
            " --namespace flux-system --create-namespace --wait --timeout 5m",
        ], id="absent"),
        pytest.param(False, "failed", None, id="unreachable"),
    ],
)
def test_operator_lookup_installs_only_when_absent(
    api: apiserver.Api, tmp_path: Path, reachable: bool, lookup: str, calls: list[str] | None
) -> None:
    """Rule: an operator Deployment the API reports absent installs the chart at the given pin; an API that does not
    answer installs nothing (fix d191dcd)."""
    helm_log = tmp_path / "helm.log"
    helm = fake_tool(tmp_path / "bin", "helm", _installing_helm(api, helm_log))
    config = api.config if reachable else _unreachable(tmp_path)
    run = _role(tmp_path, config, *_flux_operator_extra(config), "-e", json.dumps({
        "helm_binary_path": str(helm), "flux_operator_chart_version": "0.0.0-test",
        # A helm at the pin is never downloaded again; a download from here would fail the run.
        "helm_download_url": (tmp_path / "absent.tar.gz").as_uri(),
    }), tasks_from=("operator.yml",))
    assert (run.returncode == 0) is reachable and ran(run, "Check whether Flux Operator deployment exists") == {
        "localhost": lookup}, run.output
    assert (helm_log.read_text(encoding="utf-8").splitlines() if helm_log.exists() else None) == calls
