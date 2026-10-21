"""flux-operator Ansible role: template rules and check-mode proof (p0a-13)."""

from __future__ import annotations

import subprocess
import textwrap

import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    CONTAINER_NAME,
    FLUX_OPERATOR_ROLE_TASKS,
    FLUX_OPERATOR_ROLE_TEMPLATES,
    ansible_render_template,
    docker_exec,
)

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


def test_site_yml_role_order_and_ssh_user() -> None:
    play = yaml.safe_load(SITE_YML.read_text(encoding="utf-8"))
    assert isinstance(play, list)
    assert len(play) == 1
    entry = play[0]
    assert entry.get("become") is True
    assert entry.get("vars", {}).get("ansible_user") == "{{ ssh_user }}"
    roles = entry.get("roles")
    assert roles == ["base-os", "node-disk", "k3s", "flux-operator"]


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


def test_sops_age_secret_task_uses_kubectl_create_not_apply() -> None:
    content = (FLUX_OPERATOR_ROLE_TASKS / "sops_age.yml").read_text(encoding="utf-8")
    assert "kubectl create" in content
    assert "kubectl apply" not in content
