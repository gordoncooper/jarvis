"""Always-on platform standing checks: Flux, nodes, pods, and LAN HTTPS."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

from lib._env import reason, tool_env

from verify.common import CheckError, Context, Fetch, Issuer, Kubectl, items
from verify.registry import register

REPO_ROOT = Path(__file__).resolve().parents[2]
FLUX_RESOURCES = (
    "kustomizations.kustomize.toolkit.fluxcd.io",
    "gitrepositories.source.toolkit.fluxcd.io",
    "helmreleases.helm.toolkit.fluxcd.io",
)
ENABLELB = "svccontroller.k3s.cattle.io/enablelb"
CANARY_SHA256 = "60eea60c805ba09c287eead2e6f0c7f04b5b8e1f66634f941216527e41c8bcc7"
HTTP_TIMEOUT_SECONDS = 30
PENDING_GRACE_SECONDS = 300


def role_label_prefix(dns_suffix: str) -> str:
    return f"role.{dns_suffix}/"


def _run(
    argv: Sequence[str], *, input: str | None = None, cwd: Path | None = None
) -> CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            input=input,
            cwd=cwd,
            env=tool_env(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=HTTP_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        print("run make deps", flush=True)
        raise SystemExit(1) from None
    except subprocess.TimeoutExpired:
        return CompletedProcess(list(argv), 1, "", f"{argv[0]} timed out")


def _fetch(host: str, ca_file: Path) -> tuple[int, str]:
    proc = _run(
        [
            "curl",
            "-fsS",
            "--max-time",
            str(HTTP_TIMEOUT_SECONDS),
            "--cacert",
            str(ca_file),
            f"https://{host}/",
        ]
    )
    if proc.returncode:
        return 1, reason(proc)
    return 0, proc.stdout


def _issuer(host: str, ca_file: Path) -> tuple[int, str]:
    proc = _run(
        [
            "openssl",
            "s_client",
            "-connect",
            f"{host}:443",
            "-servername",
            host,
            "-CAfile",
            str(ca_file),
            "-no-CApath",
            "-no-CAstore",
            "-verify_return_error",
        ],
        input="",
    )
    if proc.returncode:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        useful = next(
            (
                line.strip()
                for line in lines
                if "verify error:" in line.lower()
                or "verify return code:" in line.lower()
            ),
            lines[0].strip() if lines else "certificate verify failed",
        )
        return 1, useful
    cert = _run(["openssl", "x509", "-noout", "-issuer"], input=proc.stdout)
    if cert.returncode or not cert.stdout.strip():
        return 1, "no certificate issuer"
    return 0, cert.stdout.strip()


def _inventory(repo_root: Path, env: str) -> dict[str, set[str]]:
    proc = _run(
        ["ansible-inventory", "-i", f"envs/{env}/inventory", "--list"],
        cwd=repo_root,
    )
    if proc.returncode:
        raise CheckError(f"inventory: {reason(proc)}")
    try:
        hostvars = json.loads(proc.stdout)["_meta"]["hostvars"]
        return {name: set(host["node_roles"]) for name, host in hostvars.items()}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CheckError("inventory: invalid node_roles") from exc


def check_flux(kubectl: Kubectl) -> None:
    for resource in FLUX_RESOURCES:
        found = items(kubectl(["get", resource, "-A", "-o", "json"]), f"list {resource}")
        if not found:
            raise CheckError(f"{resource}: none found")
        proc = kubectl(
            [
                "wait",
                "--for=condition=Ready",
                "--all",
                "-A",
                "--timeout=10s",
                resource,
            ]
        )
        if proc.returncode:
            raise CheckError(f"{resource}: {reason(proc)}")


def check_nodes(
    kubectl: Kubectl, roles: dict[str, set[str]], dns_suffix: str
) -> None:
    listed = items(kubectl(["get", "nodes", "-o", "json"]), "list nodes")
    try:
        nodes = {
            item["metadata"]["name"]: item["metadata"].get("labels") or {}
            for item in listed
        }
    except (KeyError, TypeError) as exc:
        raise CheckError("nodes: invalid metadata") from exc
    if set(nodes) != set(roles):
        raise CheckError(
            f"node names: inventory={sorted(roles)} cluster={sorted(nodes)}"
        )
    prefix = role_label_prefix(dns_suffix)
    for name, expected in sorted(roles.items()):
        labels = nodes[name]
        got = {
            key.removeprefix(prefix)
            for key, value in labels.items()
            if key.startswith(prefix) and value == "true"
        }
        prefixed = {key for key in labels if key.startswith(prefix)}
        if got != expected or len(prefixed) != len(got):
            raise CheckError(
                f"{name}: role labels {sorted(got)} != node_roles {sorted(expected)}"
            )
    edge = {name for name, assigned in roles.items() if "edge" in assigned}
    present = {
        name
        for name, labels in nodes.items()
        if labels.get(ENABLELB) == "true"
    }
    malformed = {
        name for name, labels in nodes.items() if ENABLELB in labels and name not in present
    }
    if present != edge or malformed:
        raise CheckError(f"enablelb nodes {sorted(present)} != edge nodes {sorted(edge)}")


def _node_ready(item: dict) -> bool:
    for condition in (item.get("status") or {}).get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def check_nodes_ready(kubectl: Kubectl) -> None:
    listed = items(kubectl(["get", "nodes", "-o", "json"]), "list nodes")
    not_ready: list[str] = []
    for item in listed:
        name = (item.get("metadata") or {}).get("name")
        if not name:
            raise CheckError("node has no name")
        if not _node_ready(item):
            not_ready.append(name)
    if not_ready:
        raise CheckError(
            "; ".join(f"{name}: not Ready" for name in sorted(not_ready))
        )


def _crash_loop(status: dict) -> bool:
    for key in ("containerStatuses", "initContainerStatuses"):
        for container in status.get(key) or []:
            if not isinstance(container, dict):
                continue
            waiting = (container.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") == "CrashLoopBackOff":
                return True
    return False


def _older_than(stamp: object, now: datetime, seconds: int) -> bool:
    if not isinstance(stamp, str):
        return True
    try:
        created = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    if created.tzinfo is None:
        return True
    return (now.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() > seconds


def check_pods(kubectl: Kubectl, now: datetime) -> None:
    listed = items(kubectl(["get", "pods", "-A", "-o", "json"]), "list pods")
    stuck: list[str] = []
    for pod in listed:
        meta = pod.get("metadata") or {}
        namespace, name = meta.get("namespace"), meta.get("name")
        if not namespace or not name:
            raise CheckError("pod has no namespace or name")
        status = pod.get("status") or {}
        if _crash_loop(status):
            stuck.append(f"{namespace}/{name}: CrashLoopBackOff")
            continue
        if status.get("phase") == "Pending" and _older_than(
            meta.get("creationTimestamp"), now, PENDING_GRACE_SECONDS
        ):
            stuck.append(f"{namespace}/{name}: Pending")
    if stuck:
        raise CheckError("; ".join(sorted(stuck)))


def check_https(
    repo_root: Path, env: str, dns_suffix: str, fetch: Fetch, issuer: Issuer
) -> str:
    host = f"testapp.{dns_suffix}"
    ca_file = repo_root / "envs" / env / "ca.crt"
    if not ca_file.is_file():
        raise CheckError(f"missing envs/{env}/ca.crt")
    code, body = fetch(host, ca_file)
    if code or not body:
        raise CheckError(f"LAN HTTPS {host}: {body or 'empty body'}")
    if hashlib.sha256(body.encode()).hexdigest() != CANARY_SHA256:
        raise CheckError(f"LAN HTTPS {host}: unexpected body")
    code, cert_issuer = issuer(host, ca_file)
    if code or not cert_issuer:
        raise CheckError(f"certificate chain {host}: {cert_issuer or 'empty issuer'}")
    return cert_issuer


@register("platform")
def platform_checks(ctx: Context) -> list[tuple[int, str, object]]:
    dns_suffix = str(ctx.values["dns_suffix"])
    return [
        (10, "Flux Ready", lambda: check_flux(ctx.kubectl)),
        (
            30,
            "node roles and enablelb",
            lambda: check_nodes(ctx.kubectl, ctx.roles, dns_suffix),
        ),
        (40, "nodes Ready", lambda: check_nodes_ready(ctx.kubectl)),
        (50, "pod health", lambda: check_pods(ctx.kubectl, ctx.now)),
    ]
