# CONTRACT: Exposure

A component is reachable at `<svc>.<dns_suffix>` by adding one Ingress (D7). cert-manager and lan-dns point at this contract when those components are built.

## Offers

- JARVIS DNS answers the name.
- The internal CA wildcard covers it (R28).
- Ports 80 and 443 land on the edge nodes. Every env gives `edge` to at least one node, with no fallback to `control_plane` (D42).

## Expects

- The component adds its own Gatus check for each exposed endpoint (R5).
- Gatus trusts the internal CA's public root (committed in plain text) and resolves the zone through cluster DNS. k3s `coredns-custom` forwards that zone to JARVIS DNS.
- `dns_suffix` comes from `envs/<env>/values/cluster-vars.yaml`. Core does not hardcode it (R3, R28).

## Health

An HTTPS response on `<svc>.<dns_suffix>` whose certificate chains to the internal CA, and the component's Gatus check is up. Manifests that expose the endpoint set probes, log to stdout, set a memory request and limit, and set a CPU request with no CPU limit (R5).

## Config

- Hostname: `<svc>.<dns_suffix>`.
- One plain Ingress. No Gateway API.
- Wildcard name: `*.<dns_suffix>`.

## Proven by

- The R5 manifest lint (`ci/checks/manifest-lint`, p0b-06).
- The P0b, P2, and P4a reachability checks (`make verify`).
