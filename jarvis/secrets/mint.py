"""Create-if-missing SOPS bootstrap secrets for an env. Never overwrites.

Also writes envs/<env>/ca.crt, the public CA certificate, and never the key.
A new CA permits only that env's dns_suffix. A backups-restic file git already
records is not recreated unless --rotate-restic is set. k3s tokens are minted
only for a new env; an env with other secrets gets its cluster's token sealed.

--app app-<name> mints only that hosted app's own backup credential, sealed under
envs/<env>/secrets/app-<name>/, and writes the Component the env lists to register
it. A credential git already records is never minted again.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import bcrypt
import yaml

from jarvis.backups import app_credentials as _app_backups
from jarvis.env.runtime import instance_root
from jarvis.paths import PRODUCT_ROOT
from jarvis.secrets import age as _age
from jarvis.secrets import sops as _sops

REPO_ROOT = PRODUCT_ROOT

# 10 years.
CA_DAYS = 3650
CA_SECRET_FILENAME = "cert-manager-ca.sops.yaml"
# Not a cluster Secret. The k3s role reads it on the controller.
K3S_TOKEN_FILENAME = "k3s-token.sops.yaml"
# No x509_extensions. Host openssl.cnf's v3_ca sets basicConstraints without
# keyUsage. This file makes the -addext flags the only extensions.
_CA_REQ_CONFIG = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = unused
"""
_CERT_MARK = "-----BEGIN CERTIFICATE-----"
_KEY_MARK = "PRIVATE KEY"

# Hostname labels, and at least two, so a CA name constraint cannot permit a
# whole single label. The value is placed in a critical permitted DNS subtree.
_DNS_SUFFIX = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)

# One htpasswd user. The password stays out of the repository URL.
RESTIC_REST_USERNAME = "restic"
# Server prefix. A volume repository is this plus "ns-<namespace>/pvc-<pvc>".
# Those prefixes keep a segment off rest-server's reserved names
# (data, index, keys, locks, snapshots, config).
RESTIC_REPOSITORY = "rest:http://restic.platform-backups.svc.cluster.local:8000/"


@dataclass(frozen=True)
class SecretSpec:
    filename: str
    build: Callable[[], dict]
    # Sealed under envs/<env>/secrets/<folder>/ when set.
    folder: str | None = None


def _repo_root() -> Path:
    return instance_root(REPO_ROOT)


def _secrets_dir(env: str) -> Path:
    return _repo_root() / "envs" / env / "secrets"


def _random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _run_openssl(args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["openssl", *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
        raise subprocess.CalledProcessError(result.returncode, ["openssl", *args])
    return result.stdout


def _cluster_text(env: str) -> str:
    path = _repo_root() / "envs" / env / "values" / "cluster-vars.yaml"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit("cluster-vars.yaml is required") from exc


def _yaml_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(\S+)\s*$", text, re.M)
    return match.group(1) if match else None


def _dns_suffix(env: str) -> str:
    suffix = _yaml_scalar(_cluster_text(env), "dns_suffix") or ""
    if not _DNS_SUFFIX.fullmatch(suffix):
        raise SystemExit("refusing to mint a CA without a DNS suffix")
    return suffix


def _expand_path(raw: str) -> str:
    if raw == "~" or raw.startswith("~/"):
        return str(Path.home() / raw.removeprefix("~/"))
    return raw


def _sops_child_env(env: str) -> dict[str, str]:
    """Prefer SOPS_AGE_KEY_FILE. Otherwise use the env's age_key_file."""
    child = os.environ.copy()
    if child.get("SOPS_AGE_KEY_FILE"):
        return child
    configured = _yaml_scalar(_cluster_text(env), "age_key_file") or _age.DEFAULT_KEY_FILE
    child["SOPS_AGE_KEY_FILE"] = _expand_path(configured)
    return child


def _generate_ca_material(dns_suffix: str) -> tuple[str, str]:
    """Return (certificate PEM, key PEM). The key stays in memory."""
    if not _DNS_SUFFIX.fullmatch(dns_suffix):
        raise SystemExit("refusing to mint a CA without a DNS suffix")
    key_pem = _run_openssl(
        [
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
        ]
    ).decode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="jarvis-ca-",
        suffix=".cnf",
    ) as cfg:
        cfg.write(_CA_REQ_CONFIG)
        cfg.flush()
        cert_pem = _run_openssl(
            [
                "req",
                "-x509",
                "-new",
                "-nodes",
                "-key",
                "/dev/stdin",
                "-sha256",
                "-days",
                str(CA_DAYS),
                "-subj",
                "/CN=jarvis-internal-ca/O=jarvis",
                "-config",
                cfg.name,
                "-addext",
                "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
                "-addext",
                "nameConstraints=critical,"
                f"permitted;DNS:{dns_suffix},"
                "excluded;IP:0.0.0.0/0.0.0.0,"
                "excluded;IP:0:0:0:0:0:0:0:0/0:0:0:0:0:0:0:0,"
                "permitted;email:none@nameconstraint.invalid,"
                f"permitted;URI:{dns_suffix},"
                f"permitted;URI:.{dns_suffix}",
            ],
            input_bytes=key_pem.encode("utf-8"),
        ).decode("utf-8")
    return cert_pem, key_pem


def _ca_root_path(env: str) -> Path:
    return _repo_root() / "envs" / env / "ca.crt"


def _public_certificate(pem: str) -> str:
    if _KEY_MARK in pem or _CERT_MARK not in pem:
        raise SystemExit("refusing to write a CA root that is not a certificate")
    if not pem.endswith("\n"):
        pem += "\n"
    return pem


def _write_public_root(path: Path, cert_pem: str) -> None:
    cert_pem = _public_certificate(cert_pem)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=".ca-",
            suffix=".crt",
        ) as tmp:
            tmp.write(cert_pem)
            tmp_path = Path(tmp.name)
        tmp_path.chmod(0o644)
        tmp_path.replace(path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _extract_tls_crt(secret_path: Path, sops_bin: str, env: str) -> str:
    """Decrypt only stringData.tls.crt. The private key stays encrypted."""
    result = subprocess.run(
        [
            sops_bin,
            "decrypt",
            "--extract",
            '["stringData"]["tls.crt"]',
            str(secret_path),
        ],
        cwd=_repo_root(),
        env=_sops_child_env(env),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return result.stdout


# Plain metadata. .sops.yaml encrypts only data and stringData.
_PRUNE = "kustomize.toolkit.fluxcd.io/prune"


def _guarded_metadata(metadata: dict[str, str]) -> dict[str, object]:
    return {**metadata, "annotations": {_PRUNE: "disabled"}}


def _secret_doc(metadata: dict[str, str], string_data: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _guarded_metadata(metadata),
        "type": "Opaque",
        "stringData": string_data,
    }


def _tls_secret_doc(metadata: dict[str, str], cert_pem: str, key_pem: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _guarded_metadata(metadata),
        "type": "kubernetes.io/tls",
        "stringData": {"tls.crt": cert_pem, "tls.key": key_pem},
    }


def _htpasswd_line(username: str, password: str) -> str:
    if not username or any(char in username for char in ":\r\n") or any(
        char in password for char in "\r\n\x00"
    ):
        raise SystemExit("refusing to build an htpasswd line")
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    return f"{username}:{hashed}\n"


def _htpasswd_accepts(line: str, username: str, password: str) -> bool:
    user, sep, hashed = line.strip().partition(":")
    if not sep or user != username or not hashed.startswith("$2"):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


def _restic_string_data() -> dict[str, str]:
    rest_password = _random_token(32)
    return {
        "RESTIC_PASSWORD": _random_token(32),
        "RESTIC_REPOSITORY": RESTIC_REPOSITORY,
        "RESTIC_REST_USERNAME": RESTIC_REST_USERNAME,
        "RESTIC_REST_PASSWORD": rest_password,
        "htpasswd": _htpasswd_line(RESTIC_REST_USERNAME, rest_password),
    }


def _app_restic_string_data(namespace: str) -> dict[str, str]:
    """An app's own password and apps-server login. The user is ns-<namespace>, its folder."""
    username = _app_backups.rest_username(namespace)
    rest_password = _random_token(32)
    return {
        "RESTIC_PASSWORD": _random_token(32),
        "RESTIC_REPOSITORY": _app_backups.APPS_REPOSITORY,
        "RESTIC_REST_USERNAME": username,
        "RESTIC_REST_PASSWORD": rest_password,
        "htpasswd": _htpasswd_line(username, rest_password),
    }


def _app_specs(namespace: str) -> list[SecretSpec]:
    """One hosted app's sealed files. Add a spec here to give every app another one."""
    name = _app_backups.secret_name(namespace)
    return [
        SecretSpec(
            f"{name}.sops.yaml",
            lambda: _secret_doc(
                {"name": name, "namespace": _app_backups.SECRET_NAMESPACE},
                _app_restic_string_data(namespace),
            ),
            folder=namespace,
        ),
    ]


def _specs(env: str) -> list[SecretSpec]:
    # The k3s tokens come first, so a run that fails later leaves them in place.
    return [
        SecretSpec(
            K3S_TOKEN_FILENAME,
            lambda: _secret_doc(
                {"name": "k3s-token"},
                {"server_token": _random_token(32), "agent_token": _random_token(32)},
            ),
        ),
        SecretSpec(
            CA_SECRET_FILENAME,
            lambda: _tls_secret_doc(
                {"name": "cert-manager-ca", "namespace": "platform-cert-manager"},
                *_generate_ca_material(_dns_suffix(env)),
            ),
        ),
        SecretSpec(
            "backups-restic.sops.yaml",
            lambda: _secret_doc(
                {"name": "backups-restic", "namespace": "platform-backups"},
                _restic_string_data(),
            ),
        ),
    ]


def _secret_name(filename: str) -> str:
    suffix = ".sops.yaml"
    if not filename.endswith(suffix):
        raise SystemExit("bootstrap secret filename must end with .sops.yaml")
    return filename[: -len(suffix)]


def _git_env() -> dict[str, str]:
    """Environment for a git read. Git-location variables are not passed on; messages are
    in C so the stderr checks below do not depend on the operator's locale."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["LC_ALL"] = "C"
    return env


def _git_unavailable(result: subprocess.CompletedProcess[str], rel: str) -> None:
    detail = (result.stderr or "").strip().splitlines()
    summary = detail[0] if detail else f"exit {result.returncode}"
    raise SystemExit(
        "refusing to mint secrets because git could not say whether "
        f"{rel} is already recorded: {summary}"
    )


def _git_run(repo: Path, args: list[str], rel: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                f"--git-dir={repo / '.git'}",
                f"--work-tree={repo}",
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=_git_env(),
        )
    except OSError as exc:
        raise SystemExit(
            "refusing to mint secrets because git could not say whether "
            f"{rel} is already recorded: {exc}"
        ) from exc


def _git_records(rel: str) -> bool:
    """True when git already has rel in the index, in HEAD, or in an earlier commit.

    A directory with no .git is not a repository, so the path is not recorded.
    When .git is present, a git error refuses. It does not count as "not recorded".
    """
    repo = _repo_root()
    # lexists: a .git symlink to nowhere is a broken repository (git refuses below), not "no repository".
    if not os.path.lexists(repo / ".git"):
        return False
    inside = _git_run(repo, ["rev-parse", "--is-inside-work-tree"], rel)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        if inside.returncode == 0:
            raise SystemExit(
                "refusing to mint secrets because git could not say whether "
                f"{rel} is already recorded: not a work tree"
            )
        _git_unavailable(inside, rel)
    listed = _git_run(repo, ["ls-files", "--error-unmatch", "--", rel], rel)
    if listed.returncode == 0:
        return True
    if listed.returncode != 1 or "fatal:" in listed.stderr:
        _git_unavailable(listed, rel)
    if _git_run(repo, ["rev-parse", "--verify", "--quiet", "HEAD"], rel).returncode != 0:
        return False  # no commit yet: the index check above was the only place it could be
    present = _git_run(repo, ["cat-file", "-e", f"HEAD:{rel}"], rel)
    if present.returncode == 0:
        return True
    stderr = present.stderr or ""
    path_missing = "does not exist in 'HEAD'" in stderr or "not in 'HEAD'" in stderr
    if present.returncode != 128 or not path_missing:
        _git_unavailable(present, rel)
    history = _git_run(repo, ["log", "-1", "--format=%H", "--", rel], rel)
    if history.returncode != 0:
        _git_unavailable(history, rel)
    return bool(history.stdout.strip())


def _refuse_unsafe_regeneration(env: str, *, rotate_restic: bool) -> None:
    """Stop before a new restic password or a CA that would not match ca.crt."""
    repo = _repo_root()
    restic_rel = f"envs/{env}/secrets/backups-restic.sops.yaml"
    restic = _sops.secret_path(repo, env, "backups-restic")
    if not restic.exists() and _git_records(restic_rel) and not rotate_restic:
        raise SystemExit(
            "refusing to recreate backups-restic because git already records it; "
            "rotation is in core/platform/backups/CONTRACT.md; "
            "pass --rotate-restic to mint a new password"
        )
    ca_secret = _secrets_dir(env) / CA_SECRET_FILENAME
    ca_root = _ca_root_path(env)
    others = [ca_root] + [
        _secrets_dir(env) / spec.filename for spec in _specs(env) if spec.filename != K3S_TOKEN_FILENAME
    ]
    if not (_secrets_dir(env) / K3S_TOKEN_FILENAME).exists() and any(p.exists() for p in others):
        raise SystemExit(
            "refusing to mint k3s tokens for an env that already has secrets; "
            "its cluster has a token already, so the owner seals it as k3s-token "
            "(core/platform/backups/CONTRACT.md, Rebuild a dead node)"
        )
    if not ca_secret.exists() and ca_root.is_file():
        raise SystemExit(
            f"refusing to mint a CA while {ca_root.relative_to(repo)} is present; "
            "delete it with the sealed CA only as part of the CA rotation"
        )


def bootstrap(env: str, *, sops_bin: str | None = None, rotate_restic: bool = False) -> list[str]:
    created: list[str] = []
    _refuse_unsafe_regeneration(env, rotate_restic=rotate_restic)
    sops = sops_bin or _sops.require_sops()
    secrets = _secrets_dir(env)
    ca_secret = secrets / CA_SECRET_FILENAME
    ca_root = _ca_root_path(env)
    for spec in _specs(env):
        name = _secret_name(spec.filename)
        path = _sops.secret_path(_repo_root(), env, name)
        if path.exists():
            continue
        document = spec.build()
        _sops.encrypt_secret_to_file(
            repo_root=_repo_root(),
            env=env,
            name=name,
            document=document,
            sops_bin=sops,
        )
        created.append(str(path.relative_to(_repo_root())))
        if spec.filename == CA_SECRET_FILENAME and not ca_root.exists():
            _write_public_root(ca_root, document["stringData"]["tls.crt"])
            created.append(str(ca_root.relative_to(_repo_root())))
    if not ca_root.exists() and ca_secret.exists():
        print(
            f"bootstrap-secrets: {ca_root.relative_to(_repo_root())} is missing; "
            "decrypting tls.crt needs the age key",
            file=sys.stderr,
        )
        _write_public_root(ca_root, _extract_tls_crt(ca_secret, sops, env))
        created.append(str(ca_root.relative_to(_repo_root())))
    return created


def bootstrap_app(env: str, namespace: str, *, sops_bin: str | None = None) -> list[str]:
    """Create-if-missing one hosted app's sealed credential and its registration Component.

    Every refusal runs before the first write. A credential git already records is never
    minted again: its repositories only open with the password it holds.
    """
    if not _app_backups.APP_NAMESPACE.fullmatch(namespace):
        raise SystemExit("refusing --app: it must be a hosted app namespace, app-<name>")
    repo = _repo_root()
    missing = []
    for spec in _app_specs(namespace):
        path = _sops.secret_path(repo, env, _secret_name(spec.filename), spec.folder)
        if path.exists():
            continue
        rel = path.relative_to(repo).as_posix()
        if _git_records(rel):
            raise SystemExit(
                f"refusing to recreate {rel} because git already records it; an app's "
                "repositories open only with its first password (core/platform/backups/CONTRACT.md, "
                "App credentials)"
            )
        missing.append((spec, path))
    component = repo / _app_backups.registration_rel(env, namespace)
    wanted = _app_backups.registration_component(namespace)
    if component.exists() and component.read_text(encoding="utf-8") != wanted:
        raise SystemExit(
            f"refusing to overwrite {component.relative_to(repo)}: it differs from the "
            "registration this tool writes; delete it and run again"
        )
    sops = (sops_bin or _sops.require_sops()) if missing else ""
    created: list[str] = []
    for spec, path in missing:
        _sops.encrypt_secret_to_file(
            repo_root=repo,
            env=env,
            name=_secret_name(spec.filename),
            namespace=spec.folder,
            document=spec.build(),
            sops_bin=sops,
        )
        created.append(str(path.relative_to(repo)))
    if not component.exists():
        component.parent.mkdir(parents=True, exist_ok=True)
        component.write_text(wanted, encoding="utf-8")
        created.append(str(component.relative_to(repo)))
    return created


def _key_file(env: str) -> Path:
    return _age.key_path(_yaml_scalar(_cluster_text(env), "age_key_file"))


def _lifecycle(args: argparse.Namespace) -> int:
    """The recipient and rotation operations: each changes the rule or a key, then re-encrypts."""
    from jarvis.secrets import recipients as _recipients

    repo, env = _repo_root(), args.env
    key_file = _key_file(env)
    os.environ["SOPS_AGE_KEY_FILE"] = str(key_file)
    if args.rotate == "deploy-key":
        # Sealing needs only the public recipients.
        return _rotate_deploy_key(env)
    # What these operations may change: the rule, the env's sealed files and the key file. A failure
    # puts each back as it was, so the repository and the key file end where they started.
    before = _snapshot([repo / ".sops.yaml", key_file, *_recipients.sealed_files(repo / "envs" / env)])
    try:
        # Every check before the first change: a key that cannot open the files must not get as far
        # as printing a recovery key or editing the rule.
        _recipients.require_working_key(repo, env, key_file)
        if args.add_recovery_key:
            if _recipients.add_recovery(repo, env) is None:
                print(f"PASS envs/{env}/secrets/ already has a second recipient; nothing changed")
        elif args.rotate == "age":
            old, new = _recipients.rotate(repo, env, key_file)
            print(f"PASS new working key {new} in {key_file}, in place of {old} in .sops.yaml")
        changed = _recipients.update(repo, env)
    except (_recipients.RecipientsError, _age.AgeError) as exc:
        print(f"FAIL secrets: {exc}")
        restored = _restore(before)
        if restored:
            print(f"     Put back as they were: {', '.join(_display(repo, path) for path in restored)}.")
            if args.add_recovery_key:
                print("     The recovery key printed above was never used: discard it.")
        return 1
    for rel in changed:
        print(f"PASS re-encrypted {rel}")
    if not changed:
        print(f"PASS every sealed file of {env} opens for exactly its recipients; nothing changed")
    if args.rotate == "age":
        print(
            "Next: commit and push, then put the new key in the cluster: "
            "kubectl -n flux-system delete secret sops-age && jarvis site --tags flux-operator "
            f"(it recreates sops-age from {_key_file(env)}). Once Flux is Ready, delete every other "
            "key's lines from that file, so it holds the new working key only."
        )
    elif changed:
        print("Next: commit and push the re-encrypted files.")
    return 0


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    """Each path's bytes, or None for one that does not exist yet."""
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _restore(before: dict[Path, bytes | None]) -> list[Path]:
    """Put each path back as the snapshot holds it; returns the ones that had changed."""
    restored = []
    for path, data in before.items():
        now = path.read_bytes() if path.is_file() else None
        if now == data:
            continue
        if data is None:
            path.unlink()
        else:
            path.write_bytes(data)
        restored.append(path)
    return restored


def _display(repo: Path, path: Path) -> str:
    return path.relative_to(repo).as_posix() if path.is_relative_to(repo) else str(path)


def _rotate_deploy_key(env: str) -> int:
    """A new read-only deploy key: sealed in place of the old one, its public half printed."""
    from jarvis.env import scaffold

    _private, public = scaffold.deploy_key(env)
    _sops.encrypt_secret_to_file(
        repo_root=_repo_root(),
        env=env,
        name="flux-deploy-key",
        document=yaml.safe_load(scaffold.deploy_key_secret(_private)),
        sops_bin=_sops.require_sops(),
    )
    print(f"PASS sealed a new envs/{env}/secrets/flux-deploy-key.sops.yaml")
    print(
        "Next: add this read-only deploy key to the repository on the forge, commit and push, then "
        "kubectl -n flux-system delete secret flux-deploy-key && jarvis site --tags flux-operator; "
        f"once Flux reads the repo again, remove the old deploy key from the forge.\n  {public}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, help="Env under envs/")
    parser.add_argument(
        "--rotate-restic",
        action="store_true",
        help=(
            "Recreate a missing backups-restic file even when git already records it. "
            "Mints a new password. This is not the restic rotation."
        ),
    )
    parser.add_argument(
        "--app",
        metavar="NAMESPACE",
        help="Mint only this hosted app's own backup credential (app-<name>).",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "--update-recipients",
        action="store_true",
        help="Re-encrypt every sealed file whose age recipients differ from .sops.yaml's rule",
    )
    which.add_argument(
        "--add-recovery-key",
        action="store_true",
        help="Give an env with one age recipient its offline recovery key (shown once), then re-encrypt",
    )
    which.add_argument(
        "--rotate",
        choices=("age", "deploy-key"),
        help="age: a new working key, every file re-encrypted to it; deploy-key: a new Flux deploy key",
    )
    args = parser.parse_args(argv)
    if args.app is not None and args.rotate_restic:
        parser.error("--app and --rotate-restic are separate runs")
    if args.update_recipients or args.add_recovery_key or args.rotate:
        if args.app is not None or args.rotate_restic:
            parser.error("--update-recipients, --add-recovery-key and --rotate are runs of their own")
        return _lifecycle(args)
    if args.app is not None:
        created = bootstrap_app(args.env, args.app)
    else:
        created = bootstrap(args.env, rotate_restic=args.rotate_restic)
    if created:
        for rel in created:
            print(f"created {rel}")
    else:
        print("bootstrap-secrets: all files present; nothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
