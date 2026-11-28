"""Helpers for Ansible role proof tests and template renders."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

import yaml

from support import REPO_ROOT, sample_root

ANSIBLE_DIR = REPO_ROOT / "infra" / "ansible"
K3S_ROLE_DIR = ANSIBLE_DIR / "roles" / "k3s"
FLUX_OPERATOR_ROLE_DIR = ANSIBLE_DIR / "roles" / "flux-operator"
FLUX_OPERATOR_ROLE_TASKS = FLUX_OPERATOR_ROLE_DIR / "tasks"
K3S_ROLE_TEMPLATES = K3S_ROLE_DIR / "templates"


# A play's hosts and connection for a run on this machine.
LOCALHOST = ("-i", "localhost,", "-e", "ansible_connection=local")


def _ansible_env(env: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """This process's environment with become off and the repo's ansible.cfg, then env (None unsets a key)."""
    full = {**os.environ, "ANSIBLE_BECOME": "false", "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg"), **(env or {})}
    return {key: value for key, value in full.items() if value is not None}


def playbook(
    *args: object, env: Mapping[str, str | None] | None = None, json: bool = False
) -> subprocess.CompletedProcess[str]:
    """ansible-playbook from the Ansible directory, with become off. .output is stdout and stderr together; env
    goes over this process's environment (None unsets a key). json=True parses the ansible.posix.json callback's
    document into .json (see changed, plays, failed_task, ran), and .output is then JSON: escaped values, no banners."""
    env = {**({"ANSIBLE_STDOUT_CALLBACK": "ansible.posix.json"} if json else {}), **(env or {})}
    command = ["ansible-playbook", *map(str, args)]
    proc = subprocess.run(command, cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False, env=_ansible_env(env))
    proc.output = proc.stdout + proc.stderr  # type: ignore[attr-defined]
    if json:
        proc.json = _document(proc)  # type: ignore[attr-defined]
    return proc


def _document(proc: subprocess.CompletedProcess[str]) -> dict:
    """The json callback's document. A run that stops before its first play prints none."""
    assert proc.stdout, proc.output
    return json.loads(proc.stdout)


def changed(run: subprocess.CompletedProcess[str]) -> dict[str, int]:
    """Each host's changed count in a json=True run, for every host the run reached."""
    return {host: counts["changed"] for host, counts in run.json["stats"].items()}


def plays(run: subprocess.CompletedProcess[str]) -> list[str]:
    """The names of the plays a json=True run started, in order."""
    return [play["play"]["name"] for play in run.json["plays"]]


def failed_task(run: subprocess.CompletedProcess[str]) -> tuple[str, str] | None:
    """The first task that failed in a json=True run, named as in its task file, and its msg; None if none did."""
    return next(((name, result.get("msg", "")) for name, _, result in _results(run) if result.get("failed")), None)


def ran(run: subprocess.CompletedProcess[str], task: str) -> dict[str, str]:
    """How task, named as in its task file, ended on each host that reached it in a json=True run:
    failed, unreachable, skipped, changed or ok. {} when no host reached it."""
    return {host: next((end for end in ("failed", "unreachable", "skipped", "changed") if result.get(end)), "ok")
            for name, host, result in _results(run) if name == task}


def _results(run: subprocess.CompletedProcess[str]) -> Iterator[tuple[str, str, dict]]:
    """(task name, host, result) for each host's result in a json=True run, in order; handlers count as tasks."""
    for play in run.json["plays"]:
        for task in play["tasks"]:
            for host, result in task["hosts"].items():
                # Ansible names a role's task "<role> : <name>".
                yield task["task"]["name"].split(" : ", 1)[-1], host, result


def local_play(
    tmp_path: Path, tasks: str, *, bin_dir: Path | None = None, hosts: str = "localhost", vars: dict | None = None
) -> Path:
    """A play file with no facts or become: tasks is the YAML list under tasks:, indented four spaces.
    bin_dir puts fake host tools first on PATH; vars are the play's vars."""
    environment = f'  environment:\n    PATH: "{bin_dir}:/usr/bin:/bin"\n' if bin_dir else ""
    play_vars = f"  vars: {json.dumps(vars)}\n" if vars else ""
    play = tmp_path / "play.yml"
    head = f"---\n- hosts: {hosts}\n  gather_facts: false\n  become: false\n{environment}{play_vars}"
    play.write_text(f"{head}  tasks:\n{tasks}", encoding="utf-8")
    return play


def role_tasks(role: str, tasks_from: str | None = None) -> str:
    """local_play's tasks: import role, or only its tasks_from file."""
    only = f"        tasks_from: {tasks_from}\n" if tasks_from else ""
    return f"    - ansible.builtin.import_role:\n        name: {role}\n{only}"


def fake_tool(bin_dir: Path, name: str, body: str) -> Path:
    """An executable bin_dir/name holding body, a script with its #! line, standing in for a host tool."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / name
    tool.write_text(body, encoding="utf-8")
    tool.chmod(0o755)
    return tool


# fake_systemctl's body: one unit, whatever name a call gives, "<active> <enabled>" in the state file beside it.
_SYSTEMCTL = r"""
import sys
from pathlib import Path

args, state = sys.argv[1:], Path(__file__).with_name("state")
with Path(__file__).with_name("systemctl.log").open("a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")
command = args[0] if args else ""
if command == FAIL:
    sys.exit(f"{command} failed")
active, enabled = state.read_text(encoding="utf-8").split()
sets = {"start": ("active", enabled), "restart": ("active", enabled), "stop": ("inactive", enabled),
        "enable": (active, "enabled"), "disable": (active, "disabled")}
if command == "show":
    print(f"LoadState=loaded\nActiveState={active}")
elif command == "is-active":
    print(active)
    sys.exit((0 if active == "active" else 3) if IS_ACTIVE_RC is None else IS_ACTIVE_RC)
elif command == "is-enabled":
    print(enabled)
    sys.exit(0 if enabled == "enabled" else 1)
elif command in sets:
    state.write_text(" ".join(sets[command]), encoding="utf-8")
elif command != "daemon-reload":
    sys.exit(f"unexpected systemctl {' '.join(args)}")
"""


def fake_systemctl(bin_dir: Path, *, running: bool = True, fail: str = "", is_active_rc: int | None = None) -> Path:
    """A systemctl in bin_dir for one unit, active and enabled when running: show, is-active and is-enabled
    read its state, and start, stop, enable and disable change it. fail names a command that exits 1;
    is_active_rc replaces is-active's exit (4: no such unit). Returns its log, one line per call."""
    fake_tool(bin_dir, "systemctl", f"#!{sys.executable}\nFAIL, IS_ACTIVE_RC = {fail!r}, {is_active_rc!r}{_SYSTEMCTL}")
    (bin_dir / "state").write_text("active enabled" if running else "inactive disabled", encoding="utf-8")
    log = bin_dir / "systemctl.log"
    log.write_text("", encoding="utf-8")
    return log


def env_name() -> str:
    """The sample instance's env (ci/tests/sample.py)."""
    return "env-00"


def site_vars_file(dest: Path, repo_root: Path | None = None, *, env: str | None = None, **override: object) -> Path:
    """dest, holding what jarvis site passes every play (jarvis.node.site.site_vars) for the
    sample env, or env at repo_root, with override's keys set; None drops a key."""
    from jarvis.node.site import site_vars

    root, name = repo_root or sample_root(), env or env_name()
    found = {**site_vars(root, name, load_cluster_vars(root, env=name)), **override}
    dest.write_text(json.dumps({key: value for key, value in found.items() if value is not None}), encoding="utf-8")
    return dest


def inventory_path() -> Path:
    return sample_root() / "envs" / env_name() / "inventory" / "hosts.yml"


def roles_inventory_path() -> Path:
    return REPO_ROOT / "infra" / "inventory" / "roles"


def role_groups(*inventory: Path) -> dict[str, set[str]]:
    """Each group's hosts, all and ungrouped left out, from ansible-inventory --list over the sources in order."""
    command = ["ansible-inventory", "--list", *[arg for source in inventory for arg in ("-i", str(source))]]
    proc = subprocess.run(command, cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False, env=_ansible_env())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    listed = json.loads(proc.stdout)
    return {name: set(listed[name].get("hosts", [])) for name in listed.keys() - {"_meta", "all", "ungrouped"}}


def expected_role_groups(hosts: Path) -> dict[str, set[str]]:
    """The groups a hosts file's node_roles make: each role, holding the hosts that list it."""
    groups: dict[str, set[str]] = {}
    for name, host in yaml.safe_load(hosts.read_text(encoding="utf-8"))["all"]["hosts"].items():
        for role in (host or {}).get("node_roles") or []:
            groups.setdefault(role, set()).add(name)
    return groups


def load_role_defaults(role_dir: Path) -> dict[str, object]:
    data = yaml.safe_load((role_dir / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def load_cluster_vars(repo_root: Path | None = None, *, env: str | None = None) -> dict[str, object]:
    name = env_name() if env is None else env
    path = (repo_root or sample_root()) / "envs" / name / "values" / "cluster-vars.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # With the schema's defaults, as Ansible and the renderer see the env.
    from jarvis.schema.env import resolve_cluster_vars

    return resolve_cluster_vars(data)


def ansible_render_template(
    template_name: str,
    variables: dict[str, object],
    *,
    templates_dir: Path,
    inventory: Path | None = None,
) -> str:
    """Render a role template via ansible.builtin.template (same engine as the role), with what
    jarvis site passes for the sample env beneath variables."""
    src = templates_dir / template_name
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        dest = Path(handle.name)
    extra = ["-e", f"@{site_vars_file(dest.with_suffix('.json'))}", *(["-e", json.dumps(variables)] if variables else [])]
    cmd = ["ansible", "localhost", *(["-i", str(inventory)] if inventory is not None else []),
           "-c", "local", "-m", "ansible.builtin.template", "-a", f"src={src} dest={dest}", *extra]
    run = subprocess.run(cmd, cwd=ANSIBLE_DIR, capture_output=True, text=True, check=False, env=_ansible_env())
    try:
        if run.returncode != 0:
            raise AssertionError(run.stdout + run.stderr)
        return dest.read_text(encoding="utf-8")
    finally:
        dest.unlink(missing_ok=True)
        dest.with_suffix(".json").unlink(missing_ok=True)

