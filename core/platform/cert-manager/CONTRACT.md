# CONTRACT: internal CA

cert-manager issues the wildcard for `<dns_suffix>` from the env's internal CA; Traefik's default TLS store serves it.

## Offers

| What | Detail |
| --- | --- |
| ClusterIssuer `internal-ca` | Signs from Secret `platform-cert-manager/cert-manager-ca` |
| Certificate `wildcard` | `*.<dns_suffix>`, Secret `wildcard-tls` |
| TLSStore `default` (`platform-cert-manager`) | `wildcard-tls` as the default certificate; the only store Traefik reads |
| Certificate `wildcard-apps` (app hosting on) | `*.apps.<dns_suffix>`, Secret `wildcard-apps-tls`, served by the same store; separate, so it never re-keys `wildcard` |
| HelmRelease `cert-manager` | Chart `v1.21.3` from `https://charts.jetstack.io`, `crds.enabled`, images digest-pinned |

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

## Rotate the CA

`jarvis secrets` never writes over an existing `ca.crt`, and cert-manager does not reissue a leaf when its CA changes.

1. Delete `envs/<env>/secrets/cert-manager-ca.sops.yaml` and `envs/<env>/ca.crt`, then run `jarvis secrets --env <env>`.
2. Commit both and push. Wait until Secret `platform-cert-manager/cert-manager-ca`'s `tls.crt` matches `ca.crt`.
3. `KUBECONFIG=~/.kube/jarvis-<env>.yaml kubectl delete secret -n platform-cert-manager wildcard-tls` (and `wildcard-apps-tls`), so cert-manager issues new leaves.
4. Check: `openssl s_client -connect <svc>.<dns_suffix>:443 -servername <svc>.<dns_suffix> -CAfile envs/<env>/ca.crt -verify_return_error </dev/null >/dev/null` exits 0.
5. Record `openssl x509 -noout -fingerprint -sha256 -in envs/<env>/ca.crt` in the env's README, trust the new root on every client, and remove the old one.
