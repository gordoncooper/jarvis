"""flux-operator Ansible role: template renders and proof (p0a-13)."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    CONTAINER_NAME,
    FLUX_OPERATOR_ROLE_DIR,
    FLUX_OPERATOR_ROLE_TASKS,
    FLUX_OPERATOR_ROLE_TEMPLATES,
    ansible_render_template,
    cluster_vars_playbook_path,
    docker_exec,
    env_name,
    DOCKER_PLAYBOOK_EXTRA_VARS,
    flux_operator_check_playbook_cmd,
    inventory_path,
    load_cluster_vars,
    playbook_recap_field,
    require_docker,
    start_systemd_container,
    stop_systemd_container,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SITE_YML = ANSIBLE_DIR / "site.yml"

FLUX_INSTANCE_EXPECTED = textwrap.dedent(
    """\
    # Managed by Ansible (flux-operator role): one-time FluxInstance (R1); Flux manages it afterward.
    apiVersion: fluxcd.controlplane.io/v1
    kind: FluxInstance
    metadata:
      name: flux
      namespace: flux-system
    spec:
      distribution:
        version: "2.8.x"
        registry: ghcr.io/fluxcd
      components:
        - source-controller
        - source-watcher
        - kustomize-controller
        - helm-controller
        - notification-controller
        - image-reflector-controller
        - image-automation-controller
      cluster:
        type: kubernetes
        size: medium
        multitenant: false
        networkPolicy: true
        domain: cluster.local
      sync:
        kind: GitRepository
        url: "https://github.com/gordoncooper/jarvis"
        ref: "refs/heads/main"
        path: "envs/{jarvis_env}/flux"
    """
)


def _render_flux_instance(jarvis_env: str) -> str:
    return ansible_render_template(
        "flux-instance.yaml.j2",
        {
            "flux_instance_name": "flux",
            "flux_system_namespace": "flux-system",
            "flux_distribution_version": "2.8.x",
            "flux_git_repo_url": "https://github.com/gordoncooper/jarvis",
            "flux_git_ref": "refs/heads/main",
            "jarvis_env": jarvis_env,
        },
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    )


def test_flux_instance_template_exact_content() -> None:
    jarvis_env = env_name()
    rendered = _render_flux_instance(jarvis_env)
    assert rendered == FLUX_INSTANCE_EXPECTED.format(jarvis_env=jarvis_env)


def test_flux_instance_template_quoted_jarvis_env() -> None:
    jarvis_env = env_name()
    quoted = yaml.safe_dump(jarvis_env).strip()
    loaded = yaml.safe_load(quoted)
    assert isinstance(loaded, str)
    rendered = _render_flux_instance(loaded)
    assert rendered == FLUX_INSTANCE_EXPECTED.format(jarvis_env=jarvis_env)


def test_sops_age_secret_template_exact_content() -> None:
    key_body = textwrap.dedent(
        """\
        # created: 2026-01-01T00:00:00Z
        # public key: age1example
        AGE-SECRET-KEY-1TEST0000000000000000000000000000000000000000000000000000
        """
    )
    rendered = ansible_render_template(
        "sops-age-secret.yaml.j2",
        {
            "flux_system_namespace": "flux-system",
            "flux_sops_age_key_content": key_body,
        },
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    )
    assert rendered == textwrap.dedent(
        f"""\
        # Managed by Ansible (flux-operator role): bootstrap SOPS age key (R25); not reconciled from Git.
        apiVersion: v1
        kind: Secret
        metadata:
          name: sops-age
          namespace: flux-system
          annotations:
            kustomize.toolkit.fluxcd.io/reconcile: disabled
        type: Opaque
        stringData:
          age.agekey: |-
            # created: 2026-01-01T00:00:00Z
            # public key: age1example
            AGE-SECRET-KEY-1TEST0000000000000000000000000000000000000000000000000000
        """
    )


def test_site_yml_role_order_and_ssh_user() -> None:
    play = yaml.safe_load(SITE_YML.read_text(encoding="utf-8"))
    assert isinstance(play, list)
    assert len(play) == 1
    entry = play[0]
    assert entry.get("become") is True
    assert entry.get("vars", {}).get("ansible_user") == "{{ ssh_user }}"
    roles = entry.get("roles")
    assert roles == ["base-os", "k3s", "flux-operator"]


def test_site_yml_syntax_check_includes_flux_operator() -> None:
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook not installed")
    cmd = [
        "ansible-playbook",
        "--syntax-check",
        "site.yml",
        "-i",
        str(inventory_path().resolve()),
        "-e",
        f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
    ]
    proc = subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


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
        "flux_sops_age_existing_b64",
    )
    for task in _task_documents():
        blob = _task_text(task).lower()
        if not any(needle.lower() in blob for needle in needles):
            continue
        assert task.get("no_log") is True, task.get("name", task)


def _generate_throwaway_age_key(tmp_path: Path) -> tuple[Path, str]:
    age_keygen = shutil.which("age-keygen")
    if age_keygen is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "age.key"
    subprocess.run(
        [age_keygen, "-o", str(keyfile)],
        check=True,
        capture_output=True,
        text=True,
    )
    return keyfile, keyfile.read_text(encoding="utf-8")


def _install_fake_k3s_with_flux_done(secret_b64: str) -> None:
    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail
        if [[ "${{1:-}}" == "kubectl" ]]; then
          shift
          joined="$*"
          case "$joined" in
            *"get deployment flux-operator"*)
              echo "deployment.apps/flux-operator"
              exit 0
              ;;
            *"get fluxinstance flux"*)
              echo "fluxinstance.fluxcd.controlplane.io/flux"
              exit 0
              ;;
            *"get secret sops-age"*"jsonpath"*)
              printf '%s' '{secret_b64}'
              exit 0
              ;;
            *"get secret sops-age"*)
              echo "secret/sops-age"
              exit 0
              ;;
          esac
        fi
        echo "unexpected k3s invocation: $*" >&2
        exit 1
        """
    )
    docker_exec(
        CONTAINER_NAME,
        "bash",
        "-lc",
        "install -d /usr/local/bin && "
        f"cat > /usr/local/sbin/k3s-wrapper <<'EOF'\n{script}\nEOF\n"
        "chmod 0755 /usr/local/sbin/k3s-wrapper && "
        "if [[ -x /usr/local/bin/k3s && ! -f /usr/local/bin/k3s.real ]]; then "
        "mv /usr/local/bin/k3s /usr/local/bin/k3s.real; fi && "
        "ln -sf /usr/local/sbin/k3s-wrapper /usr/local/bin/k3s",
    )


def _install_fake_k3s_without_secret() -> None:
    script = textwrap.dedent(
        """\
        #!/bin/bash
        set -euo pipefail
        if [[ "${1:-}" == "kubectl" ]]; then
          shift
          joined="$*"
          case "$joined" in
            *"get deployment flux-operator"*)
              echo "deployment.apps/flux-operator"
              exit 0
              ;;
            *"get fluxinstance flux"*)
              echo "fluxinstance.fluxcd.controlplane.io/flux"
              exit 0
              ;;
            *"get secret sops-age"*)
              exit 1
              ;;
          esac
        fi
        echo "unexpected k3s invocation: $*" >&2
        exit 1
        """
    )
    docker_exec(
        CONTAINER_NAME,
        "bash",
        "-lc",
        "install -d /usr/local/bin /usr/local/sbin && "
        f"cat > /usr/local/sbin/k3s-wrapper <<'EOF'\n{script}\nEOF\n"
        "chmod 0755 /usr/local/sbin/k3s-wrapper && "
        "ln -sf /usr/local/sbin/k3s-wrapper /usr/local/bin/k3s",
    )


def _run_flux_operator_playbook(
    *,
    env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "ansible-playbook",
        "site.yml",
        "-i",
        f"{CONTAINER_NAME},",
        "-e",
        f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
        "-e",
        "ansible_connection=docker",
        *DOCKER_PLAYBOOK_EXTRA_VARS,
        "--skip-tags",
        "chrony-service,k3s,k3s-service",
    ]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def test_flux_operator_check_mode_idempotent_when_done(tmp_path: Path) -> None:
    require_docker()
    keyfile, key_content = _generate_throwaway_age_key(tmp_path)
    secret_b64 = base64.b64encode(key_content.encode("utf-8")).decode("ascii")
    start_systemd_container()
    try:
        prep = subprocess.run(
            [
                "ansible-playbook",
                "site.yml",
                "-i",
                f"{CONTAINER_NAME},",
                "-e",
                f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
                "-e",
                "ansible_connection=docker",
                *DOCKER_PLAYBOOK_EXTRA_VARS,
                "--skip-tags",
                "chrony-service,k3s,k3s-service,flux-cluster",
            ],
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        assert prep.returncode == 0, prep.stdout + prep.stderr
        _install_fake_k3s_with_flux_done(secret_b64)
        env = {**os.environ, "SOPS_AGE_KEY_FILE": str(keyfile)}
        first = subprocess.run(
            flux_operator_check_playbook_cmd(),
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        second = subprocess.run(
            flux_operator_check_playbook_cmd(),
            cwd=ANSIBLE_DIR,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert first.returncode == 0, first.stdout + first.stderr
        assert second.returncode == 0, second.stdout + second.stderr
        assert playbook_recap_field(second.stdout + second.stderr, "changed") == 0
    finally:
        stop_systemd_container()


def test_sops_age_key_file_unset_fails(tmp_path: Path) -> None:
    require_docker()
    start_systemd_container()
    try:
        _install_fake_k3s_without_secret()
        env = {k: v for k, v in os.environ.items() if k != "SOPS_AGE_KEY_FILE"}
        result = _run_flux_operator_playbook(env=env)
        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert "SOPS_AGE_KEY_FILE" in output
    finally:
        stop_systemd_container()


def test_sops_age_key_file_missing_fails(tmp_path: Path) -> None:
    require_docker()
    missing = tmp_path / "missing-age.key"
    start_systemd_container()
    try:
        _install_fake_k3s_without_secret()
        result = _run_flux_operator_playbook(
            env={**os.environ, "SOPS_AGE_KEY_FILE": str(missing)},
        )
        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert "SOPS_AGE_KEY_FILE" in output or "existing age private key" in output.lower()
    finally:
        stop_systemd_container()


def test_cluster_vars_flux_operator_pin_matches_defaults() -> None:
    cluster = load_cluster_vars()
    defaults = yaml.safe_load(
        (FLUX_OPERATOR_ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert isinstance(defaults, dict)
    assert cluster["flux_operator_version"] == defaults["flux_operator_version"]
    sha = cluster["flux_operator_install_sha256"]
    assert isinstance(sha, str)
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
