"""Standing checks for the hello add-on. jarvis verify loads this file only when the env enables hello."""

from __future__ import annotations

from jarvis.env.runtime import reason
from jarvis.verify.backups import SECRET_KEYS_TEMPLATE
from jarvis.verify.common import CheckError, Context, items
from jarvis.verify.registry import register

NAMESPACE = "hello"
SECRET = "hello-greeting"


def check_hello(ctx: Context) -> None:
    deployments = items(
        ctx.kubectl(["get", "deployments", "-n", NAMESPACE, "-o", "json"]),
        f"list {NAMESPACE} deployments",
    )
    ready = {
        (item.get("metadata") or {}).get("name"): (item.get("status") or {}).get("readyReplicas") or 0
        for item in deployments
    }
    if not ready.get("hello"):
        raise CheckError(f"{NAMESPACE}/hello: no ready replica")
    # Key names only: verify never reads Secret values.
    proc = ctx.kubectl(
        ["get", "secret", SECRET, "-n", NAMESPACE, "-o", f"go-template={SECRET_KEYS_TEMPLATE}"]
    )
    if proc.returncode:
        raise CheckError(f"read {NAMESPACE}/{SECRET}: {reason(proc)}")
    if not proc.stdout.strip():
        raise CheckError(f"{NAMESPACE}/{SECRET} has no data keys")


@register("hello")
def hello_checks(ctx: Context) -> list[tuple[int, str, object]]:
    return [(60, "add-on hello Ready", lambda: check_hello(ctx))]
