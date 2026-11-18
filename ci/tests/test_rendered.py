"""The kubeconform and policies checks (jarvis validate) on objects as Flux and Helm render them."""

from __future__ import annotations

import copy

from jarvis.checks.rendered import kubeconform, policies

PINNED = "registry.example/app:1.0@sha256:" + "a" * 64
PROBE = {"httpGet": {"path": "/", "port": 80}}
SIZED = {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "32Mi"}}


def _deployment(namespace: str = "demo", name: str = "web", **container: object) -> dict:
    spec = {"name": "web", "image": PINNED, "resources": SIZED, "livenessProbe": PROBE, "readinessProbe": PROBE}
    spec.update(container)
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [spec]}}},
    }  # fmt: skip


def test_each_rule_fails_its_seeded_object_and_passes_a_sound_one() -> None:
    """One seeded object per rule fails that rule; a sound workload and a chart's listed exception pass."""
    unprobed = _deployment(name="unprobed")
    for probe in ("livenessProbe", "readinessProbe"):
        unprobed["spec"]["template"]["spec"]["containers"][0].pop(probe)
    # A Job needs no probes and may keep a CPU limit.
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "run", "namespace": "demo"},
           "spec": {"template": {"spec": {"restartPolicy": "Never", "containers": [
               {"name": "run", "image": PINNED, "resources": {**SIZED, "limits": {"cpu": "1", "memory": "32Mi"}}}]}}}}
    seeded = {
        "images-pinned": _deployment(name="tagged", image="registry.example/app:1.0"),
        "resources": _deployment(name="unsized", resources={"requests": {"cpu": "10m"}}),
        "probes": unprobed,
        "stdout-logs": _deployment(name="file-logs", args=["--log-file=/var/log/app.log"]),
        "load-balancers": {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web", "namespace": "demo"},
                           "spec": {"type": "LoadBalancer", "ports": [{"port": 80}]}},
        "backups": {"apiVersion": "volsync.backube/v1alpha1", "kind": "ReplicationSource",
                    "metadata": {"name": "data", "namespace": "demo"}, "spec": {"sourcePVC": "data"}},
    }  # fmt: skip
    failed = policies(list(seeded.values()))
    assert {line.split(":")[0] for line in failed} == set(seeded), failed
    throttled = _deployment(resources={**SIZED, "limits": {"cpu": "1", "memory": "32Mi"}})
    assert [line.split(":")[0] for line in policies([throttled])] == ["resources"]
    excepted = copy.deepcopy(unprobed)
    excepted["metadata"] = {"name": "cert-manager-cainjector", "namespace": "platform-cert-manager"}
    assert policies([_deployment(), job, excepted]) == []


def test_kubeconform_reads_a_kind_from_the_crd_beside_it() -> None:
    """A custom object is checked against the CRD in the same objects, strictly: an unknown field fails."""
    crd = {
        "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        "metadata": {"name": "widgets.example.io"},
        "spec": {"group": "example.io", "scope": "Namespaced",
                 "names": {"kind": "Widget", "plural": "widgets", "singular": "widget"},
                 "versions": [{"name": "v1", "served": True, "storage": True, "schema": {"openAPIV3Schema": {
                     "type": "object", "properties": {
                         "apiVersion": {"type": "string"}, "kind": {"type": "string"}, "metadata": {"type": "object"},
                         "spec": {"type": "object", "properties": {"size": {"type": "integer"}}}}}}}]},
    }  # fmt: skip
    widget = {"apiVersion": "example.io/v1", "kind": "Widget", "metadata": {"name": "w", "namespace": "demo"},
              "spec": {"size": 1}}  # fmt: skip
    assert kubeconform([crd, widget], "v1.36.5+k3s1") == []
    widget["spec"]["colour"] = "red"
    assert kubeconform([crd, widget], "v1.36.5+k3s1") != []
