"""The kubeconform and policies checks (jarvis validate) on objects as Flux and Helm render them."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from jarvis.checks.rendered import core_rules, git_rules, kubeconform, policies
from support import REPO_ROOT, ExampleCopy, bootstrap_example_copy, flux_built, render_switches, set_switches

PINNED = "registry.example/app:1.0@sha256:" + "a" * 64
PROBE = {"httpGet": {"path": "/", "port": 80}}
SIZED = {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "32Mi"}}
RULES = [*yaml.safe_load_all((REPO_ROOT / "core/platform/kyverno/rules/rules.yaml").read_text(encoding="utf-8")), *git_rules()]
FLUX = {"kustomize.toolkit.fluxcd.io/name": "platform-configs", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
KEPT = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}


def _applied(kind: str, name: str, *, api: str = "v1", namespace: str | None = None, flux: bool = True,
             kept: bool = False, spec: dict | None = None) -> dict:
    """An object as Flux's build gives it (its name label), or as a chart renders it (flux=False)."""
    meta = {"name": name, **({"namespace": namespace} if namespace else {}),
            **({"labels": FLUX} if flux else {}), **({"annotations": KEPT} if kept else {})}
    return {"apiVersion": api, "kind": kind, "metadata": meta, **({"spec": spec} if spec else {})}


def _deployment(namespace: str = "demo", name: str = "web", **container: object) -> dict:
    spec = {"name": "web", "image": PINNED, "resources": SIZED, "livenessProbe": PROBE, "readinessProbe": PROBE}
    spec.update(container)
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [spec]}}},
    }  # fmt: skip


def test_each_rule_fails_its_seeded_object_and_passes_a_sound_one() -> None:
    """One seeded object per rule fails that rule; a sound workload and each rule's exceptions pass."""
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
        "load-balancers": {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web", "namespace": "demo"},
                           "spec": {"type": "LoadBalancer", "ports": [{"port": 80}]}},
        "backups": {"apiVersion": "volsync.backube/v1alpha1", "kind": "ReplicationSource",
                    "metadata": {"name": "data", "namespace": "demo"}, "spec": {"sourcePVC": "data"}},
        "flux-prune": _applied("PersistentVolumeClaim", "data", namespace="demo"),
    }  # fmt: skip
    failed = policies(list(seeded.values()), RULES)
    assert {line.split(":")[0] for line in failed} == set(seeded), failed
    throttled = _deployment(resources={**SIZED, "limits": {"cpu": "1", "memory": "32Mi"}})
    assert [line.split(":")[0] for line in policies([throttled], RULES)] == ["resources"]
    excepted = copy.deepcopy(unprobed)
    excepted["metadata"] = {"name": "cert-manager-cainjector", "namespace": "platform-cert-manager"}
    # Flux keeps its protected kinds, but not a chart's objects, a tenant's release or the canary's namespace.
    kept = [
        _applied("PersistentVolumeClaim", "data", namespace="demo", kept=True),
        _applied("CustomResourceDefinition", "widgets.example.io", api="apiextensions.k8s.io/v1", flux=False),
        _applied("HelmRelease", "app-web", api="helm.toolkit.fluxcd.io/v2", namespace="flux-system",
                 spec={"serviceAccountName": "app-web"}),
        _applied("Namespace", "platform-testapp"),
    ]  # fmt: skip
    assert policies([_deployment(), job, excepted, *kept], RULES) == []


def test_a_feature_brings_its_own_exception(example_copy: ExampleCopy, tmp_path: Path) -> None:
    """App DNS's LoadBalancer passes core's rules as an env that lists App DNS builds them, and
    fails them in one that does not."""
    service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "k8s-gateway", "namespace": "platform-app-dns"},
               "spec": {"type": "LoadBalancer", "ports": [{"port": 53}]}}  # fmt: skip
    off = bootstrap_example_copy(tmp_path)
    set_switches(off.env_dir, features=["alerts", "app-hosting", "offsite-backup-copy"])
    render_switches(off.env_dir, off.root)
    found = [policies([service], [*core_rules(flux_built(copy.root, copy.name)), *git_rules()]) for copy in (example_copy, off)]
    assert found[0] == [] and [line.split(":")[0] for line in found[1]] == ["load-balancers"]


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
