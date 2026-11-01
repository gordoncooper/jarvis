"""p0b-08 fixture: known file at testapp.${dns_suffix}, no per-Ingress TLS secret (R28).

The pod uses RuntimeDefault seccomp and a read-only root. /tmp is the only emptyDir.
"""

from __future__ import annotations

import hashlib

import yaml

from support import _kustomize_build

_BODY = "testapp\n"
_SHA = "60eea60c805ba09c287eead2e6f0c7f04b5b8e1f66634f941216527e41c8bcc7"


def test_fixture_serves_known_file_on_the_default_certificate() -> None:
    docs = {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(_kustomize_build("platform/testapp"))
        if isinstance(doc, dict) and isinstance(doc.get("metadata"), dict)
    }
    body = docs[("ConfigMap", "testapp")]["data"]["index.html"]
    assert body == _BODY
    assert hashlib.sha256(body.encode()).hexdigest() == _SHA
    assert "PersistentVolumeClaim" not in {kind for kind, _name in docs}

    pod = docs[("Deployment", "testapp")]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 101
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["volumes"] == [
        {"name": "html", "configMap": {"name": "testapp"}},
        {"name": "tmp", "emptyDir": {}},
    ]
    assert container["volumeMounts"] == [
        {"name": "html", "mountPath": "/usr/share/nginx/html", "readOnly": True},
        {"name": "tmp", "mountPath": "/tmp"},
    ]

    ingress = docs[("Ingress", "testapp")]
    assert ingress["spec"]["rules"][0]["host"] == "testapp.${dns_suffix}"
    assert "tls" not in ingress["spec"]
    annotations = ingress["metadata"]["annotations"]
    assert annotations["traefik.ingress.kubernetes.io/router.entrypoints"] == "websecure"
    assert annotations["traefik.ingress.kubernetes.io/router.tls"] == "true"
    assert docs[("Service", "testapp")]["spec"]["type"] == "ClusterIP"
