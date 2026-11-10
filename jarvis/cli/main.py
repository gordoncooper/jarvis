"""jarvis: run an instance repo's envs, at the core release each env pins.

  jarvis [--env <env>] [--core <path>] <command> [<args>]
  jarvis --version

Run it from inside an instance repo (the folder that holds envs/). --env is needed only when
the repo holds more than one env; it may come before or after the command. A command runs at
the core release its env pins (core_git_ref): this checkout when it is that tag, otherwise a
checkout of the tag cached under ~/.cache/jarvis/core/, set up from that release's own lockfiles.
--core <path> runs a local core checkout instead, whatever the env pins (core development).

Commands:
  init [<dir>]           Start an instance repo with its first env (jarvis init --help)
  env add <name>         Add another env (cluster) to this instance repo
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
  verify                 Standing checks of the running cluster; --smoke adds short-lived probes
  drill                  Scratch-volume and etcd restore drill
  restore (--app <ns> | --all) [--as-of <time>]
                         Restore backed-up volumes

Each command's own options: jarvis <command> --help, from inside the instance repo (they are
those of the release the env pins). jarvis passes --env itself when the repo holds one env.

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


def _help(name: str, command: Command) -> int:
    """A command's own help: its options, or its docstring when it takes none."""
    try:
        module = importlib.import_module(command.module)
    except (ImportError, OSError):
        # An installed CLI holds no core trees (schema/, core/): only the release an env pins can
        # load this command. Inside an instance repo, help is handed off to it first.
        print(
            f"jarvis {name}: run jarvis {name} --help from inside an instance repo; a command's options are "
            "those of the core release its env pins"
        )
        return 0
    if not command.takes_options:
        print(module.__doc__)
        return 0
    try:
        module.main(["--help"])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _version() -> int:
    """The release this CLI came from, and inside an instance repo the release each env pins:
    every command runs the env's, so a difference is fine, but it is worth seeing."""
    own = _core.own_release()
    print(f"jarvis {own[1]} ({own[0]})" if own else "jarvis: release unknown (neither a core checkout at a tag nor installed from git+<url>@<tag>)")
    try:
        instance = find_instance(Path.cwd())
    except UsageError:
        return 0
    for env_dir in sorted(path for path in (instance / "envs").iterdir() if path.is_dir()):
        try:
            print(f"{env_dir.name} pins {_core.pinned_tag(env_dir)}")
        except PinError as exc:
            print(f"{env_dir.name}: {exc}")
    return 0


def _take(args: list[str], name: str) -> tuple[str | None, list[str]]:
    """Take one --<name> <value> option out of args."""
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2 :]
        if arg.startswith(name + "="):
            return arg.partition("=")[2], args[:i] + args[i + 1 :]
    return None, args


def _start(command: str, args: list[str], core: str | None, env_name: str | None) -> int:
    """jarvis init and jarvis env add: no env to read a pin from yet, so the new env pins the core
    release this CLI came from (--core-url and --core-ref name another), and runs from it."""
    from jarvis.cli import init

    if command == "env" and (not args or args[0] != "add"):
        raise UsageError("usage: jarvis env add <name> --domain <dns suffix> --node <host>[:<roles>] ...")
    if "-h" in args or "--help" in args:
        init.parser("init" if command == "init" else "env add").print_help()
        return 0
    url, args = _take(args, "--core-url")
    tag, args = _take(args, "--core-ref")
    if env_name is not None:
        args = ["--env", env_name, *args] if command == "init" else args
    if core is not None:
        os.environ[_core.UNPINNED_VAR] = "1"
        checkout = Path(core).expanduser().resolve()
        if checkout != PRODUCT_ROOT:
            forward = [*(["--core-url", url] if url else []), *(["--core-ref", tag] if tag else [])]
            _core.hand_off(checkout, [command, *args, *forward])
    if not (url and tag):
        own = _core.own_release()
        if own is None:
            raise UsageError(f"jarvis {command} cannot tell which core release it is; pass --core-url and --core-ref")
        url, tag = url or own[0], (tag or own[1]).removeprefix("refs/tags/")
    tag = tag.removeprefix("refs/tags/")
    if not os.environ.get(_core.UNPINNED_VAR) and not _core.at_tag(PRODUCT_ROOT, tag):
        if os.environ.get(_core.DELEGATED_VAR):
            raise _core.CoreError(f"{PRODUCT_ROOT} is not core {tag}")
        _core.hand_off(_core.checkout_tag(url, tag), [command, *args, "--core-url", url, "--core-ref", tag])
    activate_toolchain(PRODUCT_ROOT)
    try:
        if command == "init":
            return init.run_init(args, (url, tag))
        return init.run_env_add(find_instance(Path.cwd()), args[1:], (url, tag))
    except init.scaffold.ScaffoldError as exc:
        print(f"FAIL {command}: {exc}", file=sys.stderr)
        return USAGE
    except init._age.AgeError as exc:
        print(f"FAIL {command}: {exc}", file=sys.stderr)
        return ENVIRONMENT


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
    if rest[:1] == ["--version"]:
        return _version()
    if not rest or rest[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if rest else USAGE
    command, args = rest[0], rest[1:]
    if command in ("init", "env"):
        return _start(command, args, core, env_name)
    if command not in COMMANDS:
        raise UsageError(f"unknown command {command!r}; see jarvis --help")
    wants_help = "-h" in args or "--help" in args
    if wants_help:
        try:
            instance = find_instance(Path.cwd())
        except UsageError:
            # Outside an instance repo: this CLI's own help, where it can load the command.
            return _help(command, COMMANDS[command])
        if env_name is None:
            # Any env's release shows the options; with several, the first stands in for --env.
            envs = sorted(path.name for path in (instance / "envs").iterdir() if path.is_dir())
            env_name = envs[0] if envs else None
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
        _core.hand_off(_core.checkout(env_dir), ["--env", env, *rest] if wants_help else argv)
    if os.environ.get(_core.UNPINNED_VAR) and instance != PRODUCT_ROOT:
        problem = pin_problem(PRODUCT_ROOT, env_dir)
        if problem:
            print(f"WARN jarvis: unpinned run with --core: {problem}", file=sys.stderr)
    if wants_help:
        return _help(command, COMMANDS[command])
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
