# CONTRACT: test app

A platform fixture answers `https://testapp.<dns_suffix>/` through one Traefik Ingress. The certificate is the internal CA wildcard on TLSStore `default`, not a secret in this namespace (R28).

## Offers

- Namespace `platform-testapp`.
- Deployment `testapp`, ClusterIP Service `testapp` on port 8080, Ingress `testapp`.
- `GET /` returns the ConfigMap file. The bytes are `testapp` plus a trailing newline. sha256 `60eea60c805ba09c287eead2e6f0c7f04b5b8e1f66634f941216527e41c8bcc7`.

## Expects

- Flux `platform-configs`, which depends on `platform-controllers` and substitutes `dns_suffix` from `envs/<env>/values/cluster-vars.yaml`. Core does not hardcode the suffix (R3, R28).
- TLSStore `default` and Certificate `wildcard` in `platform-cert-manager` (the cert-manager contract).
- Traefik Ingress class `traefik` and entrypoint `websecure` (the exposure contract). Chart v40 enables TLS on that entrypoint.
- App DNS publishes Ingress hosts in the zone. This fixture does not add a Gatus check; that lands with Gatus.

## Health

The Deployment is Ready. The container sets a memory request of `64Mi` and limit of `128Mi`, and a CPU request of `50m` with no CPU limit (R5). Readiness and liveness are HTTP `GET /` on port 8080. The image writes access and error logs to stdout and stderr.

An HTTPS response on `testapp.<dns_suffix>` whose body is the file above and whose certificate chains to the internal CA. That check is the post-merge curl.

## Config

- Image `nginxinc/nginx-unprivileged:1.30.6-alpine-slim`, digest-pinned. The process is uid 101 and listens on 8080. `docker.io/library/nginx` is not used: its master process runs as root. A custom regex manager in `renovate.json`, scoped to `platform/testapp/`, updates the tag and the digest.
- The page is ConfigMap `testapp` key `index.html`, mounted at `/usr/share/nginx/html`. No PVC, so the backup policy has nothing to enroll.
- Ingress host `testapp.${dns_suffix}`. Annotations `traefik.ingress.kubernetes.io/router.entrypoints: websecure` and `traefik.ingress.kubernetes.io/router.tls: "true"`. No `spec.tls` secret. Traefik 3.7 ignores a TLS section that has no secret name, and the default store's certificate is the wildcard.
- Prune stays enabled on the namespace.

## Proven by

- `ci/tests/test_testapp.py` (known file, non-root uid, host `${dns_suffix}`, no TLS secret, ClusterIP).
- `ci/checks/manifest-lint`, `ci/checks/loadbalancer`, and `ci/checks/digest-pin`.
- Post-merge, from the LAN: `curl -fsS --cacert envs/<env>/ca.crt https://testapp.<dns_suffix>/`.
