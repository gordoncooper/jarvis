"""flux-operator Ansible role: template rules and check-mode proof (p0a-13)."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import yaml

from ansible_role_proof import (
    ANSIBLE_DIR,
    FLUX_OPERATOR_ROLE_TASKS,
    FLUX_OPERATOR_ROLE_TEMPLATES,
    ansible_render_template,
    load_cluster_vars,
    playbook_recap_field,
)
from conftest import require_cli
from schema.env import DEFAULT_SSH_USER

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
        assert "ssh_user | default(" in str(play.get("vars", {}).get("ansible_user"))
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
    connection = plays[0].get("vars")
    assert all(play.get("vars") == connection for play in plays)


def test_site_yml_ssh_user_default_matches_schema() -> None:
    plays = yaml.safe_load(SITE_YML.read_text(encoding="utf-8"))
    expr = plays[0]["vars"]["ansible_user"]
    literal = expr.split("default(", 1)[1].split(")", 1)[0].strip("\"'")
    assert literal == DEFAULT_SSH_USER


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
    stat = next(
        task for task in _task_documents() if task.get("name") == "Stat the age private key file"
    )
    assert stat.get("delegate_to") == "localhost"
    assert stat.get("become") is False
    assert stat.get("run_once") is True


def _ansible_env() -> dict[str, str]:
    env = os.environ.copy()
    env["ANSIBLE_BECOME"] = "false"
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_DIR / "ansible.cfg")
    env.pop("SOPS_AGE_KEY_FILE", None)
    return env


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _flux_operator_extra(binary: Path) -> list[str]:
    cluster = load_cluster_vars()
    return [
        "-e",
        f"k3s_binary_path={binary}",
        "-e",
        f"jarvis_env={cluster['jarvis_env']}",
        "-e",
        f"flux_git_repo_url={cluster['flux_git_repo_url']}",
        "-e",
        f"flux_git_ref={cluster['flux_git_ref']}",
    ]


def _backup_inventory(tmp_path: Path) -> Path:
    path = tmp_path / "backup-hosts.yml"
    path.write_text(
        "all:\n"
        "  children:\n"
        "    backup:\n"
        "      hosts:\n"
        "        backup-host:\n"
        "          ansible_connection: local\n",
        encoding="utf-8",
    )
    return path


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
    cmd.extend(
        [
            "-e",
            "ansible_connection=local",
            *extra,
        ]
    )
    if check:
        cmd.append("--check")
    return subprocess.run(
        cmd,
        cwd=ANSIBLE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_flux_operator_check_mode_idempotent_when_done(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            printf '%s\\n' "$*" >> {log}
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
                echo "secret/sops-age"
                exit 0
                ;;
            esac
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: Flux operator already installed
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
        """
    )
    extra = _flux_operator_extra(binary)
    missing_key = tmp_path / "missing-age-key"
    extra.extend(["-e", f"age_key_file={missing_key}"])
    env = _ansible_env()
    inventory = ["localhost,", str(_backup_inventory(tmp_path))]
    first = _run_localhost_role(play, tmp_path, extra, env=env, check=True, inventory=inventory)
    second = _run_localhost_role(play, tmp_path, extra, env=env, check=True, inventory=inventory)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert playbook_recap_field(second.stdout + second.stderr, "changed") == 0


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
            "-e",
            f"age_key_file={tmp_path / 'missing-age-key'}",
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


def test_sops_age_secret_task_uses_kubectl_create_not_apply(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    keyfile = tmp_path / "age.key"
    keyfile.write_text("fixture-age-key\n", encoding="utf-8")
    keyfile.chmod(0o600)
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> {log}
            joined="$*"
            if [[ "$joined" == *"get secret sops-age -o name"* ]]; then
              exit 1
            fi
            if [[ "$joined" == *"kubectl create"* ]]; then
              exit 0
            fi
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: Create sops-age secret
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: sops_age.yml
        """
    )
    env = _ansible_env()
    env["SOPS_AGE_KEY_FILE"] = str(keyfile)
    proc = _run_localhost_role(play, tmp_path, ["-e", f"k3s_binary_path={binary}"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    invocations = log.read_text(encoding="utf-8")
    assert "kubectl create" in invocations
    assert "kubectl apply" not in invocations


def _assert_key_was_not_read(proc: subprocess.CompletedProcess[str], sentinel: str) -> None:
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "Refuse an age private key" in output
    assert sentinel not in output
    assert "Load age private key from the controller" not in output


def test_sops_age_refuses_mode_0644_without_reading_the_key(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    sentinel = "SENTINEL-AGE-KEY-NOT-READ"
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    keyfile = tmp_path / "age.key"
    keyfile.write_text(sentinel + "\n", encoding="utf-8")
    keyfile.chmod(0o644)
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> {log}
            joined="$*"
            if [[ "$joined" == *"get secret sops-age -o name"* ]]; then
              exit 1
            fi
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: Refuse a world-readable age key
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: sops_age.yml
        """
    )
    env = _ansible_env()
    env["SOPS_AGE_KEY_FILE"] = str(keyfile)
    proc = _run_localhost_role(play, tmp_path, ["-e", f"k3s_binary_path={binary}"], env=env)
    _assert_key_was_not_read(proc, sentinel)
    invocations = log.read_text(encoding="utf-8")
    assert "get secret sops-age -o name" in invocations
    assert "create" not in invocations
    assert "-o json" not in invocations


def test_sops_age_refuses_wrong_owner_from_stat(tmp_path: Path) -> None:
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
    if chowned:
        log = tmp_path / "k3s.log"
        binary = tmp_path / "k3s"
        _write_executable(
            binary,
            "#!/bin/bash\n"
            f"printf '%s\\n' \"$*\" >> {log}\n"
            'if [[ "$*" == *"get secret sops-age -o name"* ]]; then exit 1; fi\n'
            'echo "unexpected k3s: $*" >&2\nexit 1\n',
        )
        play = textwrap.dedent(
            """\
            ---
            - name: Refuse an age key owned by someone else
              hosts: localhost
              gather_facts: false
              become: false
              tasks:
                - ansible.builtin.import_role:
                    name: flux-operator
                    tasks_from: sops_age.yml
            """
        )
        env = _ansible_env()
        env["SOPS_AGE_KEY_FILE"] = str(keyfile)
        proc = _run_localhost_role(play, tmp_path, ["-e", f"k3s_binary_path={binary}"], env=env)
        _assert_key_was_not_read(proc, sentinel)
        assert "create" not in log.read_text(encoding="utf-8")
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
    proc = _run_localhost_role(play, tmp_path, [], env=_ansible_env())
    _assert_key_was_not_read(proc, sentinel)

    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            printf '%s\\n' "$*" >> {log}
            joined="$*"
            if [[ "$joined" == *"get fluxinstance"* ]]; then
              echo "fluxinstance.fluxcd.controlplane.io/flux"
              exit 0
            fi
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: FluxInstance already present
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: instance.yml
        """
    )
    env = _ansible_env()
    extra = ["-e", f"k3s_binary_path={binary}"]
    inventory = ["localhost,", str(_backup_inventory(tmp_path))]
    first = _run_localhost_role(play, tmp_path, extra, env=env, inventory=inventory)
    second = _run_localhost_role(play, tmp_path, extra, env=env, inventory=inventory)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    invocations = log.read_text(encoding="utf-8")
    assert invocations.count("get fluxinstance") == 2
    assert "backup-rebuild-hold" not in invocations
    assert "configmap" not in invocations.lower()


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


def test_fresh_cluster_with_restic_repos_sets_the_hold(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    stdin_path = tmp_path / "apply.stdin"
    binary = tmp_path / "k3s"
    restic = tmp_path / "backup" / "restic"
    (restic / "ns-demo" / "pvc-drill").mkdir(parents=True)
    (restic / "ns-demo" / "pvc-drill" / "config").write_text("repo\n", encoding="utf-8")
    (restic / "ns-decoy" / "notpvc").mkdir(parents=True)
    (restic / "ns-decoy" / "notpvc" / "config").write_text("nope\n", encoding="utf-8")
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            printf '%s\\n' "$*" >> {log}
            joined="$*"
            if [[ "$joined" == *"get fluxinstance"* ]]; then
              echo 'Error from server (NotFound): fluxinstances.fluxcd.controlplane.io "flux" not found' >&2
              exit 1
            fi
            if [[ "$joined" == *"kubectl apply -f -"* ]]; then
              cat > {stdin_path}
              echo "configmap/backup-rebuild-hold created"
              exit 0
            fi
            if [[ "$joined" == *"kubectl apply -f "* ]]; then
              echo "fluxinstance.fluxcd.controlplane.io/flux"
              exit 0
            fi
            echo "unexpected k3s invocation: $*" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: Fresh cluster
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: instance.yml
        """
    )
    env = _ansible_env()
    extra = [
        *_flux_operator_extra(binary),
        "-e",
        f"backup_path={tmp_path / 'backup'}",
        "-e",
        f"ansible_become_exe={_become_exe(tmp_path)}",
    ]
    proc = _run_localhost_role(
        play,
        tmp_path,
        extra,
        env=env,
        inventory=["localhost,", str(_backup_inventory(tmp_path))],
    )
    output = proc.stdout + proc.stderr
    # changed_when used to fail the play here, before the FluxInstance render.
    assert "'stdout' is undefined" not in output
    assert "Apply the rebuild hold" in output
    rendered = stdin_path.read_text(encoding="utf-8")
    assert "ns-demo/pvc-drill" in rendered
    assert "ns-decoy" not in rendered
    if proc.returncode != 0:
        assert "chown failed" in output
    else:
        invocations = log.read_text(encoding="utf-8").splitlines()
        hold_apply = next(i for i, line in enumerate(invocations) if "apply -f -" in line)
        flux_apply = next(
            i for i, line in enumerate(invocations) if "apply -f " in line and "apply -f -" not in line
        )
        assert hold_apply < flux_apply


def test_fluxinstance_lookup_error_does_not_set_the_hold(tmp_path: Path) -> None:
    require_cli("ansible-playbook")
    log = tmp_path / "k3s.log"
    binary = tmp_path / "k3s"
    _write_executable(
        binary,
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            printf '%s\\n' "$*" >> {log}
            echo "The connection to the server 127.0.0.1:6443 was refused" >&2
            exit 1
            """
        ),
    )
    play = textwrap.dedent(
        """\
        ---
        - name: API error
          hosts: localhost
          gather_facts: false
          become: false
          tasks:
            - ansible.builtin.import_role:
                name: flux-operator
                tasks_from: instance.yml
        """
    )
    env = _ansible_env()
    proc = _run_localhost_role(
        play,
        tmp_path,
        ["-e", f"k3s_binary_path={binary}"],
        env=env,
        inventory=["localhost,", str(_backup_inventory(tmp_path))],
    )
    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "Check whether FluxInstance exists" in output
    assert "Stat restic data on the backup host" not in output
    assert "backup-rebuild-hold" not in log.read_text(encoding="utf-8")
