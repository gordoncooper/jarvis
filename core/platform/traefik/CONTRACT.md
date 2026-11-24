# CONTRACT: Exposure

A component is reachable at `<svc>.<dns_suffix>` by adding one HTTPRoute on the core door.

## Offers

| What | Detail |
| --- | --- |
| Traefik | k3s's bundled chart, configured by `kube-system/HelmChartConfig traefik`: Gateway API provider and GatewayClass `traefik` on; Ingress and Traefik CRD providers off, so an Ingress or IngressRoute routes nothing |
| Gateway `platform-gateway/jarvis` (`gateway/`, platform-configs) | Shared-address mode: every listener is HTTPS on Traefik's `websecure` entrypoint (`port: 8443`, published on 443), which reads a request, body included, for up to an hour |
| Core door | Listener `core`: `*.<dns_suffix>`, Certificate `wildcard` (Secret `wildcard-tls`, internal CA); routes from namespaces labelled `jarvis.io/layer` `platform` or `feature` |
| Apps door (app hosting on) | Listeners `apps` (`*.apps.<dns_suffix>`, Certificate `wildcard-apps`) and `apps-granted` (no hostname, for granted custom hostnames); routes only from namespaces with that label ([app-hosting](../../feature/app-hosting/CONTRACT.md)) |
| Ports 80 and 443 | The chart's LoadBalancer Service, published by ServiceLB on `edge` nodes; every env has at least one. Port 80 has no listener |
| Names | App DNS when listed ([app-dns](../../feature/app-dns/CONTRACT.md)), otherwise the LAN's DNS |

## Expects

| What | From |
| --- | --- |
| Gateway API CRDs (standard channel) | k3s's `traefik-crd` chart |
| `dns_suffix` | `values/cluster-vars.yaml` |
| ClusterIssuer `internal-ca` | [cert-manager](../cert-manager/CONTRACT.md) |
| Exposed workloads set probes, set a memory request and limit and a CPU request without a limit | `policies` (`jarvis validate`) |

## Health

| Check | Pass |
| --- | --- |
| Traefik Deployment | Ready, chart probes in place |
| `wildcard` (and `wildcard-apps`) | Ready, chaining to `envs/<env>/ca.crt` |
| `jarvis verify` | `testapp.<dns_suffix>` answers over HTTPS through the core door, with a certificate chaining to the internal CA |
| e2e | a hosted app answers through the apps door |

## Limits

- Both doors share one address and Traefik: a busy app can slow core, and an app cannot use a port core holds.
