"""The kubeconform and policies checks (jarvis validate) on objects as Flux and Helm render them."""

from __future__ import annotations

import shutil
from pathlib import Path

import sample
import yaml

from jarvis.checks.rendered import core_rules, git_rules, kubeconform, policies
from support import REPO_ROOT, ExampleCopy, flux_built, render_switches, set_switches

PINNED = "registry.example/app:1.0@sha256:" + "a" * 64
PROBE = {"httpGet": {"path": "/", "port": 80}}
SIZED = {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "32Mi"}}
RULES = [*yaml.safe_load_all((REPO_ROOT / "core/platform/kyverno/rules/rules.yaml").read_text(encoding="utf-8")), *git_rules()]
FLUX = {"kustomize.toolkit.fluxcd.io/name": "platform-configs", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
KEPT = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}
CRD, RELEASE = "apiextensions.k8s.io/v1", "helm.toolkit.fluxcd.io/v2"


def _applied(kind: str, name: str, *, api: str = "v1", namespace: str | None = None, flux: bool = True,
             annotations: dict | None = None, spec: dict | None = None) -> dict:
    """An object as Flux's build gives it (its name label), or as a chart renders it (flux=False)."""
    meta = {"name": name, **({"namespace": namespace} if namespace else {}),
            **({"labels": FLUX} if flux else {}), **({"annotations": annotations} if annotations else {})}
    return {"apiVersion": api, "kind": kind, "metadata": meta, **({"spec": spec} if spec else {})}


def _deployment(name: str, namespace: str = "demo", init: dict | None = None, **container: object) -> dict:
    """A sound Deployment of one container, web, with container's fields changed (None drops one), and init as
    its init container."""
    spec = {"name": "web", "image": PINNED, "resources": SIZED, "livenessProbe": PROBE, "readinessProbe": PROBE, **container}
    pod = {"containers": [{key: value for key, value in spec.items() if value is not None}], **({"initContainers": [init]} if init else {})}
    return {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": {"matchLabels": {"app": name}}, "template": {"metadata": {"labels": {"app": name}}, "spec": pod}},
    }  # fmt: skip


UNSIZED = "needs cpu and memory requests and a memory limit: web"
UNPROBED = "needs a liveness and a readiness probe: web"
PRUNED = "Flux keeps this kind: it carries kustomize.toolkit.fluxcd.io/prune: disabled, so a bad commit or a moved path cannot delete it"
OPTED_OUT = "a PVC in Git does not opt out of backups (backup: none)"
THROTTLED = {**SIZED, "limits": {"cpu": "1", "memory": "32Mi"}}
DB = _deployment("db")
# Each object breaks one field, so each line shows its rule caught that break on its own.
SEEDED = [
    (_deployment("tagged", image="registry.example/app:1.0"), "images-pinned: demo/Deployment/tagged: image not pinned by digest: web"),
    (_deployment("short-digest", image=PINNED[:-1]), "images-pinned: demo/Deployment/short-digest: image not pinned by digest: web"),
    (_deployment("init-tagged", init={"name": "init", "image": "registry.example/app:1.0", "resources": SIZED}),
     "images-pinned: demo/Deployment/init-tagged: image not pinned by digest: init"),
    (_deployment("no-cpu-request", resources={**SIZED, "requests": {"memory": "16Mi"}}), f"resources: demo/Deployment/no-cpu-request: {UNSIZED}"),
    (_deployment("no-memory-request", resources={**SIZED, "requests": {"cpu": "10m"}}), f"resources: demo/Deployment/no-memory-request: {UNSIZED}"),
    (_deployment("no-memory-limit", resources={"requests": SIZED["requests"]}), f"resources: demo/Deployment/no-memory-limit: {UNSIZED}"),
    (_deployment("init-unsized", init={"name": "init", "image": PINNED}),
     "resources: demo/Deployment/init-unsized: needs cpu and memory requests and a memory limit: init"),
    (_deployment("throttled", resources=THROTTLED), "resources: demo/Deployment/throttled: long-running container has a cpu limit: web"),
    (_deployment("no-liveness", livenessProbe=None), f"probes: demo/Deployment/no-liveness: {UNPROBED}"),
    (_deployment("no-readiness", readinessProbe=None), f"probes: demo/Deployment/no-readiness: {UNPROBED}"),
    (_applied("Service", "web", namespace="demo", spec={"type": "LoadBalancer", "ports": [{"port": 80}]}),
     "load-balancers: demo/Service/web: not a LoadBalancer Service: core publishes through Traefik's"),
    (_applied("ReplicationSource", "data", api="volsync.backube/v1alpha1", namespace="demo", spec={"sourcePVC": "data"}),
     "backups: demo/ReplicationSource/data: Git carries no ReplicationSource or ReplicationDestination; the pvc-backup policies make them"),
    (_applied("PersistentVolumeClaim", "opted-out", namespace="demo", annotations={**KEPT, "backup": "none"}),
     f"backups: demo/PersistentVolumeClaim/opted-out: {OPTED_OUT}"),
    ({**DB, "kind": "StatefulSet", "spec": {**DB["spec"], "volumeClaimTemplates": [{"metadata": {"name": "data", "annotations": {"backup": "none"}}}]}},
     f"backups: demo/StatefulSet/db: {OPTED_OUT}"),
    (_applied("PersistentVolumeClaim", "data", namespace="demo"), f"flux-prune: demo/PersistentVolumeClaim/data: {PRUNED}"),
    # The twins of the exceptions below: a Flux object of each kind, a core release, another namespace.
    (_applied("CustomResourceDefinition", "gadgets.example.io", api=CRD), f"flux-prune: CustomResourceDefinition/gadgets.example.io: {PRUNED}"),
    (_applied("HelmRelease", "kyverno", api=RELEASE, namespace="flux-system", spec={"serviceAccountName": "flux-platform"}),
     f"flux-prune: flux-system/HelmRelease/kyverno: {PRUNED}"),
    (_applied("Namespace", "platform-demo"), f"flux-prune: Namespace/platform-demo: {PRUNED}"),
]  # fmt: skip
SOUND = [
    _deployment("web"),
    _applied("Service", "internal", namespace="demo", spec={"ports": [{"port": 80}]}),  # no type: a ClusterIP
    # A Job needs no probes; a Job and an init container may keep a CPU limit.
    _applied("Job", "run", api="batch/v1", namespace="demo", spec={"template": {"spec": {"restartPolicy": "Never",
             "containers": [{"name": "run", "image": PINNED, "resources": THROTTLED}]}}}),
    _deployment("init-throttled", init={"name": "init", "image": PINNED, "resources": THROTTLED}),
    _deployment("cert-manager-cainjector", "platform-cert-manager", livenessProbe=None, readinessProbe=None),
    # Flux keeps its protected kinds, but not a chart's objects, a tenant's release or the canary's namespace.
    _applied("PersistentVolumeClaim", "kept", namespace="demo", annotations=KEPT),
    _applied("CustomResourceDefinition", "widgets.example.io", api=CRD, flux=False),
    _applied("HelmRelease", "app-web", api=RELEASE, namespace="flux-system", spec={"serviceAccountName": "app-web"}),
    _applied("Namespace", "platform-testapp"),
]  # fmt: skip


def test_each_rule_fails_its_seeded_object_and_passes_a_sound_one() -> None:
    """Rule: each seeded object fails its one rule with that rule's message; a sound workload and each
    rule's exceptions pass."""
    assert policies([*(doc for doc, _ in SEEDED), *SOUND], RULES) == sorted(line for _, line in SEEDED)


def test_a_feature_brings_its_own_exception(example_copy: ExampleCopy, tmp_path: Path) -> None:
    """Rule: App DNS's LoadBalancer passes core's rules as an env that lists App DNS builds them, and
    fails them once that env switches App DNS off."""
    service = _applied("Service", "k8s-gateway", namespace="platform-app-dns", flux=False, spec={"type": "LoadBalancer", "ports": [{"port": 53}]})
    off = ExampleCopy(shutil.copytree(example_copy.root, tmp_path / "repo", symlinks=True), example_copy.name)
    set_switches(off.env_dir, features=[feature for feature in sample.ALL_FEATURES if feature != "app-dns"])
    render_switches(off.env_dir, off.root)
    found = [policies([service], [*core_rules(flux_built(copy.root, copy.name)), *git_rules()]) for copy in (example_copy, off)]
    assert found == [[], ["load-balancers: platform-app-dns/Service/k8s-gateway: not a LoadBalancer Service: core publishes"
                          " through Traefik's"]]


def test_kubeconform_reads_a_kind_from_the_crd_beside_it() -> None:
    """Rule: a custom object is checked against the CRD in the same objects, strictly: an unknown field fails."""
    crd = {
        "apiVersion": CRD, "kind": "CustomResourceDefinition", "metadata": {"name": "widgets.example.io"},
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
    (problem,) = kubeconform([crd, widget], "v1.36.5+k3s1")
    # kubeconform names the schema file it read, in a temporary folder, between these two.
    assert problem.startswith("Widget w: ") and problem.endswith(" - at '/spec': additional properties 'colour' not allowed"), problem
