"""jarvis init and jarvis env add: start an instance repo, or add a cluster to one.

  jarvis init [<dir>] --env <name> --domain <dns suffix> --repo-url <url> --node <host>[:<roles>] ...
  jarvis env add <name> --domain <dns suffix> --node <host>[:<roles>] ...

init makes <dir> (default: here; it must be empty) a git repo holding one env: its values, its
inventory, .sops.yaml, a README, and unless --no-ci the Renovate config and the CI caller. It
creates the working age key if there is none, makes the offline recovery key and prints its
private half once, mints the env's secrets (jarvis secrets), seals a read-only deploy key for an
ssh:// --repo-url, renders the env and installs a pre-commit hook. The env passes jarvis validate
as soon as init returns. env add does the same for one more env in an existing repo, with
the same age recipients as the repo's first env.

The env pins the core release this command runs from. Omitted values are asked for when run
from a terminal.

Roles: control_plane, workers, backup, edge, gpu. A lone --node <host> gets every role but gpu.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
from pathlib import Path

import yaml

from jarvis.env import scaffold
from jarvis.secrets import age as _age
from jarvis.secrets import recipients as _recipients

_KNOWN_FEATURES = ("app-dns", "app-hosting", "offsite-backup-copy", "apps-backup-server")


def parser(command: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"jarvis {command}", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    if command == "init":
        p.add_argument("dir", nargs="?", default=".", help="The new instance repo (default: here; must be empty)")
        p.add_argument("--env", dest="name", help="The first env's name (a DNS label)")
        p.add_argument("--repo-url", help="Where Flux reads this repo: ssh://git@<forge>/<owner>/<repo> or https://")
        p.add_argument("--branch", default="main", help="The branch Flux follows (default: main)")
        p.add_argument("--no-ci", action="store_true", help="Write no CI workflow or Renovate config")
        p.add_argument(
            "--with-agents",
            action="store_true",
            help="Add the coding-agent kit: AGENTS.md, CLAUDE.md and the deny lists in .claude/ and .cursor/",
        )
        p.add_argument(
            "--recovery-recipient",
            help="Use this age public key as the recovery key instead of making one (it must be offline)",
        )
    else:
        p.add_argument("name", help="The new env's name (a DNS label)")
    p.add_argument("--domain", help="The env's DNS suffix, e.g. home.lan")
    p.add_argument("--node", action="append", default=[], help="<host> or <host>:<role>,<role> (repeat per node)")
    p.add_argument("--ssh-user", help="The user Ansible logs in as on the nodes (default: you)")
    p.add_argument("--time-zone", default="Etc/UTC", help="The nodes' time zone (default: Etc/UTC)")
    p.add_argument("--feature", action="append", default=[], choices=_KNOWN_FEATURES, help="Turn a feature on (repeat)")
    p.add_argument("--apps-repo-url", default="", help="The hosted apps repository, if the env will host apps")
    p.add_argument(
        "--with-examples",
        action="store_true",
        help="Turn on app-hosting with the example apps, for a first-run tour (apps repository: jarvis-apps beside core, unless --apps-repo-url)",
    )
    p.add_argument("--age-key-file", default="", help=f"The working age key (default: {_age.DEFAULT_KEY_FILE})")
    return p


def _ask(value: str | None, question: str) -> str:
    if value:
        return value
    if not sys.stdin.isatty():
        raise scaffold.ScaffoldError(f"missing {question} (pass it as a flag, or run from a terminal)")
    answer = input(f"{question}: ").strip()
    if not answer:
        raise scaffold.ScaffoldError(f"missing {question}")
    return answer


def _spec(args: argparse.Namespace, *, name: str, repo_url: str, branch: str, core: tuple[str, str]) -> scaffold.EnvSpec:
    domain = _ask(args.domain, "the env's DNS suffix (--domain)")
    raw_nodes = args.node or [_ask(None, "the first node's host name (--node)")]
    nodes = [scaffold.Node.parse(text, only=len(raw_nodes) == 1) for text in raw_nodes]
    features = list(dict.fromkeys(args.feature + (["app-hosting"] if args.with_examples else [])))
    apps_repo_url = args.apps_repo_url or (scaffold.apps_repo_beside(core[0]) if args.with_examples else "")
    return scaffold.EnvSpec(
        name=name,
        domain=domain,
        repo_url=repo_url,
        core_url=core[0],
        core_tag=core[1],
        nodes=nodes,
        branch=branch,
        ssh_user=args.ssh_user or getpass.getuser(),
        time_zone=args.time_zone,
        features=features,
        apps_repo_url=apps_repo_url,
        age_key_file=args.age_key_file,
        examples=args.with_examples,
    )


def _working_key(spec: scaffold.EnvSpec) -> str:
    path = _age.key_path(spec.age_key_file or None)
    if path.exists():
        public = _age.public_key(path)
        print(f"PASS age key {path} (public key {public})")
        return public
    public = _age.create_key(path)
    print(f"PASS created the age key {path} (mode 0600, public key {public})")
    return public


def _recovery_key(given: str | None) -> str:
    if given:
        if not _age.PUBLIC_KEY.fullmatch(given):
            raise scaffold.ScaffoldError(f"--recovery-recipient {given!r} is not an age public key")
        return given
    secret, public = _age.new_key_in_memory()
    _recipients.print_recovery_key(secret, public)
    return public


def _secrets(instance: Path, spec: scaffold.EnvSpec) -> None:
    """Mint the env's own secrets (k3s tokens, CA, restic login), so the new env is whole."""
    from jarvis.secrets import mint

    os.environ["INSTANCE"] = str(instance)
    for path in mint.bootstrap(spec.name):
        print(f"PASS minted {path}")


def _deploy_key(instance: Path, spec: scaffold.EnvSpec) -> str | None:
    if not spec.repo_url.startswith("ssh://"):
        return None
    from jarvis.secrets import sops as _sops

    private, public = scaffold.deploy_key(spec.name)
    _sops.encrypt_secret_to_file(
        repo_root=instance,
        env=spec.name,
        name="flux-deploy-key",
        document=yaml.safe_load(scaffold.deploy_key_secret(private)),
        sops_bin=_sops.require_sops(),
    )
    print(f"PASS sealed envs/{spec.name}/secrets/flux-deploy-key.sops.yaml")
    return public


def _finish(instance: Path, spec: scaffold.EnvSpec, deploy_public: str | None, skipped: list[str], *, up: str) -> None:
    from jarvis.apps.checkout import AppsCheckoutError, fetch
    from jarvis.render import env as _render

    os.environ["INSTANCE"] = str(instance)
    # An env that lists apps renders from their files at their pinned tags.
    try:
        if not os.environ.get("APPS_REPO") and (checkout := fetch(instance)) is not None:
            os.environ["APPS_REPO"] = str(checkout)
    except AppsCheckoutError as exc:
        # Everything else is written; only the render waits for the apps repository.
        raise scaffold.ScaffoldError(f"{exc}; envs/{spec.name} is written, run jarvis render once it is reachable") from exc
    _render.render_env(instance / "envs" / spec.name, instance)
    print(f"PASS rendered envs/{spec.name}/flux/")
    for line in skipped:
        print(f"WARN skipped {line}")
    steps = []
    if deploy_public:
        steps.append(f"Add this read-only deploy key to the repository on your forge (write access off):\n       {deploy_public}")
    others = " (and each other node)" if len(spec.nodes) > 1 else ""
    steps.append(f"Prepare the node: jarvis node prep {spec.nodes[0].name}{others}")
    steps.append(f"Commit and push: git add -A && git commit -m '{spec.name}: first cluster' && git push")
    steps.append(up)
    print("\nNext:")
    for number, step in enumerate(steps, start=1):
        print(f"  {number}. {step}")


def _missing_tools(spec: scaffold.EnvSpec) -> list[str]:
    needed = ["git", "age-keygen", "sops"] + (["ssh-keygen"] if spec.repo_url.startswith("ssh://") else [])
    return [tool for tool in needed if shutil.which(tool) is None]


def _refuse_missing_tools(command: str, spec: scaffold.EnvSpec) -> bool:
    """Check the tools before the first write, so a missing one leaves nothing half made."""
    missing = _missing_tools(spec)
    if missing:
        print(f"FAIL {command}: not on PATH: {', '.join(missing)} (run jarvis doctor)", flush=True)
    return bool(missing)


def run_init(argv: list[str], core: tuple[str, str]) -> int:
    args = parser("init").parse_args(argv)
    instance = Path(args.dir).expanduser().resolve()
    if instance.exists() and any(instance.iterdir()):
        print(f"REFUSE init: {instance} is not empty", flush=True)
        return 3
    name = _ask(args.name, "the first env's name (--env)")
    repo_url = _ask(args.repo_url, "the URL Flux reads this repo from (--repo-url)")
    spec = _spec(args, name=name, repo_url=repo_url, branch=args.branch, core=core)
    spec.check()
    if _refuse_missing_tools("init", spec):
        return 4
    instance.mkdir(parents=True, exist_ok=True)
    scaffold.git_init(instance, spec.branch)
    working = _working_key(spec)
    recovery = _recovery_key(args.recovery_recipient)
    scaffold.write_env(instance, spec)
    scaffold.write_sops_rule(instance, spec.name, [working, recovery])
    skipped = scaffold.write_repo_files(instance, spec, ci=not args.no_ci, agents=args.with_agents)
    _secrets(instance, spec)
    deploy_public = _deploy_key(instance, spec)
    print(f"PASS installed the pre-commit hook {scaffold.install_hook(instance).relative_to(instance)}")
    _finish(instance, spec, deploy_public, skipped, up="jarvis up")
    return 0


def run_env_add(instance: Path, argv: list[str], core: tuple[str, str]) -> int:
    args = parser("env add").parse_args(argv)
    sops_config = instance / ".sops.yaml"
    existing = sorted(path.name for path in (instance / "envs").iterdir() if path.is_dir())
    keys = next((found for env in existing if (found := _age.recipients(sops_config, env))), [])
    if not keys:
        print("REFUSE env add: .sops.yaml has no env's age recipients to reuse; run jarvis init first", flush=True)
        return 3
    cluster_vars = (instance / "envs" / existing[0] / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")
    first = yaml.safe_load(cluster_vars) or {}
    branch = str(first.get("flux_git_ref", "refs/heads/main")).removeprefix("refs/heads/")
    spec = _spec(args, name=args.name, repo_url=str(first.get("flux_git_repo_url", "")), branch=branch, core=core)
    spec.check()
    if _refuse_missing_tools("env add", spec):
        return 4
    scaffold.write_env(instance, spec)
    scaffold.write_sops_rule(instance, spec.name, keys)
    print(f"PASS envs/{spec.name}, encrypted to the same {len(keys)} age recipients as the repo's other envs")
    _secrets(instance, spec)
    _finish(instance, spec, _deploy_key(instance, spec), [], up=f"jarvis up --env {spec.name}")
    return 0
