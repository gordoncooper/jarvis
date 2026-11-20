"""jarvis verify's hosted-app component, against a stubbed kubectl (read-only calls only)."""

from __future__ import annotations

import json
from subprocess import CompletedProcess

import pytest

from jarvis.verify.apps import check_app_backups, check_app_baseline, check_declared_unbacked
from jarvis.verify.common import CheckError, CheckWarning

SUFFIX = "env-test.example"
ENV = {"notes": {"version": "v0.1.0", "grants": ["hostname notes.example.org"]}}


def _item(kind: str, namespace: str, name: str, **body: object) -> dict:
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, **body}


class Cluster:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        grants = {"apps-domain": f"apps.{SUFFIX}", "privileged": "false", "root": "false", "host-network": "false",
                  "host-path": "false", "hostnames": "notes.example.org", "lan-ports": "", "lan-all": "false", "lan-targets": ""}
        self.namespaces = [{"metadata": {"name": "app-notes", "labels": {"hosting.jarvis.io/app": "notes"},
                                         "annotations": {f"hosting.jarvis.io/{key}": value for key, value in grants.items()}}}]
        self.policies = [_item("NetworkPolicy", "app-notes", name) for name in ("default-deny", "cluster-dns", "same-namespace")]
        self.ranges = [_item("LimitRange", "app-notes", "app")]
        self.bindings = [{"metadata": {"name": "app-file"}}, {"metadata": {"name": "app-grants"}}]
        self.hosts = [f"notes.apps.{SUFFIX}", "notes.example.org"]
        self.routes = [_item("HTTPRoute", "app-notes", "app-web", spec={"hostnames": self.hosts})]
        self.apps = [_item("App", "app-notes", "notes", spec={"network": {"web": [{"service": "notes", "port": 80}]},
                                                             "volumes": [{"name": "cache", "backup": False}, {"name": "data", "backup": True}]})]
        self.pvcs = [_item("PersistentVolumeClaim", "app-notes", "cache", metadata={"namespace": "app-notes", "name": "cache", "annotations": {"backup": "none"}})]
        self.sources: list[dict] = []

    def kubectl(self, args, input=None) -> CompletedProcess[str]:
        argv = list(args)
        self.calls.append(argv)
        lists = {
            "namespaces": self.namespaces, "networkpolicies": self.policies, "limitranges": self.ranges,
            "validatingadmissionpolicybindings": self.bindings, "apps.hosting.jarvis.io": self.apps, "pvc": self.pvcs,
            "replicationsource": self.sources,
            "httproutes.gateway.networking.k8s.io": self.routes,
        }
        if argv[0] == "get" and argv[1] in lists:
            return CompletedProcess(argv, 0, json.dumps({"items": lists[argv[1]]}), "")
        return CompletedProcess(argv, 1, "", "NotFound")


def test_listed_app_with_its_baseline_and_grants_passes() -> None:
    """Rule: a listed app's namespace with its baseline and the env's grants, under the
    cluster-wide bindings, passes, read-only."""
    cluster = Cluster()
    check_app_baseline(cluster.kubectl, data=ENV, dns_suffix=SUFFIX)
    assert all(call[0] == "get" for call in cluster.calls)


@pytest.mark.parametrize(
    ("seed", "expected"),
    [
        (lambda c: c.policies.pop(0), "no NetworkPolicy default-deny"),
        (lambda c: c.namespaces[0]["metadata"].update(labels={}), "label hosting.jarvis.io/app"),
        (lambda c: c.bindings.pop(1), "no ValidatingAdmissionPolicyBinding app-grants"),
        (lambda c: c.namespaces[0]["metadata"]["annotations"].update({"hosting.jarvis.io/privileged": "true"}), "annotation hosting.jarvis.io/privileged"),
        (lambda c: c.namespaces[0]["metadata"]["annotations"].update({"hosting.jarvis.io/lan-all": "true"}), "annotation hosting.jarvis.io/lan-all"),
        (lambda c: c.namespaces[0]["metadata"].pop("annotations"), "annotation hosting.jarvis.io/apps-domain "),
        (lambda c: c.hosts.append(f"grafana.{SUFFIX}"), "hostname 'grafana.env-test.example' is not a web entry's"),
        (lambda c: c.ranges.clear(), "no LimitRange app"),
        (lambda c: c.routes.clear(), "no HTTPRoute app-web"),
    ],
    ids=["no-default-deny", "unlabelled", "no-grants-binding", "grant-without-line", "lan-grant-without-line", "no-annotations",
         "host-outside-apps", "no-limitrange", "no-core-route"],
)
def test_app_namespace_off_its_baseline_fails(seed, expected: str) -> None:
    """Rule: each missing baseline object, a grant the env did not give, a host that is no web
    entry's and a web entry without core's route fail."""
    cluster = Cluster()
    seed(cluster)
    with pytest.raises(CheckError, match=expected):
        check_app_baseline(cluster.kubectl, data=ENV, dns_suffix=SUFFIX)


def test_app_namespace_the_env_dropped_is_reported() -> None:
    """Rule: an app- namespace the env no longer lists is reported, not failed."""
    cluster = Cluster()
    cluster.namespaces.append({"metadata": {"name": "app-old", "labels": {"hosting.jarvis.io/app": "old"}}})
    with pytest.raises(CheckWarning, match="app-old"):
        check_app_baseline(cluster.kubectl, data=ENV, dns_suffix=SUFFIX)


def test_declared_unbacked_volume_is_reported_and_an_undeclared_one_fails() -> None:
    """Rule: a PVC the app file declares backup: false is reported as declared; backup: none on
    any other app PVC fails."""
    cluster = Cluster()
    with pytest.raises(CheckWarning, match="declared not backed up by the app file: app-notes/cache"):
        check_declared_unbacked(cluster.kubectl)
    cluster.pvcs.append(_item("PersistentVolumeClaim", "app-notes", "data", metadata={"namespace": "app-notes", "name": "data", "annotations": {"backup": "none"}}))
    with pytest.raises(CheckError, match="app-notes/data"):
        check_declared_unbacked(cluster.kubectl)


def test_an_app_volume_with_backups_on_needs_its_source() -> None:
    """Rule: a bound app volume with no ReplicationSource fails while its app's backups are on,
    passes with one, and is not checked while they are off."""
    cluster = Cluster()
    cluster.pvcs.append(_item("PersistentVolumeClaim", "app-notes", "data", spec={"volumeName": "pv-1"}))
    check_app_backups(cluster.kubectl, on=[])
    with pytest.raises(CheckError, match="app-notes/data: the app's backups are on, and no ReplicationSource"):
        check_app_backups(cluster.kubectl, on=["app-notes"])
    cluster.sources.append(_item("ReplicationSource", "app-notes", "data"))
    check_app_backups(cluster.kubectl, on=["app-notes"])
