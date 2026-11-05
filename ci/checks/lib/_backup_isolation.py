"""No app backup credential reaches core, and core's never reaches an app.

On what each env's platform-configs path renders (and on the apps server core ships):

- A pvc-backup rule that generates a Secret and can match an app- namespace reads only that
  app's own platform-backups/backups-restic-<namespace>, never core's backups-restic.
- Core's rest-server (Deployment restic) takes its users only from backups-restic.
- The apps rest-server (Deployment restic-apps) runs --private-repos, never mounts backups-restic,
  and mounts only the htpasswd key of app credentials.
- An env that renders an app credential also renders the apps server.
"""

from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass
from pathlib import Path

from _manifests import build_flux_path, kustomize_build, pod_spec
from schema.env import CORE_SOURCE_DIR

_TOOLS_LIB = Path(__file__).resolve().parents[3] / "tools" / "lib"
if str(_TOOLS_LIB) not in sys.path:
    sys.path.insert(0, str(_TOOLS_LIB))

import _app_backups  # noqa: E402

CORE_SECRET = "backups-restic"
CORE_SERVER = "restic"
POLICY = "pvc-backup"
APP_SECRET_URL = (
    "/api/v1/namespaces/platform-backups/secrets/"
    + _app_backups.secret_name("{{ request.object.metadata.namespace }}")
)
# A namespace name only an app- pattern can match.
_PROBE_APP_NAMESPACE = "app-probe"


@dataclass(frozen=True)
class Violation:
    where: str
    reason: str


def scan_backup_isolation(root: Path) -> list[Violation]:
    root = Path(root).resolve()
    found: list[Violation] = []
    shipped = f"{CORE_SOURCE_DIR}/{_app_backups.APPS_SERVER_DIR}"
    docs, error = kustomize_build(root, shipped)
    if error:
        found.append(Violation(_app_backups.APPS_SERVER_DIR, error))
    found.extend(_render_violations(_app_backups.APPS_SERVER_DIR, docs))
    envs = root / "envs"
    for env_dir in sorted(p for p in envs.iterdir() if p.is_dir()) if envs.is_dir() else []:
        rel = f"envs/{env_dir.name}/flux/platform-configs"
        if not (root / rel).is_dir():
            continue
        path = build_flux_path(root, f"envs/{env_dir.name}/", {"spec": {"path": f"./{rel}"}})
        if path.error:
            found.append(Violation(rel, path.error))
            continue
        found.extend(_render_violations(rel, path.docs))
    return found


def format_violations(found: list[Violation]) -> list[str]:
    return [f"{item.where}: {item.reason}" for item in found]


def _render_violations(where: str, docs: list[dict]) -> list[Violation]:
    found: list[Violation] = []
    names = {(doc.get("kind"), (doc.get("metadata") or {}).get("name")) for doc in docs}
    for doc in docs:
        kind, name = doc.get("kind"), (doc.get("metadata") or {}).get("name")
        if kind == "ClusterPolicy" and name == POLICY:
            found.extend(Violation(where, reason) for reason in _policy_reasons(doc))
        elif kind == "Deployment" and name == CORE_SERVER:
            for secret, _keys in _secret_volumes(doc):
                if secret != CORE_SECRET:
                    found.append(
                        Violation(where, f"core's rest-server mounts Secret {secret}; its users come only from {CORE_SECRET}")
                    )
        elif kind == "Deployment" and name == _app_backups.APPS_SERVER:
            found.extend(Violation(where, reason) for reason in _apps_server_reasons(doc))
        elif kind == "Secret" and isinstance(name, str) and name.startswith(_app_backups.secret_name("app-")):
            if ("Deployment", _app_backups.APPS_SERVER) not in names:
                found.append(
                    Violation(where, f"Secret {name} is an app credential, but {_app_backups.APPS_SERVER_DIR} is not listed")
                )
    return found


def _policy_reasons(policy: dict) -> list[str]:
    reasons = []
    for rule in (policy.get("spec") or {}).get("rules") or []:
        generate = rule.get("generate") or {}
        if generate.get("kind") != "Secret" or not _can_match_app(rule):
            continue
        urls = [
            str((entry.get("apiCall") or {}).get("urlPath", ""))
            for entry in rule.get("context") or []
            if "/secrets/" in str((entry.get("apiCall") or {}).get("urlPath", ""))
        ]
        if urls != [APP_SECRET_URL]:
            reasons.append(
                f"rule {rule.get('name')} generates a Secret in an app- namespace from {urls or 'no Secret'}; "
                f"it may read only that app's own credential, never {CORE_SECRET}"
            )
    return reasons


def _can_match_app(rule: dict) -> bool:
    match = rule.get("match") or {}
    filters = [item.get("resources") or {} for key in ("any", "all") for item in match.get(key) or []]
    filters = filters or [match.get("resources") or {}]
    if not any(_namespace_matches(item.get("namespaces")) for item in filters):
        return False
    for item in (rule.get("exclude") or {}).get("any") or []:
        resources = item.get("resources") or {}
        if set(resources) == {"namespaces"} and _namespace_matches(resources["namespaces"]):
            return False
    return True


def _namespace_matches(patterns: object) -> bool:
    if patterns is None:
        return True
    return isinstance(patterns, list) and any(
        isinstance(item, str) and fnmatch.fnmatchcase(_PROBE_APP_NAMESPACE, item) for item in patterns
    )


def _apps_server_reasons(doc: dict) -> list[str]:
    reasons = []
    for secret, keys in _secret_volumes(doc):
        if not secret.startswith(_app_backups.secret_name("app-")):
            reasons.append(f"the apps server mounts Secret {secret}; only app credentials")
        elif keys != ["htpasswd"]:
            reasons.append(f"the apps server mounts {secret} keys {keys}; only htpasswd")
    pod = pod_spec(doc) or {}
    command = "\n".join(
        str(part) for container in pod.get("containers") or [] for part in (container.get("command") or []) + (container.get("args") or [])
    )
    if "--private-repos" not in command:
        reasons.append("the apps server runs without --private-repos, so an app login is not confined to its own folder")
    return reasons


def _secret_volumes(doc: dict) -> list[tuple[str, list[str] | None]]:
    """(Secret name, mounted keys or None for all) for each Secret a workload mounts."""
    found: list[tuple[str, list[str] | None]] = []
    for volume in (pod_spec(doc) or {}).get("volumes") or []:
        sources = [volume] + list((volume.get("projected") or {}).get("sources") or [])
        for source in sources:
            secret = source.get("secret")
            if isinstance(secret, dict):
                name = secret.get("secretName") or secret.get("name")
                items = secret.get("items")
                keys = [item.get("key") for item in items] if isinstance(items, list) else None
                found.append((str(name), keys))
    return found
