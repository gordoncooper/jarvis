# CONTRACT: App DNS

A second CoreDNS with the k8s_gateway plugin answers `${dns_suffix}` from HTTPRoute hostnames, and cluster DNS forwards the zone to it. A feature: `app-dns` under `features`. Without it, the LAN's own DNS names services (a wildcard for `${dns_suffix}` at an edge host).

## Offers

| What | Detail |
| --- | --- |
| Names | Each HTTPRoute `spec.hostnames` entry in `${dns_suffix}`, at its Gateway's `status.addresses` (Traefik's, IP preferred); each LoadBalancer Service's `coredns.io/hostname` (a hosted app's granted port, from app-hosting) and `<service>.<namespace>.${dns_suffix}`, at its own `status.loadBalancer` address; TTL 30s |
| Service | LoadBalancer on UDP and TCP 53 at ClusterIP `${app_dns_cluster_ip}`, published by ServiceLB on edge nodes; its Component lets it through core's `load-balancers` rule |
| `kube-system/coredns-custom` | Key `app-dns.server` forwards the zone to that address |
| HelmRelease `flux-system/k8s-gateway` | OCI chart `codeberg.org/k8s-gateway/charts/k8s-gateway`, pinned in `oci-repository.yaml`, image digest-pinned, namespace `platform-app-dns` (layer `feature`, Pod Security `baseline`); prune disabled on all four objects |

## Expects

| What | From |
| --- | --- |
| `dns_suffix`, `app_dns_cluster_ip` | `values/cluster-vars.yaml` |
| Gateway `platform-gateway/jarvis`, whose status carries Traefik's address | [traefik](../../platform/traefik/CONTRACT.md) |
| Edge nodes off the systemd-resolved stub (`DNSStubListener=no`), so port 53 is free | base-os, only when this feature is listed |

## Health

| Check | Pass |
| --- | --- |
| Deployment `k8s-gateway` | Ready (`/health` on 8080, `/ready` on 8181) |
| `jarvis verify --smoke` | a new HTTPRoute hostname answers at Traefik's address |
