"""flux-operator Ansible role: template rules and check-mode proof (p0a-13)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    CONTAINER_NAME,
    DOCKER_PLAYBOOK_EXTRA_VARS,
    FLUX_OPERATOR_ROLE_TASKS,
    FLUX_OPERATOR_ROLE_TEMPLATES,
    ansible_render_template,
    cluster_vars_playbook_path,
    control_plane_inventory,
    docker_exec,
    flux_operator_check_playbook_cmd,
    playbook_recap_field,
    require_docker,
    start_systemd_container,
    stop_systemd_container,
)
from conftest import require_cli

SITE_YML = ANSIBLE_DIR / "site.yml"


def test_sops_age_secret_template_not_reconciled_from_git() -> None:
    key_body = "AGE-SECRET-KEY-1TEST\n"
    rendered = ansible_render_template(
        "sops-age-secret.yaml.j2",
        {
            "flux_system_namespace": "flux-system",
            "flux_sops_age_key_content": key_body,
        },
        templates_dir=FLUX_OPERATOR_ROLE_TEMPLATES,
    )
    assert "name: sops-age" in rendered
    assert "namespace: flux-system" in rendered
    assert "kustomize.toolkit.fluxcd.io/reconcile: disabled" in rendered


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
        for key in ("ansible.builtin.include_role", "include_role", "ansible.builtin.import_role", "import_role"):
            included = task.get(key)
            if isinstance(included, dict) and isinstance(included.get("name"), str):
                names.append(included["name"])
            elif isinstance(included, str):
                names.append(included)
    return names


def test_site_yml_role_order_and_ssh_user() -> None:
    plays = yaml.safe_load(SITE_YML.read_text(encoding="utf-8"))
    assert isinstance(plays, list)
    assert [play.get("hosts") for play in plays] == [
        "all",
        "all",
        "data_disk",
        "control_plane",
        "all:!control_plane",
        "control_plane",
        "control_plane",
        "control_plane",
    ]
    for play in plays:
        assert play.get("become") is True
        assert play.get("vars", {}).get("ansible_user") == "{{ ssh_user }}"
    assert _role_names(plays[0]) == []
    assert plays[0].get("tags") == ["always"]
    assert _role_names(plays[1]) == ["base-os"]
    assert _role_names(plays[2]) == ["node-disk"]
    assert _role_names(plays[3]) == ["k3s"]
    assert plays[3]["roles"][0]["vars"]["k3s_mode"] == "server"
    assert _role_names(plays[4]) == ["k3s"]
    assert plays[4]["roles"][0]["vars"]["k3s_mode"] == "agent"
    assert _role_names(plays[5]) == ["k3s"]
    assert _role_names(plays[6]) == ["kubeconfig"]
    assert _role_names(plays[7]) == ["flux-operator"]
    assert plays[3].get("tags") == ["k3s"]
    assert plays[4].get("tags") == ["k3s"]
    assert plays[5].get("tags") == ["k3s"]
    assert plays[6].get("tags") == ["kubeconfig"]
    assert plays[7].get("tags") == ["flux-operator"]


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
        "flux_sops_age_existing_json",
        "flux_sops_age_existing_key_content",
    )
    for task in _task_documents():
        blob = _task_text(task).lower()
        if not any(needle.lower() in blob for needle in needles):
            continue
        assert task.get("no_log") is True, task.get("name", task)


def _generate_throwaway_age_key(tmp_path: Path) -> tuple[Path, str]:
    key_body = textwrap.dedent(
        """\
        # created: 2026-01-01T00:00:00Z
        # public key: age1prooffixture000000000000000000000000000000000000000000
        AGE-SECRET-KEY-1PROOF00000000000000000000000000000000000000000000000000
        """
    )
    keyfile = tmp_path / "age.key"
    keyfile.write_text(key_body, encoding="utf-8")
    return keyfile, key_body


def _install_fake_k3s_with_flux_done(secret_b64: str) -> None:
    secret_json = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "sops-age", "namespace": "flux-system"},
            "data": {"age.agekey": secret_b64},
        },
        separators=(",", ":"),
    )
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
            *"get secret sops-age"*" -o json"*)
              printf '%s' '{secret_json}'
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


@pytest.mark.container
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
                control_plane_inventory(CONTAINER_NAME),
                "-e",
                f"jarvis_cluster_vars={cluster_vars_playbook_path()}",
                "-e",
                "ansible_connection=docker",
                *DOCKER_PLAYBOOK_EXTRA_VARS,
                "--skip-tags",
                "chrony-service,k3s,kubeconfig,flux-operator",
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


def test_existing_sops_age_skips_key_when_unset(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    binary.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        'if [[ "$*" == *"get secret sops-age -o name" ]]; then\n'
        "  echo secret/sops-age\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected k3s: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    play = tmp_path / "play.yml"
    play.write_text(
        textwrap.dedent(
            """\
            ---
            - name: Re-run sops-age without the private key
              hosts: localhost
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_role:
                    name: flux-operator
                    tasks_from: sops_age.yml
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("SOPS_AGE_KEY_FILE", None)
    env["ANSIBLE_BECOME"] = "false"
    proc = subprocess.run(
        [
            "ansible-playbook",
            str(play),
            "-i",
            "localhost,",
            "-e",
            "ansible_connection=local",
            "-e",
            f"k3s_binary_path={binary}",
        ],
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    invocations = log.read_text(encoding="utf-8")
    assert "get secret sops-age -o name" in invocations
    assert "-o json" not in invocations
    assert "create" not in invocations


def test_sops_age_secret_task_uses_kubectl_create_not_apply() -> None:
    content = (FLUX_OPERATOR_ROLE_TASKS / "sops_age.yml").read_text(encoding="utf-8")
    assert "kubectl create" in content
    assert "kubectl apply" not in content
