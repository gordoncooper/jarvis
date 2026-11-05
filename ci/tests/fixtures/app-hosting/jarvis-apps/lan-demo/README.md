# lan-demo

An echo server (`traefik/whoami`, pinned by digest) reachable on the LAN on TCP 8080, with no web address.

**Proves:** a risky item needs the env's exception line. `app.yaml` asks for one LAN port (`network.ports: [{listen: 8080, target: 8080, protocol: TCP}]`), and `deploy/` ships a `LoadBalancer` Service for it with `port: 8080`, `targetPort: 8080` and `allocateLoadBalancerNodePorts: false`.

- Without the line, the cluster refuses the Service. Jarvis's `app-grants` admission policy says: *a LoadBalancer exposes only ports the env grants as '<app>: lan-port <port>/<protocol>'*.
- With the line, it is admitted. The line goes in the env's `values/apps.yaml`:

  ```yaml
  exceptions:
    - lan-demo: lan-port 8080/TCP
  ```

Asking in `app.yaml` alone is not enough, and neither is the line alone. Both must name the same port. A grants file placed in this folder grants nothing. The Service's `targetPort` must be the number `app.yaml` gives as `target`; a named port is refused.

- `app.yaml`: no `network.web` (so no web address), `volumes: []`, the one LAN port.
- `deploy/`: a Deployment (non-root, read-only root filesystem, probes on `/health`) and the LoadBalancer Service.
