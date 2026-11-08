"""Fixture: known file at testapp.${dns_suffix}, no per-Ingress TLS secret.

The pod uses RuntimeDefault seccomp and a read-only root. /tmp is the only emptyDir.
"""

from __future__ import annotations

import yaml

from support import _kustomize_build

_BODY = "testapp\n"


def test_fixture_serves_known_file_on_the_default_certificate() -> None:
    docs = {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(_kustomize_build("core/platform/testapp"))
        if isinstance(doc, dict) and isinstance(doc.get("metadata"), dict)
    }
    body = docs[("ConfigMap", "testapp")]["data"]["index.html"]
    assert body == _BODY
    assert "PersistentVolumeClaim" not in {kind for kind, _name in docs}

    pod = docs[("Deployment", "testapp")]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 101
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    # Every mount names a declared volume, and /tmp is the only writable mount.
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert {mount["name"] for mount in container["volumeMounts"]} <= set(volumes)
    writable = [mount for mount in container["volumeMounts"] if not mount.get("readOnly")]
    assert [mount["mountPath"] for mount in writable] == ["/tmp"]
    assert "emptyDir" in volumes[writable[0]["name"]]
    assert [name for name, volume in volumes.items() if "emptyDir" in volume] == [writable[0]["name"]]

    ingress = docs[("Ingress", "testapp")]
    assert ingress["spec"]["rules"][0]["host"] == "testapp.${dns_suffix}"
    assert "tls" not in ingress["spec"]
    annotations = ingress["metadata"]["annotations"]
    assert annotations["traefik.ingress.kubernetes.io/router.entrypoints"] == "websecure"
    assert annotations["traefik.ingress.kubernetes.io/router.tls"] == "true"
    assert docs[("Service", "testapp")]["spec"]["type"] == "ClusterIP"
