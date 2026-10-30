# CONTRACT: Exposure

A component is reachable at `<svc>.<dns_suffix>` by adding one Ingress (D7). cert-manager and App DNS point at this contract when those components are built.

## Offers

- App DNS answers the name.
- The internal CA wildcard covers it (R28).
- Ports 80 and 443 land on the edge nodes. Every env gives `edge` to at least one node, with no fallback to `control_plane` (D42).

## Expects

- The component adds its own Gatus check for each exposed endpoint (R5).
- Gatus trusts the internal CA's public root (committed in plain text) and resolves the zone through cluster DNS. k3s `coredns-custom` forwards that zone to App DNS.
- `dns_suffix` comes from `envs/<env>/values/cluster-vars.yaml`. Core does not hardcode it (R3, R28).

## Health

An HTTPS response on `<svc>.<dns_suffix>` whose certificate chains to the internal CA, and the component's Gatus check is up. Manifests that expose the endpoint set probes, log to stdout, set a memory request and limit, and set a CPU request with no CPU limit (R5).

`HelmChartConfig/traefik` in `kube-system` is applied, and the bundled Traefik deployment is Ready. The chart's default liveness and readiness probes stay in place. The container sets a memory request of `128Mi` and limit of `256Mi`, and a CPU request of `100m` with no CPU limit (D30).

## Config

- Hostname: `<svc>.<dns_suffix>`.
- One plain Ingress. No Gateway API. The bundled chart's Gateway provider, Gateway, and GatewayClass are off.
- Ingress class: `traefik`, the class the bundled chart registers. It is a manifest constant, not a cluster-vars key (D32).
- Wildcard name: `*.<dns_suffix>`.
- `kube-system` holds this HelmChartConfig and ConfigMap `coredns-custom` (the App DNS forward). p0b-04 puts TLSStore `default` next to the wildcard Certificate in `platform-cert-manager` (the namespace of the internal CA secret). Traefik reads the certificate secret from the TLSStore's namespace.
- Chart v40 does not template `providers.kubernetesCRD.defaultTLSResourcesNamespace`. `additionalArguments` passes `--providers.kubernetesCRD.defaultTLSResourcesNamespace=platform-cert-manager`, so only that namespace's default store counts.
- Ports 80 and 443 stay the chart's LoadBalancer Service ports. k3s ServiceLB publishes them on nodes labeled for the edge set (D42).
- Checked: bundled Traefik chart v40 on k3s v1.36.5+k3s1.

## Proven by

- `ci/tests/test_traefik.py` (HelmChartConfig and `coredns-custom` in `kube-system`, Gateway provider off, memory limit, CPU request without a CPU limit, LoadBalancer Service).
- The R5 manifest lint (`ci/checks/manifest-lint`, p0b-06).
- The P0b, P2, and P4a reachability checks (`make verify`).
