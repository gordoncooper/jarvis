# CONTRACT: internal CA

cert-manager issues the wildcard for `<dns_suffix>` from the internal CA. Traefik's default TLS store reads that certificate.

## Offers

- ClusterIssuer `internal-ca` signs from Secret `cert-manager-ca` in `platform-cert-manager`.
- Certificate `wildcard` in that namespace, name `*.<dns_suffix>`, stored as Secret `wildcard-tls`.
- TLSStore `default` in that namespace, with `wildcard-tls` as the default certificate.
- Only in an env that turns app hosting on ([core/feature/app-hosting/CONTRACT.md](../../feature/app-hosting/CONTRACT.md)): Certificate `wildcard-apps`, name `*.apps.<dns_suffix>`, stored as Secret `wildcard-apps-tls`, which TLSStore `default` serves for a name under `.apps`. It is a separate Certificate, so it never reissues or re-keys `wildcard`. The CA's name constraint (`dns_suffix` and its subdomains) covers it. `kube-system` holds the Traefik HelmChartConfig and ConfigMap `coredns-custom`.

## Expects

- Secret `cert-manager-ca` (`tls.crt` and `tls.key`) from `jarvis secrets`. The public root is `envs/<env>/ca.crt`. The private key is not written there.
- `dns_suffix` from `envs/<env>/values/cluster-vars.yaml`. Core does not hardcode it.
- Namespace `platform-cert-manager` from the cert-manager controller.

## Health

`Certificate/wildcard` (and `Certificate/wildcard-apps` with app hosting on) is Ready and chains to the committed public root. The cert-manager controller, webhook, and cainjector are Ready. Each sets a memory request of `128Mi` and limit of `256Mi`, and a CPU request of `100m` with no CPU limit. The chart's probes stay in place.

## Config

- Helm chart `cert-manager` `v1.21.3` from `https://charts.jetstack.io`, installed into `platform-cert-manager`. `crds.enabled` is true. The chart's empty `clusterResourceNamespace` selects the release namespace.
- Controller, webhook, cainjector, startupapicheck, and acmesolver images are `quay.io/jetstack/cert-manager-*` at chart appVersion `v1.21.3`, digest-pinned. Renovate updates the chart, the tags and the digests in one PR.
- CA lifetime is 10 years, set when `jarvis secrets` generates the key. The leaf lifetime is cert-manager's default.
- TLSStore name `default` is the one Traefik uses. The HelmChartConfig limits the default store to this namespace.

## Proven by

- `ci/tests/test_cert_manager.py` (controller, issuer, wildcard, TLSStore).
- `ci/tests/test_bootstrap_secrets.py` (public root, no key, no rewrite).
- `ci/tests/test_traefik.py` (env `k3s_version` matches the checked line in the Traefik contract).
- Certificate Ready and the chain to `ca.crt` are a post-merge drill (`jarvis verify`).

## Operations

### Rotate the CA

`jarvis secrets` keeps an existing `envs/<env>/ca.crt` and does not write a new one over it. Rotation deletes that file first. The private key is never written there. cert-manager v1.21.3 does not reissue a leaf when the issuer CA changes.

1. Delete `envs/<env>/secrets/cert-manager-ca.sops.yaml` and `envs/<env>/ca.crt`.
2. From the repo root, run `jarvis secrets --env <env>`.
3. Commit the new secret and `envs/<env>/ca.crt`, and push to the branch `flux_git_ref` names. Flux syncs from that remote.
4. Wait until Flux has applied Secret `platform-cert-manager/cert-manager-ca` and its `tls.crt` matches `ca.crt`.
5. Secret `platform-cert-manager/wildcard-tls` (and `wildcard-apps-tls` with app hosting on) keeps serving the old leaf until cert-manager renews it (about day 60, the default). Those are the only leaves this CA signs today. Delete them so cert-manager issues new leaves. Each certificate is missing until its write finishes.

```bash
KUBECONFIG=~/.kube/jarvis-<env>.yaml kubectl delete secret -n platform-cert-manager wildcard-tls
# with app hosting on, also: wildcard-apps-tls
```

6. Confirm the served certificate chains to the new `ca.crt`. Exit 0. `<svc>.<dns_suffix>` is any name Traefik serves with that certificate.

```bash
openssl s_client -connect <svc>.<dns_suffix>:443 -servername <svc>.<dns_suffix> \
  -CAfile envs/<env>/ca.crt -verify_return_error </dev/null >/dev/null
```

7. Print the SHA-256 fingerprint of the new root and record it in the env's README, so a client can check the file it trusts: `openssl x509 -noout -fingerprint -sha256 -in envs/<env>/ca.crt`.
8. Trust the new `ca.crt` on every client and remove the old root from each client's trust store. A client that keeps the old root still accepts certificates the old CA signed.
