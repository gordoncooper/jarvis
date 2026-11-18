# CONTRACT: internal CA

cert-manager issues the wildcard for `<dns_suffix>` from the env's internal CA; Traefik's default TLS store serves it. Rotating the CA: [docs/secrets.md](../../../docs/secrets.md#rotate-the-ca).

## Offers

| What | Detail |
| --- | --- |
| ClusterIssuer `internal-ca` | Signs from Secret `platform-cert-manager/cert-manager-ca` |
| Certificate `wildcard` | `*.<dns_suffix>`, Secret `wildcard-tls` |
| TLSStore `default` (`platform-cert-manager`) | `wildcard-tls` as the default certificate; the only store Traefik reads |
| Certificate `wildcard-apps` (app hosting on) | `*.apps.<dns_suffix>`, Secret `wildcard-apps-tls`, served by the same store; separate, so it never re-keys `wildcard` |
| HelmRelease `cert-manager` | OCI chart `quay.io/jetstack/charts/cert-manager`, pinned in `oci-repository.yaml`; `crds.enabled`, images digest-pinned |

## Expects

| What | From |
| --- | --- |
| Secret `cert-manager-ca` (`tls.crt`, `tls.key`; 10-year CA, name-constrained to `dns_suffix`) and public root `envs/<env>/ca.crt` | `jarvis secrets` |
| `dns_suffix` | `values/cluster-vars.yaml` |

## Health

| Check | Pass |
| --- | --- |
| `wildcard` (and `wildcard-apps`) | Ready, chaining to `envs/<env>/ca.crt` |
| controller, webhook, cainjector | Ready |
| `jarvis verify` | the wildcard served over HTTPS chains to `ca.crt` |
| `jarvis verify --smoke` | `internal-ca` issues a new Certificate |
