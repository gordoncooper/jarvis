"""jarvis verify's hosted-app component, against a stubbed kubectl (read-only calls only)."""

from __future__ import annotations

import pytest

from jarvis.verify.apps import check_app_backups, check_app_baseline, check_declared_unbacked
from jarvis.verify.common import CheckError, CheckWarning
from support import completed

SUFFIX = "env-test.example"
ENV = {"notes": {"version": "v0.1.0", "grants": ["hostname notes.example.org"]}}
# The env's grants for notes, as its namespace's annotations carry them.
GRANTS = {"apps-domain": f"apps.{SUFFIX}", "privileged": "false", "root": "false", "host-network": "false",
          "host-path": "false", "hostnames": "notes.example.org", "lan-ports": "", "lan-all": "false", "lan-targets": ""}


def _item(kind: str, namespace: str, name: str, **body: object) -> dict:
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, **body}


class Cluster:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.namespaces = [{"metadata": {"name": "app-notes", "labels": {"hosting.jarvis.io/app": "notes"},
                                         "annotations": {f"hosting.jarvis.io/{key}": value for key, value in GRANTS.items()}}}]
        self.policies = [_item("NetworkPolicy", "app-notes", name) for name in ("default-deny", "cluster-dns", "same-namespace")]
        self.ranges = [_item("LimitRange", "app-notes", "app")]
        self.bindings = [{"metadata": {"name": "app-file"}}, {"metadata": {"name": "app-grants"}}]
        self.hosts = [f"notes.apps.{SUFFIX}", "notes.example.org"]
        self.routes = [_item("HTTPRoute", "app-notes", "app-web", spec={"hostnames": self.hosts})]
        self.apps = [_item("App", "app-notes", "notes", spec={"network": {"web": [{"service": "notes", "port": 80}]},
                                                             "volumes": [{"name": "cache", "backup": False}, {"name": "data", "backup": True}]})]
        self.pvcs = [_item("PersistentVolumeClaim", "app-notes", "cache", metadata={"namespace": "app-notes", "name": "cache", "annotations": {"backup": "none"}})]
        self.sources: list[dict] = []

    def kubectl(self, args, input=None):
        argv = list(args)
        self.calls.append(argv)
        lists = {
            "namespaces": self.namespaces, "networkpolicies": self.policies, "limitranges": self.ranges,
            "validatingadmissionpolicybindings": self.bindings, "apps.hosting.jarvis.io": self.apps, "pvc": self.pvcs,
            "replicationsource": self.sources,
            "httproutes.gateway.networking.k8s.io": self.routes,
        }
        if argv[0] == "get" and argv[1] in lists and argv[2:] == ["-A", "-o", "json"]:
            return completed(argv, {"items": lists[argv[1]]})
        return completed(argv, returncode=1, stderr="NotFound")


def test_listed_app_with_its_baseline_and_grants_passes() -> None:
    """Rule: a listed app's namespace with its baseline and the env's grants, under the
    cluster-wide bindings, passes, read-only."""
    cluster = Cluster()
    check_app_baseline(cluster.kubectl, data=ENV, dns_suffix=SUFFIX)
    assert {call[0] for call in cluster.calls} == {"get"}


@pytest.mark.parametrize(
    ("seed", "expected"),
    [
        (lambda c: c.policies.pop(0), "app-notes: no NetworkPolicy default-deny"),
        (lambda c: c.policies.pop(1), "app-notes: no NetworkPolicy cluster-dns"),
        (lambda c: c.policies.pop(2), "app-notes: no NetworkPolicy same-namespace"),
        (lambda c: c.namespaces[0]["metadata"].update(labels={}), "app-notes: label hosting.jarvis.io/app is not notes"),
        (lambda c: c.bindings.pop(0), "no ValidatingAdmissionPolicyBinding app-file"),
        (lambda c: c.bindings.pop(1), "no ValidatingAdmissionPolicyBinding app-grants"),
        (lambda c: c.namespaces[0]["metadata"]["annotations"].update({"hosting.jarvis.io/privileged": "true"}),
         "app-notes: annotation hosting.jarvis.io/privileged is 'true', not the env's 'false'"),
        (lambda c: c.namespaces[0]["metadata"]["annotations"].update({"hosting.jarvis.io/lan-all": "true"}),
         "app-notes: annotation hosting.jarvis.io/lan-all is 'true', not the env's 'false'"),
        (lambda c: c.namespaces[0]["metadata"].pop("annotations"),
         "; ".join(f"app-notes: annotation hosting.jarvis.io/{key} is None, not the env's {value!r}" for key, value in GRANTS.items())),
        (lambda c: c.hosts.append(f"grafana.{SUFFIX}"),
         "app-notes/app-web: hostname 'grafana.env-test.example' is not a web entry's or granted"),
        (lambda c: c.ranges.clear(), "app-notes: no LimitRange app"),
        (lambda c: c.routes.clear(), "app-notes: no HTTPRoute app-web"),
    ],
    ids=["no-default-deny", "no-cluster-dns", "no-same-namespace", "unlabelled", "no-file-binding", "no-grants-binding",
         "grant-without-line", "lan-grant-without-line", "no-annotations", "host-outside-apps", "no-limitrange", "no-core-route"],
)
def test_app_namespace_off_its_baseline_fails(seed, expected: str) -> None:
    """Rule: each missing baseline object, a grant the env did not give, a host that is no web
    entry's and a web entry without core's route fail."""
    cluster = Cluster()
    seed(cluster)
    with pytest.raises(CheckError) as raised:
        check_app_baseline(cluster.kubectl, data=ENV, dns_suffix=SUFFIX)
    assert str(raised.value) == expected


def test_app_namespace_the_env_dropped_is_reported() -> None:
    """Rule: an app- namespace the env no longer lists is reported, not failed."""
    cluster = Cluster()
    cluster.namespaces.append({"metadata": {"name": "app-old", "labels": {"hosting.jarvis.io/app": "old"}}})
    with pytest.raises(CheckWarning) as raised:
        check_app_baseline(cluster.kubectl, data=ENV, dns_suffix=SUFFIX)
    assert str(raised.value) == ("app-old: the env no longer lists these apps; delete what they left "
                                 "(docs/add-a-hosted-app.md, Turn it off)")


def test_declared_unbacked_volume_is_reported_and_an_undeclared_one_fails() -> None:
    """Rule: a PVC the app file declares backup: false is reported as declared; backup: none on
    any other app PVC fails."""
    cluster = Cluster()
    with pytest.raises(CheckWarning) as raised:
        check_declared_unbacked(cluster.kubectl)
    assert str(raised.value) == "declared not backed up by the app file: app-notes/cache"
    cluster.pvcs.append(_item("PersistentVolumeClaim", "app-notes", "data", metadata={"namespace": "app-notes", "name": "data", "annotations": {"backup": "none"}}))
    with pytest.raises(CheckError) as raised:
        check_declared_unbacked(cluster.kubectl)
    assert str(raised.value) == "app-notes/data: annotated backup: none, and the app file does not declare them backup: false"


def test_an_app_volume_with_backups_on_needs_its_source() -> None:
    """Rule: a bound app volume with no ReplicationSource fails while its app's backups are on,
    passes with one, and is not checked while they are off."""
    cluster = Cluster()
    cluster.pvcs.append(_item("PersistentVolumeClaim", "app-notes", "data", spec={"volumeName": "pv-1"}))
    check_app_backups(cluster.kubectl, on=[])
    with pytest.raises(CheckError) as raised:
        check_app_backups(cluster.kubectl, on=["app-notes"])
    assert str(raised.value) == "app-notes/data: the app's backups are on, and no ReplicationSource"
    cluster.sources.append(_item("ReplicationSource", "app-notes", "data"))
    check_app_backups(cluster.kubectl, on=["app-notes"])
