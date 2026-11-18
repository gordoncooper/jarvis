# CONTRACT: App DNS

A second CoreDNS with the k8s_gateway plugin answers `${dns_suffix}` from Ingress hosts, and cluster DNS forwards the zone to it. A feature: `app-dns` under `features`. Without it, the LAN's own DNS names services (a wildcard for `${dns_suffix}` at an edge host).

## Offers

| What | Detail |
| --- | --- |
| Names | Each Ingress `spec.rules[*].host` in `${dns_suffix}`, at its `status.loadBalancer.ingress` address (IP preferred), TTL 30s |
| Service | LoadBalancer on UDP and TCP 53 at ClusterIP `${app_dns_cluster_ip}`, published by ServiceLB on edge nodes |
| `kube-system/coredns-custom` | Key `app-dns.server` forwards the zone to that address |
| HelmRelease `flux-system/k8s-gateway` | OCI chart `codeberg.org/k8s-gateway/charts/k8s-gateway`, pinned in `oci-repository.yaml`, image digest-pinned, namespace `platform-app-dns`; prune disabled on all four objects |

## Expects

| What | From |
| --- | --- |
| `dns_suffix`, `app_dns_cluster_ip` | `values/cluster-vars.yaml` |
| Traefik's LoadBalancer Service and Ingress class `traefik` | [traefik](../../platform/traefik/CONTRACT.md) |
| Edge nodes off the systemd-resolved stub (`DNSStubListener=no`), so port 53 is free | base-os, only when this feature is listed |

## Health

| Check | Pass |
| --- | --- |
| Deployment `k8s-gateway` | Ready (`/health` on 8080, `/ready` on 8181) |
| `jarvis verify --smoke` | a new Ingress host answers at Traefik's address |
