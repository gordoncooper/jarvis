"""jarvis: run an instance repo's envs, at the core release each env pins.

  jarvis [--env <env>] [--core <path>] <command> [<args>]

Run it from inside an instance repo (the folder that holds envs/). --env is needed only when
the repo holds more than one env; it may come before or after the command. A command runs at
the core release its env pins (core_git_ref): this checkout when it is that tag, otherwise a
checkout of the tag cached under ~/.cache/jarvis/core/, set up from that release's own lockfiles.
--core <path> runs a local core checkout instead, whatever the env pins (core development).

Commands:
  doctor                 Check this controller: CLIs, the age key, the remote, host names
  validate               Every env check: schema, wiring, committed files, secret scan
  render [--check]       Write the env's flux/ from its values; --check lists stale files
  secrets [--app <ns>]   Mint the env's missing secrets (--rotate-restic: a new restic password)
  encrypt --name <n>     Encrypt a Secret manifest (a file or stdin) into the env's secrets/
  age-key-check --key-file <file>
                         Prove an age key copy decrypts one of the env's secrets
  node prep <host>       Prepare a new node: key login, passwordless sudo, its hostname
  site [--tags] [--limit] [--check]
                         Apply the env's hosts with core's Ansible playbook
  up                     doctor, validate, render --check, secrets, site, wait for Flux, verify
  verify                 Read-only standing checks of the running cluster
  drill                  Scratch-volume and etcd restore drill
  restore (--app <ns> | --all) [--as-of <time>]
                         Restore backed-up volumes

Each command's own options: jarvis <command> --help.

Exit codes: 0 ok, 1 a check failed, 2 usage error, 3 refused for safety (nothing changed),
4 the environment is wrong (a missing tool, key or access).
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Only what deciding which core runs needs: an installed CLI that is not the env's pin holds no
# core trees (schema, manifests), so nothing that reads them may load before it hands off.
from jarvis.cli import core as _core
from jarvis.env.pin import PinError, pin_problem
from jarvis.paths import PRODUCT_ROOT

USAGE = 2
REFUSED = 3
ENVIRONMENT = 4
INSTANCE_VAR = "INSTANCE"


class UsageError(Exception):
    pass


def split_globals(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Take --env and --core out of argv, wherever they are. Returns (env, core, the rest)."""
    found: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        name, sep, value = arg.partition("=")
        if name in ("--env", "--core"):
            if not sep:
                if i + 1 >= len(argv):
                    raise UsageError(f"{name} needs a value")
                value = argv[i + 1]
                i += 1
            if name in found:
                raise UsageError(f"{name} given twice")
            found[name] = value
        else:
            rest.append(arg)
        i += 1
    return found.get("--env"), found.get("--core"), rest


def find_instance(start: Path) -> Path:
    """The nearest folder at or above start that holds envs/."""
    for folder in (start, *start.parents):
        if (folder / "envs").is_dir():
            return folder
    raise UsageError("run jarvis from inside an instance repo (a folder that holds envs/)")


def choose_env(instance: Path, name: str | None) -> str:
    envs = sorted(path.name for path in (instance / "envs").iterdir() if path.is_dir())
    if name is not None:
        if name not in envs:
            raise UsageError(f"no env {name!r} in {instance / 'envs'} (envs: {', '.join(envs) or 'none'})")
        return name
    if len(envs) == 1:
        return envs[0]
    if not envs:
        raise UsageError(f"{instance / 'envs'} holds no env")
    raise UsageError(f"this repo holds several envs; pass --env ({', '.join(envs)})")


def activate_toolchain(core: Path) -> None:
    """Put the core checkout's pinned CLIs (mise) and its venv first on PATH."""
    paths: list[str] = []
    if shutil.which("mise"):
        proc = subprocess.run(["mise", "bin-paths"], cwd=core, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            paths = [line for line in proc.stdout.splitlines() if line.strip()]
    paths.append(str(Path(sys.executable).parent))
    os.environ["PATH"] = os.pathsep.join([*paths, os.environ.get("PATH", "")])


@dataclass(frozen=True)
class Command:
    """A command: the module that holds it, and how it runs for one env of an instance."""

    module: str
    run: Callable[[Path, str, list[str]], int]
    # Options of its own (jarvis <command> --help shows them); otherwise its docstring is the help.
    takes_options: bool = True


def _module_main(module: str) -> Command:
    """A command that is the module's own main(), given the env."""
    return Command(module, lambda instance, env, args: importlib.import_module(module).main(["--env", env, *args]))


def _plain(module: str, run: Callable[[Path, str], int]) -> Command:
    """A command that takes no options."""

    def command(instance: Path, env: str, args: list[str]) -> int:
        if args:
            raise UsageError(f"this command takes no arguments: {' '.join(args)}")
        return run(instance, env)

    return Command(module, command, takes_options=False)


def _node(instance: Path, env: str, args: list[str]) -> int:
    if len(args) < 2 or args[0] != "prep" or args[1].startswith("-"):
        raise UsageError("usage: jarvis node prep <host> [--login-user <user>]")
    from jarvis.node import prep

    return prep.main(["--env", env, "--host", args[1], *args[2:]])


def _doctor(instance: Path, env: str) -> int:
    from jarvis.cli import doctor

    return doctor.run(instance, instance / "envs" / env)


def _validate(instance: Path, env: str) -> int:
    from jarvis.env import validate
    from jarvis.schema.env import ENVS_VAR

    # The env's own checks only: each env is checked by the core release it pins.
    os.environ[ENVS_VAR] = env
    return validate.run(instance)


def _up(instance: Path, env: str) -> int:
    from jarvis.cli import up

    return up.run(instance, env)


COMMANDS: dict[str, Command] = {
    "doctor": _plain("jarvis.cli.doctor", _doctor),
    "validate": _plain("jarvis.env.validate", _validate),
    "render": _module_main("jarvis.render.env"),
    "secrets": _module_main("jarvis.secrets.mint"),
    "encrypt": _module_main("jarvis.secrets.encrypt"),
    "age-key-check": _module_main("jarvis.secrets.age_key_check"),
    "node": Command("jarvis.node.prep", _node),
    "site": _module_main("jarvis.node.site"),
    "up": _plain("jarvis.cli.up", _up),
    "verify": _module_main("jarvis.verify.run"),
    "drill": _module_main("jarvis.backups.drill"),
    "restore": _module_main("jarvis.backups.restore"),
}
# Commands that read hosted apps' files, so they need the apps checkout.
_READS_APPS = {"validate", "render", "secrets", "up", "verify", "drill", "restore"}


def _apps_checkout(instance: Path) -> None:
    """Point APPS_REPO at a fresh checkout of the instance's apps repository, unless it is set."""
    if os.environ.get("APPS_REPO") or instance == PRODUCT_ROOT:
        return
    from jarvis.apps.checkout import fetch

    checkout = fetch(instance)
    if checkout is not None:
        os.environ["APPS_REPO"] = str(checkout)
        head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True)
        print(f"jarvis: apps repository at {head.stdout.strip()}", file=sys.stderr)


def _help(command: Command) -> int:
    """A command's own help: its options, or its docstring when it takes none."""
    module = importlib.import_module(command.module)
    if not command.takes_options:
        print(module.__doc__)
        return 0
    try:
        module.main(["--help"])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(argv)
    except UsageError as exc:
        print(f"jarvis: {exc}", file=sys.stderr)
        return USAGE
    except PinError as exc:
        print(f"REFUSE jarvis: {exc}", file=sys.stderr)
        return REFUSED
    except _core.CoreError as exc:
        print(f"FAIL jarvis: {exc}", file=sys.stderr)
        return ENVIRONMENT


def _main(argv: list[str]) -> int:
    env_name, core, rest = split_globals(argv)
    if not rest or rest[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if rest else USAGE
    command, args = rest[0], rest[1:]
    if command not in COMMANDS:
        raise UsageError(f"unknown command {command!r}; see jarvis --help")
    if "-h" in args or "--help" in args:
        return _help(COMMANDS[command])
    if core is not None:
        checkout = Path(core).expanduser().resolve()
        os.environ[_core.UNPINNED_VAR] = "1"
        if checkout != PRODUCT_ROOT:
            without_core = [*(["--env", env_name] if env_name else []), *rest]
            _core.hand_off(checkout, without_core)
    instance = find_instance(Path.cwd())
    env = choose_env(instance, env_name)
    env_dir = instance / "envs" / env
    if not _core.runs_here(instance, env_dir):
        if os.environ.get(_core.DELEGATED_VAR):
            raise _core.CoreError(f"{PRODUCT_ROOT} is not {env}'s pinned core: {pin_problem(PRODUCT_ROOT, env_dir)}")
        _core.hand_off(_core.checkout(env_dir), argv)
    if os.environ.get(_core.UNPINNED_VAR) and instance != PRODUCT_ROOT:
        problem = pin_problem(PRODUCT_ROOT, env_dir)
        if problem:
            print(f"WARN jarvis: unpinned run with --core: {problem}", file=sys.stderr)
    activate_toolchain(PRODUCT_ROOT)
    if instance != PRODUCT_ROOT:
        os.environ[INSTANCE_VAR] = str(instance)
    if command in _READS_APPS:
        from jarvis.apps.checkout import AppsCheckoutError

        try:
            _apps_checkout(instance)
        except AppsCheckoutError as exc:
            print(f"FAIL jarvis: {exc}", file=sys.stderr)
            return ENVIRONMENT
    return COMMANDS[command].run(instance, env, args)


if __name__ == "__main__":
    sys.exit(main())
