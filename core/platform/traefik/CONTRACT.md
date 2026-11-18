# CONTRACT: Exposure

A component is reachable at `<svc>.<dns_suffix>` by adding one Ingress.

## Offers

| What | Detail |
| --- | --- |
| Ingress class `traefik`, entrypoint `websecure` | k3s's bundled Traefik, configured by `kube-system/HelmChartConfig traefik`; no Gateway API yet |
| TLS | The default TLSStore in `platform-cert-manager` only (`--providers.kubernetesCRD.defaultTLSResourcesNamespace=platform-cert-manager`), serving `*.<dns_suffix>` |
| Ports 80 and 443 | The chart's LoadBalancer Service, published by ServiceLB on `edge` nodes; every env has at least one |
| Names | App DNS when listed ([app-dns](../../feature/app-dns/CONTRACT.md)), otherwise the LAN's DNS |

## Expects

| What | From |
| --- | --- |
| `dns_suffix` | `values/cluster-vars.yaml` |
| Exposed workloads set probes, log to stdout, set a memory request and limit and a CPU request without a limit | `policies` (`jarvis validate`) |

## Health

| Check | Pass |
| --- | --- |
| Traefik Deployment | Ready, chart probes in place |
| `<svc>.<dns_suffix>` | answers over HTTPS with a certificate chaining to the internal CA |

## Limits

- Checked: bundled Traefik chart v40 on k3s v1.36.5+k3s1.
