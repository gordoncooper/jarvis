"""One hosted app's own backup credential: names, paths and the registration Component.

bootstrap-secrets writes these files and env-schema compares against them, so both read
the one definition here (platform/backups/CONTRACT.md, App credentials).
"""

from __future__ import annotations

import re

# A hosted app's namespace: app-<name>, one DNS label.
APP_NAMESPACE = re.compile(r"^app-[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?$")
# Where an app's credential Secret lives. Its sealed file sits in a folder named for the app.
SECRET_NAMESPACE = "platform-backups"
# The apps rest-server. Its users are confined to their own top-level folder (--private-repos).
APPS_SERVER = "restic-apps"
APPS_REPOSITORY = f"rest:http://{APPS_SERVER}.platform-backups.svc.cluster.local:8000/"
# The opt-in core directory the env's apps-backup-server switch lists in its platform-configs.
APPS_SERVER_DIR = "platform/optional/apps-backup-server"
# Where a credential is mounted in the apps server, restic-check and restic-offsite.
CREDENTIALS_MOUNT = "/credentials"
# Registration Components live under envs/<env>/flux/platform-configs/<REGISTRATION_DIR>/<namespace>.
REGISTRATION_DIR = "backups"
# ConfigMap in platform-backups that GeneratingPolicy pvc-backup-app reads for per-app schedule and retention.
OVERRIDES_CONFIGMAP = "backup-overrides"


def secret_name(namespace: str) -> str:
    """The app's credential Secret in platform-backups."""
    return f"backups-restic-{namespace}"


def is_secret_name(name: str) -> bool:
    """Whether name is a hosted app's credential Secret."""
    prefix = secret_name("")
    return name.startswith(prefix) and APP_NAMESPACE.fullmatch(name.removeprefix(prefix)) is not None


def rest_username(namespace: str) -> str:
    """The app's apps-server user, which is also its top-level repository folder."""
    return f"ns-{namespace}"


def sealed_rel(env: str, namespace: str) -> str:
    return f"envs/{env}/secrets/{namespace}/{secret_name(namespace)}.sops.yaml"


def registration_rel(env: str, namespace: str) -> str:
    return f"envs/{env}/flux/platform-configs/{REGISTRATION_DIR}/{namespace}/kustomization.yaml"


def _mount_ops(prefix: str, namespace: str, key: str) -> str:
    return f"""\
      - op: add
        path: {prefix}/volumes/-
        value:
          name: {namespace}
          secret:
            secretName: {secret_name(namespace)}
            items:
              - key: {key}
                path: {key}
      - op: add
        path: {prefix}/containers/0/volumeMounts/-
        value:
          name: {namespace}
          mountPath: {CREDENTIALS_MOUNT}/{namespace}
          readOnly: true
"""


def registration_component(namespace: str) -> str:
    """The Component an env lists to register one app credential.

    It applies the sealed Secret and mounts one key of it each where it is needed: the
    htpasswd line in the apps server, the restic password in restic-check and restic-offsite.
    """
    if not APP_NAMESPACE.fullmatch(namespace):
        raise ValueError(f"not a hosted app namespace: {namespace!r}")
    return (
        "# Written by make bootstrap-secrets APP=" + namespace + ". env-schema requires this exact text.\n"
        "# Registers this app's own backup credential (platform/backups/CONTRACT.md, App credentials).\n"
        "apiVersion: kustomize.config.k8s.io/v1alpha1\n"
        "kind: Component\n"
        "resources:\n"
        f"  - ../../../../secrets/{namespace}/{secret_name(namespace)}.sops.yaml\n"
        "patches:\n"
        "  - target:\n"
        "      kind: Deployment\n"
        "      namespace: platform-backups\n"
        f"      name: {APPS_SERVER}\n"
        "    patch: |-\n"
        + _mount_ops("/spec/template/spec", namespace, "htpasswd")
        + "  - target:\n"
        "      kind: CronJob\n"
        "      namespace: platform-backups\n"
        "      name: restic-(check|offsite)\n"
        "    patch: |-\n"
        + _mount_ops("/spec/jobTemplate/spec/template/spec", namespace, "RESTIC_PASSWORD")
    )
