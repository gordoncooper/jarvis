# CONTRACT: App DNS

A second CoreDNS, with the k8s_gateway plugin, answers `${dns_suffix}` from Ingress routes. Cluster DNS forwards that zone here.

## Offers

- Names from Ingress `spec.rules[*].host` in `${dns_suffix}`.
- The address is `status.loadBalancer.ingress`, with IP preferred. For Traefik that is the edge node address ServiceLB publishes.
- A LoadBalancer Service on UDP and TCP 53. k3s ServiceLB publishes it on the edge nodes.
- `kube-system` ConfigMap `coredns-custom` forwards the zone to this Service's ClusterIP.

## Expects

- `dns_suffix` and `app_dns_cluster_ip` from `envs/<env>/values/cluster-vars.yaml`. Core does not hardcode them (R3, R28).
- Traefik's LoadBalancer Service and Ingress class `traefik` (the exposure contract).
- The `edge` role. The k3s role sets `svccontroller.k3s.cattle.io/enablelb=true` on those nodes only.
- Chart `k8s-gateway` `4.0.1` from `oci://codeberg.org/k8s-gateway/charts`. The image tag is the chart appVersion.

## Health

The k8s-gateway Deployment is Ready. The chart's liveness probe is `/health` on port 8080 and the readiness probe is `/ready` on port 8181, which requires the `health` and `ready` plugins. The container sets a memory request of `128Mi` and limit of `256Mi`, and a CPU request of `100m` with no CPU limit (D30). The chart default sets a CPU limit; `resources.limits.cpu: null` drops it.

From a pod, an Ingress host in the zone resolves through this Service and through cluster DNS to an edge node address. That check is the post-merge drill.

## Config

- Namespace `platform-app-dns`. HelmRelease `k8s-gateway` in `flux-system`.
- One server block for `${dns_suffix}`, port 53. `use_tcp` on that zone makes the chart open both UDP and TCP 53.
- Plugin `k8s_gateway` watches Ingress only. `loadBalancerAddressPreference ip`. TTL 30 seconds.
- `service.clusterIP` is `${app_dns_cluster_ip}`.
- `coredns-custom` key `app-dns.server` forwards the zone to that address. CoreDNS `forward` takes an address, and k3s CoreDNS resolves names with the node resolver, so the target is not a Service hostname.
- Prune is disabled on the namespace, the HelmRepository, the HelmRelease, and `coredns-custom`.
- `kube-system` holds this ConfigMap and the Traefik HelmChartConfig.

## Proven by

- `ci/tests/test_app_dns.py` (LoadBalancer port 53, zone `${dns_suffix}`, forward target, D30 resources, probes).
- `ci/tests/test_prune_guards.py` (namespace, chart source, HelmRelease, and `coredns-custom`).
- The post-merge drill: a throwaway Ingress resolves from a pod through App DNS and through cluster DNS.
