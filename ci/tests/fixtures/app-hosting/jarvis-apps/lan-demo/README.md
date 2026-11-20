# lan-demo

An echo server (`traefik/whoami`, pinned by digest) reachable on the LAN on TCP 8080, with no web address.

**Proves:** a risky item needs the env's grant. `app.yaml` asks for one LAN port (`network.ports: [{listen: 8080, target: 8080, protocol: TCP}]`), and `deploy/` ships a `LoadBalancer` Service for it with `port: 8080`, `targetPort: 8080` and `allocateLoadBalancerNodePorts: false`.

- Without the grant, the cluster refuses the Service. Jarvis's `app-grants` admission policy says: *a LoadBalancer exposes only ports the env grants as lan-port <port>/<protocol>*.
- With it, the Service is admitted. The grant goes under the app's entry in the env's `values/apps.yaml`:

  ```yaml
  lan-demo:
    version: v0.1.1
    grants:
      - lan-port 8080/TCP
  ```

Asking in `app.yaml` alone is not enough, and neither is the grant alone. Both must name the same port. A grants file placed in this folder grants nothing. The Service's `targetPort` must be the number `app.yaml` gives as `target`; a named port is refused.

- `app.yaml`: no `network.web` (so no web address), `volumes: []`, the one LAN port.
- `deploy/`: a Deployment (non-root, all capabilities dropped, `RuntimeDefault` seccomp, read-only root filesystem, `priorityClassName: hosted-app`, startup, readiness and liveness probes on `/health`, memory request equal to limit at 32Mi) and the LoadBalancer Service.

It stays LAN-port only: no web address, no egress, and no caller (hello-web's call to it is refused). Reachable from the LAN only.

**Env grant needed:** `lan-port 8080/TCP`.
