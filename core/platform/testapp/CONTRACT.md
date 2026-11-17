# CONTRACT: test app

The platform canary at `https://testapp.<dns_suffix>/`. It depends on no service or app, and nothing depends on it.

## Offers

| What | Detail |
| --- | --- |
| Namespace `platform-testapp` | Deployment, ClusterIP Service on 8080 and Ingress `testapp` |
| `GET /` | ConfigMap `testapp` key `index.html`: `testapp` and a newline, sha256 `60eea60c805ba09c287eead2e6f0c7f04b5b8e1f66634f941216527e41c8bcc7` |
| Image | `nginxinc/nginx-unprivileged` (uid 101), digest-pinned, read-only root, `/tmp` emptyDir |
| Ingress | Host `testapp.${dns_suffix}`, `router.entrypoints: websecure`, `router.tls: "true"`, no `spec.tls`: the pattern every service follows |

## Expects

| What | From |
| --- | --- |
| `dns_suffix` substituted by `platform-configs` | `values/cluster-vars.yaml` |
| TLSStore `default` and Certificate `wildcard` | [cert-manager](../cert-manager/CONTRACT.md) |
| Ingress class `traefik`, entrypoint `websecure` | [traefik](../traefik/CONTRACT.md) |

## Health

| Check | Pass |
| --- | --- |
| Deployment `testapp` | Ready (HTTP `GET /` on 8080) |
| `jarvis verify` | the body above over HTTPS, chaining to the internal CA; by hand, `curl -fsS --cacert envs/<env>/ca.crt https://testapp.<dns_suffix>/` |
