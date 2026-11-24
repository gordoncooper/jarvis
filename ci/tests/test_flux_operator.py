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
import re
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import apiserver
import pytest
import yaml
from ansible_role_proof import (
    ANSIBLE_DIR,
    FLUX_OPERATOR_ROLE_DIR,
    FLUX_OPERATOR_ROLE_TASKS,
    env_name,
    example_flux_entry,
    load_cluster_vars,
    load_role_defaults,
    playbook_recap_field,
)
from conftest import require_cli

from jarvis.schema.values import defaults
from support import sample_root

SITE_YML = ANSIBLE_DIR / "site.yml"

_ROLE_KEYS = (
    "ansible.builtin.include_role",
    "include_role",
    "ansible.builtin.import_role",
    "import_role",
)


def _role_names(play: dict[str, object]) -> list[str]:
    names: list[str] = []
    for role in play.get("roles") or []:
        if isinstance(role, str):
            names.append(role)
        elif isinstance(role, dict) and isinstance(role.get("role"), str):
            names.append(role["role"])
    for task in play.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        for key in _ROLE_KEYS:
            included = task.get(key)
            if isinstance(included, dict) and isinstance(included.get("name"), str):
                names.append(included["name"])
            elif isinstance(included, str):
                names.append(included)
    return names


def _site_plays() -> list[dict[str, object]]:
    plays = yaml.safe_load(SITE_YML.read_text(encoding="utf-8"))
    assert isinstance(plays, list)
    return plays


def _task_documents() -> list[dict[str, object]]:
    docs: list[dict[str, object]] = []
    for path in sorted(FLUX_OPERATOR_ROLE_TASKS.glob("*.yml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            docs.extend(item for item in loaded if isinstance(item, dict))
    return docs

def _task_text(task: dict[str, object]) -> str:
    return yaml.safe_dump(task, sort_keys=False)

def test_sops_age_tasks_use_no_log() -> None:
    needles = (
        "SOPS_AGE_KEY_FILE",
        "flux_sops_age_key",
        "age private key",
        "age.agekey",
        # A lookup of a Secret returns its data.
        "kind: secret",
        "flux_sops_age_file_identities",
        "flux_sops_age_publics",
    )
    for task in _task_documents():
        blob = _task_text(task).lower()
        if not any(needle.lower() in blob for needle in needles):
            continue
        assert task.get("no_log") is True, task.get("name", task)
    stat = next(
        task for task in _task_documents() if task.get("name") == "Stat the age private key file"
    )
    assert stat.get("delegate_to") == "localhost"
    assert stat.get("become") is False
    assert stat.get("run_once") is True


def test_untagged_check_run_skips_the_flux_operator_play() -> None:
    """The flux-operator role's condition is false for an untagged --check run (run tags
    ['all']) and true once TAGS=flux-operator is named or the run is not a check."""
    import jinja2

    play = next(p for p in _site_plays() if "flux-operator" in _role_names(p))
    role = next(r for r in play["roles"] if r["role"] == "flux-operator")
    applies = jinja2.Environment().compile_expression(role["when"])
    assert not applies(ansible_check_mode=True, ansible_run_tags=["all"])
    assert applies(ansible_check_mode=True, ansible_run_tags=["flux-operator"])
    assert applies(ansible_check_mode=False, ansible_run_tags=["all"])



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
    return apiserver.Api(apiserver.free_port(), "none").kubeconfig(tmp_path / "unreachable.kubeconfig")


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


def _seal_sops_age(api: apiserver.Api, content: str) -> str:
    """flux-system/sops-age holding content; returns its resourceVersion."""
    doc = _post(api, f"{NS}/secrets", {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "sops-age", "namespace": "flux-system"},
        "data": {"age.agekey": base64.b64encode(content.encode()).decode()},
    })
    return doc["metadata"]["resourceVersion"]


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


def _ansible_env(kubeconfig: Path | None = None) -> dict[str, str]:
    """The role's environment, with only kubeconfig's server to reach (none when no task needs one)."""
    env = {key: value for key, value in os.environ.items() if key.lower() not in ("https_proxy", "http_proxy", "all_proxy")}
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    # kubernetes.core reads this when a task names no kubeconfig; the full role names it.
    env["K8S_AUTH_KUBECONFIG"] = env["KUBECONFIG"] = str(kubeconfig or "/nonexistent/kubeconfig")
    env.pop("SOPS_AGE_KEY_FILE", None)
    return env


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _flux_operator_extra(kubeconfig: Path) -> list[str]:
    cluster = load_cluster_vars()
    return [
        "-e", f"k3s_kubeconfig_path={kubeconfig}",
        "-e", f"jarvis_env={cluster['jarvis_env']}",
        "-e", f"flux_git_repo_url={cluster['flux_git_repo_url']}",
        "-e", f"flux_git_ref={cluster['flux_git_ref']}",
        # A wait that cannot pass fails in seconds, not minutes.
        "-e", "flux_operator_crd_wait_timeout=20",
        "-e", "flux_operator_rollout_wait_timeout=20",
        "-e", "flux_crd_wait_timeout=20",
    ]


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


def _play(name: str, tasks_from: str | None = None) -> str:
    """A localhost play that imports the role, or one of its task files."""
    target = f"\n                tasks_from: {tasks_from}" if tasks_from else ""
    return textwrap.dedent(
        f"""\
        ---
        - name: {name}
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator{target}
        """
    )


def _run_localhost_role(
    play_text: str,
    tmp_path: Path,
    extra: list[str],
    *,
    env: dict[str, str],
    check: bool = False,
    inventory: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    play = tmp_path / "play.yml"
    play.write_text(play_text, encoding="utf-8")
    cmd = ["ansible-playbook", str(play)]
    for source in inventory or ["localhost,"]:
        cmd.extend(["-i", source])
    # The venv's Python, which holds kubernetes.core's client.
    cmd.extend(["-e", "ansible_connection=local", "-e", f"ansible_python_interpreter={sys.executable}", *extra])
    if check:
        cmd.append("--check")
    return subprocess.run(cmd, cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False, env=env)


def test_a_converged_cluster_changes_nothing(api: apiserver.Api, tmp_path: Path) -> None:
    """With the operator Available, sops-age present and Flux syncing, a check run passes twice and changes nothing."""
    require_cli("ansible-playbook")
    _operator_running(api)
    _seal_sops_age(api, "an existing key")
    _root_source(api)
    extra = [*_flux_operator_extra(api.config), *_key_vars(tmp_path / "missing-age-key")]
    env = _ansible_env(api.config)
    inventory = ["localhost,", str(_backup_inventory(tmp_path))]
    for _ in range(2):
        proc = _run_localhost_role(_play("Flux operator already installed"), tmp_path, extra, env=env, check=True, inventory=inventory)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert playbook_recap_field(proc.stdout + proc.stderr, "changed") == 0
    assert _get(api, INSTANCE) is None



_SOPS_ENV = "env-test"
_SOPS_PLAY = textwrap.dedent(
    """\
    ---
    - name: sops-age checks, then the bootstrap secret
      hosts: localhost
      gather_facts: false
      become: false
      tasks:
        - ansible.builtin.import_role:
            name: flux-operator
            tasks_from: sops_age.yml
        - ansible.builtin.import_role:
            name: flux-operator
            tasks_from: sops_age_secret.yml
    """
)


def _failed_task(output: str) -> str | None:
    """The name of the task whose result is the first `fatal:` line."""
    task = None
    for line in output.splitlines():
        header = re.match(r"TASK \[(?:[^:\]]+ : )?(.*)\] \**$", line)
        if header:
            task = header.group(1)
        elif line.startswith("fatal:"):
            return task
    return None


def _age_identity(tmp_path: Path, name: str) -> tuple[str, str]:
    """A throwaway age identity line and its public key. Nothing is printed."""
    require_cli("age-keygen")
    path = tmp_path / f"{name}.agekey"
    proc = subprocess.run(
        ["age-keygen", "-o", str(path)], capture_output=True, text=True, check=True
    )
    public = re.search(r"age1[0-9a-z]+", proc.stderr)
    assert public is not None
    lines = path.read_text(encoding="utf-8").splitlines()
    identity = next(line for line in lines if line.startswith("AGE-SECRET-KEY-1"))
    return identity, public.group(0)


def _key_file(tmp_path: Path, *identities: tuple[str, str]) -> Path:
    """A key file in age-keygen's layout: comment lines, then each identity."""
    path = tmp_path / "keys.txt"
    body = "".join(
        f"# created: 2026-01-01T00:00:00Z\n# public key: {public}\n{identity}\n"
        for identity, public in identities
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def _key_vars(keyfile: Path, *recipients: str) -> list[str]:
    """What jarvis site resolves for the role: the working key's path and the env's recipients."""
    return ["-e", json.dumps({"sops_age_key_file": str(keyfile), "sops_age_recipients": list(recipients)})]


def _sops_config(tmp_path: Path, env: str, *publics: str) -> Path:
    """A SOPS config whose rule for env names publics."""
    path = tmp_path / "sops-config.yaml"
    rule = {
        "path_regex": f"envs/{env}/secrets/.*\\.sops\\.yaml$",
        "encrypted_regex": "^(data|stringData)$",
        "age": ",".join(publics),
    }
    path.write_text(yaml.safe_dump({"creation_rules": [rule]}), encoding="utf-8")
    return path


def _run_sops_age(api: apiserver.Api, tmp_path: Path, keyfile: Path, *recipients: str) -> subprocess.CompletedProcess[str]:
    args = ["-e", f"jarvis_env={_SOPS_ENV}", *_key_vars(keyfile, *recipients)]
    return _run_localhost_role(_SOPS_PLAY, tmp_path, args, env=_ansible_env(api.config))


def test_existing_sops_age_skips_key_when_unset(api: apiserver.Api, tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    version = _seal_sops_age(api, "an existing key")
    env = _ansible_env(api.config)
    extra = _key_vars(tmp_path / "missing-age-key")
    proc = _run_localhost_role(_play("Re-run sops-age without the private key", "sops_age.yml"), tmp_path, extra, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _get(api, _secret_path("sops-age"))["metadata"]["resourceVersion"] == version


def test_new_sops_age_secret_holds_only_the_env_identity(api: apiserver.Api, tmp_path: Path) -> None:
    """A new flux-system/sops-age holds only the identity for the env's recipients."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    mine = _age_identity(tmp_path, "mine")
    second_recipient = _age_identity(tmp_path, "second")
    keyfile = _key_file(tmp_path, other, mine)
    proc = _run_sops_age(api, tmp_path, keyfile, second_recipient[1], mine[1])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _secret_data(api, "sops-age") == {"age.agekey": mine[0]}
    assert mine[0] not in proc.stdout + proc.stderr


@pytest.mark.parametrize("case", ["full-key-file", "env-identity-only"])
def test_existing_secret_with_the_env_identity_passes_untouched(api: apiserver.Api, tmp_path: Path, case: str) -> None:
    """The refusal compares identities: a secret holding the env's one, with or without other lines, is left alone."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    mine = _age_identity(tmp_path, "mine")
    keyfile = _key_file(tmp_path, other, mine)
    version = _seal_sops_age(api, keyfile.read_text(encoding="utf-8") if case == "full-key-file" else mine[0])
    proc = _run_sops_age(api, tmp_path, keyfile, mine[1])
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert playbook_recap_field(output, "changed") == 0
    assert _get(api, _secret_path("sops-age"))["metadata"]["resourceVersion"] == version


def test_two_matching_identities_are_one_line_each(api: apiserver.Api, tmp_path: Path) -> None:
    """Two identities for the env's recipients land on two lines, and the set check uses both."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    first = _age_identity(tmp_path, "first")
    second = _age_identity(tmp_path, "second")
    keyfile = _key_file(tmp_path, other, first, second)
    recipients = (first[1], second[1])

    proc = _run_sops_age(api, tmp_path, keyfile, *recipients)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _secret_data(api, "sops-age")["age.agekey"].splitlines() == [first[0], second[0]]

    # The whole key file, comments and a third identity included, holds both.
    api.request("DELETE", _secret_path("sops-age"))
    version = _seal_sops_age(api, keyfile.read_text(encoding="utf-8"))
    proc = _run_sops_age(api, tmp_path, keyfile, *recipients)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _get(api, _secret_path("sops-age"))["metadata"]["resourceVersion"] == version

    # A secret with only one of the two does not hold the env's identities.
    api.request("DELETE", _secret_path("sops-age"))
    _seal_sops_age(api, first[0])
    proc = _run_sops_age(api, tmp_path, keyfile, *recipients)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == "Refuse to overwrite an existing sops-age secret with different content", output
    assert _secret_data(api, "sops-age") == {"age.agekey": first[0]}


def test_existing_secret_with_another_identity_refuses(api: apiserver.Api, tmp_path: Path) -> None:
    """A secret without the env's identity may be the only copy of another key, so the run refuses."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    mine = _age_identity(tmp_path, "mine")
    existing = f"# public key: {other[1]}\n{other[0]}"
    _seal_sops_age(api, existing)
    proc = _run_sops_age(api, tmp_path, _key_file(tmp_path, mine), mine[1])
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == "Refuse to overwrite an existing sops-age secret with different content", output
    assert mine[0] not in output and other[0] not in output
    assert _secret_data(api, "sops-age") == {"age.agekey": existing}


def test_key_file_without_the_env_identity_refuses_before_the_operator_install(api: apiserver.Api, tmp_path: Path) -> None:
    """The full role stops at the age key check, before Helm or any cluster write."""
    require_cli("ansible-playbook")
    other = _age_identity(tmp_path, "other")
    recipient = _age_identity(tmp_path, "recipient")
    keyfile = _key_file(tmp_path, other)
    helm_log = tmp_path / "helm.log"
    helm = tmp_path / "helm"
    _write_executable(helm, f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> {helm_log}\nexit 0\n")
    extra = [*_flux_operator_extra(api.config), "-e", f"helm_binary_path={helm}", *_key_vars(keyfile, recipient[1])]
    proc = _run_localhost_role(_play("Full flux-operator role"), tmp_path, extra, env=_ansible_env(api.config))
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == "Refuse a key file with no identity for the env's age recipient", output
    assert other[0] not in output
    assert "Check whether Flux Operator deployment exists" not in output
    assert not helm_log.exists()
    assert _get(api, _secret_path("sops-age")) is None


def _assert_key_was_not_read(proc: subprocess.CompletedProcess[str], sentinel: str) -> None:
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "Refuse an age private key" in output
    assert sentinel not in output
    assert "Derive the public key of each identity" not in output


def test_missing_age_keygen_is_named_before_no_log(api: apiserver.Api, tmp_path: Path) -> None:
    """A missing age-keygen fails in a visible task that names the binary."""
    playbook = Path(require_cli("ansible-playbook"))
    keyfile = tmp_path / "age.key"
    keyfile.write_text("not-read\n", encoding="utf-8")
    keyfile.chmod(0o600)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Hide age-keygen only. ansible-playbook stays, including when it lives
    # outside /bin and /usr/bin, which is where CI's venv puts it.
    for directory in (Path("/bin"), Path("/usr/bin"), playbook.parent):
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
        (bindir / "ansible-playbook").symlink_to(playbook)
    env = _ansible_env(api.config)
    env["PATH"] = str(bindir)
    proc = _run_localhost_role(_play("Missing age-keygen", "sops_age.yml"), tmp_path, _key_vars(keyfile), env=env)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == "Require age-keygen on the controller", output
    assert "age-keygen is not on PATH" in output
    assert "not-read" not in output
    assert "Derive the public key" not in output


def test_sops_age_refuses_mode_0644_without_reading_the_key(api: apiserver.Api, tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    sentinel = "SENTINEL-AGE-KEY-NOT-READ"
    keyfile = tmp_path / "age.key"
    keyfile.write_text(sentinel + "\n", encoding="utf-8")
    keyfile.chmod(0o644)
    env = _ansible_env(api.config)
    proc = _run_localhost_role(_play("Refuse a world-readable age key", "sops_age.yml"), tmp_path, _key_vars(keyfile), env=env)
    _assert_key_was_not_read(proc, sentinel)
    assert _get(api, _secret_path("sops-age")) is None


def test_sops_age_refuses_wrong_owner_from_stat(api: apiserver.Api, tmp_path: Path) -> None:
    """Stat uid is faked when this user cannot chown a file to someone else."""
    require_cli("ansible-playbook")
    sentinel = "SENTINEL-AGE-KEY-NOT-READ"
    other = os.getuid() + 1
    keyfile = tmp_path / "age.key"
    keyfile.write_text(sentinel + "\n", encoding="utf-8")
    keyfile.chmod(0o600)
    try:
        os.chown(keyfile, other, os.getgid())
    except OSError:
        chowned = False
    else:
        chowned = keyfile.stat().st_uid == other
    env = _ansible_env(api.config)
    if chowned:
        proc = _run_localhost_role(_play("Refuse an age key owned by someone else", "sops_age.yml"), tmp_path, _key_vars(keyfile), env=env)
        _assert_key_was_not_read(proc, sentinel)
        assert _get(api, _secret_path("sops-age")) is None
        return
    play = textwrap.dedent(
        f"""\
        ---
        - name: Fake age key stat for a different owner
          hosts: localhost
          gather_facts: false
          become: false
          vars:
            sops_age_key_stat:
              stat:
                exists: true
                mode: "0600"
                uid: {other}
          tasks:
            - ansible.builtin.include_role:
                name: flux-operator
                tasks_from: sops_age_key_mode.yml
        """
    )
    proc = _run_localhost_role(play, tmp_path, [], env=env)
    _assert_key_was_not_read(proc, sentinel)


def test_sops_age_lookup_error_does_not_bootstrap(tmp_path: Path) -> None:
    """An API that does not answer is not an absent secret: the role stops at its first call, which
    reads no secret, so its error shows and nothing is read or created."""
    require_cli("ansible-playbook")
    sentinel = "SENTINEL-AGE-KEY-NOT-READ"
    keyfile = tmp_path / "age.key"
    keyfile.write_text(sentinel + "\n", encoding="utf-8")
    keyfile.chmod(0o600)
    env = _ansible_env(_unreachable(tmp_path))
    proc = _run_localhost_role(_SOPS_PLAY, tmp_path, _key_vars(keyfile), env=env)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == "Reach the Kubernetes API", output
    assert sentinel not in output


def _become_exe(tmp_path: Path) -> Path:
    path = tmp_path / "sudo"
    _write_executable(
        path,
        textwrap.dedent(
            """\
            #!/bin/bash
            # Ansible become: sudo -H -S -n -u root <command>
            skip=0
            args=()
            for arg in "$@"; do
              if [[ "$skip" -eq 1 ]]; then
                skip=0
                continue
              fi
              case "$arg" in
                -u) skip=1 ;;
                -H|-S|-n) ;;
                *) args+=("$arg") ;;
              esac
            done
            exec "${args[@]}"
            """
        ),
    )
    return path


def _restic_data(tmp_path: Path) -> list[str]:
    """A backup host's restic tree: one PVC repository and one that is not; extra vars for it."""
    restic = tmp_path / "backup" / "restic"
    for repo, content in (("ns-demo/pvc-drill", "repo"), ("ns-decoy/notpvc", "nope")):
        (restic / repo).mkdir(parents=True)
        (restic / repo / "config").write_text(content + "\n", encoding="utf-8")
    return ["-e", f"backup_path={tmp_path / 'backup'}", "-e", f"ansible_become_exe={_become_exe(tmp_path)}"]


def _instance_extra(api_config: Path, tmp_path: Path) -> list[str]:
    return [
        *_flux_operator_extra(api_config), *_restic_data(tmp_path),
        "-e", f"flux_instance_file={example_flux_entry(tmp_path).instance_file}",
        "-e", f"flux_sync_file={sample_root() / 'envs' / env_name() / 'flux' / 'sync.yaml'}",
    ]


def test_rerun_with_root_source_present_does_not_set_rebuild_hold(api: apiserver.Api, tmp_path: Path) -> None:
    """Restic data on the backup host holds nothing back once Flux syncs: no hold, nothing applied."""
    require_cli("ansible-playbook")
    _root_source(api)
    env = _ansible_env(api.config)
    inventory = ["localhost,", str(_backup_inventory(tmp_path))]
    extra = _instance_extra(api.config, tmp_path)
    for _ in range(2):
        proc = _run_localhost_role(_play("Root GitRepository already present", "instance.yml"), tmp_path, extra,
                                   env=env, inventory=inventory)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert playbook_recap_field(proc.stdout + proc.stderr, "changed") == 0
    assert _get(api, HOLD) is None
    assert _get(api, INSTANCE) is None


def test_fresh_cluster_sets_the_hold_then_applies_the_flux_instance_and_root_source(api: apiserver.Api, tmp_path: Path) -> None:
    """On a new cluster: the hold, then the FluxInstance, then sync.yaml with the root source."""
    require_cli("ansible-playbook")
    extra = _instance_extra(api.config, tmp_path)
    proc = _run_localhost_role(_play("Fresh cluster", "instance.yml"), tmp_path, extra, env=_ansible_env(api.config),
                               inventory=["localhost,", str(_backup_inventory(tmp_path))])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _get(api, HOLD)["data"] == {"repos": "ns-demo/pvc-drill\n"}
    instance = yaml.safe_load(example_flux_entry(tmp_path).instance_file.read_text(encoding="utf-8"))
    assert _get(api, INSTANCE)["spec"] == instance["spec"]
    sync = list(yaml.safe_load_all((sample_root() / "envs" / env_name() / "flux" / "sync.yaml").read_text(encoding="utf-8")))
    root = next(doc for doc in sync if doc["kind"] == "GitRepository" and doc["metadata"]["name"] == "flux-system")
    assert _get(api, f"{SOURCES}/flux-system")["spec"] == root["spec"]
    assert _get(api, f"{SOURCES}/core") is not None
    assert _version(api, HOLD) < _version(api, INSTANCE) < _version(api, f"{SOURCES}/flux-system") <= _version(api, ROOT)


def test_root_source_lookup_error_does_not_set_the_hold(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    unreachable = _unreachable(tmp_path)
    proc = _run_localhost_role(_play("API error", "instance.yml"), tmp_path, _instance_extra(unreachable, tmp_path),
                               env=_ansible_env(unreachable), inventory=["localhost,", str(_backup_inventory(tmp_path))])
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert _failed_task(output) == "Check whether the root GitRepository exists", output
    assert "Stat restic data on the backup host" not in output


# The schema's default for flux_known_hosts, which jarvis site passes.
_GITHUB_KEY = defaults("cluster-vars")["flux_known_hosts"]
_DEPLOY_KEY_PLAY = textwrap.dedent(
    """\
    ---
    - name: Deploy key
      hosts: localhost
      gather_facts: false
      become: false
      tasks:
        - ansible.builtin.import_role:
            name: flux-operator
            tasks_from: sops_age_key_file.yml
        - ansible.builtin.import_role:
            name: flux-operator
            tasks_from: deploy_key.yml
    """
)


def test_fresh_cluster_creates_the_deploy_key_secret_once(api: apiserver.Api, tmp_path: Path) -> None:
    """An ssh:// root's sealed deploy key becomes flux-system/flux-deploy-key once, with GitHub's host key."""
    require_cli("ansible-playbook")
    require_cli("sops")
    from jarvis.secrets.sops import encrypt_secret_to_file

    identity, public = _age_identity(tmp_path, "env")
    keyfile = _key_file(tmp_path, (identity, public))
    instance = tmp_path / "instance"
    (instance / "envs" / _SOPS_ENV).mkdir(parents=True)
    _sops_config(tmp_path, _SOPS_ENV, public).rename(instance / ".sops.yaml")
    private = "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----"
    encrypt_secret_to_file(
        repo_root=instance, env=_SOPS_ENV, name="flux-deploy-key", sops_bin="sops",
        document={"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": "flux-deploy-key", "namespace": "flux-system"},
                  "stringData": {"identity": private}},
    )
    sealed = instance / "envs" / _SOPS_ENV / "secrets" / "flux-deploy-key.sops.yaml"
    env = _ansible_env(api.config)
    extra = ["-e", json.dumps({"flux_deploy_key_file": str(sealed), "flux_known_hosts": _GITHUB_KEY}), *_key_vars(keyfile)]
    versions = []
    for _ in range(2):
        proc = _run_localhost_role(_DEPLOY_KEY_PLAY, tmp_path, extra, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert private not in proc.stdout + proc.stderr
        versions.append(_version(api, _secret_path("flux-deploy-key")))
    assert versions[0] == versions[1]
    assert _get(api, _secret_path("flux-deploy-key"))["metadata"]["annotations"] == {"kustomize.toolkit.fluxcd.io/reconcile": "disabled"}
    assert _secret_data(api, "flux-deploy-key") == {
        "identity": private,
        "known_hosts": _GITHUB_KEY,
    }


def test_known_hosts_is_a_github_published_host_key() -> None:
    """flux_known_hosts' default pins a key GitHub publishes (api.github.com/meta), so Flux trusts github.com only."""
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
    return (
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        f'if [[ "$1" == version ]]; then echo "v{version}+gfixture"; fi\n'
        "exit 0\n"
    )


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


def test_helm_cli_with_another_version_is_replaced_then_left_alone(tmp_path: Path) -> None:
    """A helm reporting another version is replaced from the pinned archive; the next run downloads nothing."""
    require_cli("ansible-playbook")
    log = tmp_path / "helm.log"
    installed = tmp_path / "bin" / "helm"
    installed.parent.mkdir()
    _write_executable(installed, _fake_helm(log, "3.0.0"))
    pinned = _fake_helm(log, _pinned_helm_version())
    archive, digest = _helm_archive(tmp_path, pinned)
    play = textwrap.dedent(
        """\
        ---
        - name: Helm CLI
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: helm_cli.yml
        """
    )

    def run(url: str) -> subprocess.CompletedProcess[str]:
        return _run_localhost_role(
            play,
            tmp_path,
            [
                "-e", f"helm_binary_path={installed}",
                "-e", f"helm_download_url={url}",
                "-e", f"helm_linux_amd64_tarball_sha256={digest}",
                # Defaults are root:root. This play does not become root.
                "-e", f"helm_binary_owner={getpass.getuser()}",
                "-e", f"helm_binary_group={grp.getgrgid(os.getgid()).gr_name}",
            ],
            env=_ansible_env(),
        )

    first = run(archive.as_uri())
    assert first.returncode == 0, first.stdout + first.stderr
    assert installed.read_text(encoding="utf-8") == pinned
    # The archive URL now points at nothing, so a download would fail the run.
    second = run((tmp_path / "absent.tar.gz").as_uri())
    output = second.stdout + second.stderr
    assert second.returncode == 0, output
    assert playbook_recap_field(output, "changed") == 0


def _installing_helm(api: apiserver.Api, log: Path) -> str:
    """A helm at the pinned version whose install leaves the operator's Deployment Available, as
    the chart and a kubelet would."""
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "flux-operator", "namespace": "flux-system"},
        "spec": {"selector": {"matchLabels": {"app": "op"}},
                 "template": {"metadata": {"labels": {"app": "op"}}, "spec": {"containers": [{"name": "m", "image": "op"}]}}},
    }
    return textwrap.dedent(
        f"""\
        #!{sys.executable}
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
    )


@pytest.mark.parametrize("kind", ["absent", "unreachable"])
def test_operator_lookup_installs_only_when_absent(api: apiserver.Api, tmp_path: Path, kind: str) -> None:
    """An absent operator Deployment installs the chart. An API that does not answer does not."""
    require_cli("ansible-playbook")
    helm_log = tmp_path / "helm.log"
    helm = tmp_path / "helm"
    _write_executable(helm, _installing_helm(api, helm_log))
    config = api.config if kind == "absent" else _unreachable(tmp_path)
    extra = [*_flux_operator_extra(config), "-e", f"helm_binary_path={helm}", "-e", "flux_operator_chart_version=0.0.0-test"]
    proc = _run_localhost_role(_play("Operator lookup", "operator.yml"), tmp_path, extra, env=_ansible_env(config))
    output = proc.stdout + proc.stderr
    if kind == "absent":
        assert proc.returncode == 0, output
        assert "upgrade --install flux-operator" in helm_log.read_text(encoding="utf-8")
        return
    assert proc.returncode != 0, output
    assert _failed_task(output) == "Check whether Flux Operator deployment exists", output
    assert not helm_log.exists()
