# CONTRACT: internal CA

cert-manager signs every certificate from the env's internal CA, the Gateway's wildcards included ([traefik](../traefik/CONTRACT.md)). Rotating the CA: [docs/secrets.md](../../../docs/secrets.md#rotate-the-ca).

## Offers

| What | Detail |
| --- | --- |
| ClusterIssuer `internal-ca` | Signs from Secret `platform-cert-manager/cert-manager-ca` |
| HelmRelease `cert-manager` | OCI chart `quay.io/jetstack/charts/cert-manager`, pinned in `oci-repository.yaml`; `crds.enabled`, images digest-pinned |

## Expects

| What | From |
| --- | --- |
| Secret `cert-manager-ca` (`tls.crt`, `tls.key`; 10-year CA, name-constrained to `dns_suffix`) and public root `envs/<env>/ca.crt` | `jarvis secrets` |

## Health

| Check | Pass |
| --- | --- |
| controller, webhook, cainjector | Ready |
| `jarvis verify --smoke` | `internal-ca` issues a new Certificate |
