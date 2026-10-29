# CONTRACT: internal CA

cert-manager issues the wildcard for `<dns_suffix>` from the internal CA (R28). Traefik's default TLS store reads that certificate.

## Offers

- ClusterIssuer `internal-ca` signs from Secret `cert-manager-ca` in `platform-cert-manager`.
- Certificate `wildcard` in that namespace, name `*.<dns_suffix>`, stored as Secret `wildcard-tls`.
- TLSStore `default` in that namespace, so `kube-system` keeps only the Traefik HelmChartConfig.

## Expects

- Secret `cert-manager-ca` (`tls.crt` and `tls.key`) from `make bootstrap-secrets`. The public root is `envs/<env>/ca.crt`. The private key is not written there.
- `dns_suffix` from `envs/<env>/values/cluster-vars.yaml`. Core does not hardcode it (R3, R28).
- Namespace `platform-cert-manager` from the cert-manager controller.

## Health

`Certificate/wildcard` is Ready and chains to the committed public root. The cert-manager controller, webhook, and cainjector are Ready. Each sets a memory request of `128Mi` and limit of `256Mi`, and a CPU request of `100m` with no CPU limit (D30). The chart's probes stay in place.

## Config

- Helm chart `cert-manager` `v1.21.3` from `https://charts.jetstack.io`, installed into `platform-cert-manager`. `crds.enabled` is true. The chart's empty `clusterResourceNamespace` selects the release namespace.
- CA lifetime is 10 years, set when `bootstrap-secrets` generates the key. The leaf lifetime is cert-manager's default.
- TLSStore name `default` is the one Traefik uses. The HelmChartConfig limits the default store to this namespace.

## Proven by

- `ci/tests/test_cert_manager.py` (controller, issuer, wildcard, TLSStore).
- `ci/tests/test_bootstrap_secrets.py` (public root, no key, no rewrite).
- `ci/tests/test_traefik.py` (env `k3s_version` matches the checked line in the Traefik contract).
- Certificate Ready and the chain to `ca.crt` are a post-merge drill (`make verify`).
