# lan-demo

An echo server (`traefik/whoami`, pinned by digest) reachable on the home network on TCP 8080, with no web address.

**Proves:** a risky item needs the env's exception line. `app.yaml` asks for one home-network port (`ports: [{port: 8080, protocol: TCP, lan: true}]`), and `deploy/` ships a `LoadBalancer` Service for it with `allocateLoadBalancerNodePorts: false`.

- Without the line, the cluster refuses the Service. Jarvis's `app-grants` admission policy says: *a LoadBalancer exposes only ports the env grants as '<app>: lan-port <port>/<protocol>'*.
- With the line, it is admitted. The line goes in the env's `values/apps.yaml`:

  ```yaml
  exceptions:
    - lan-demo: lan-port 8080/TCP
  ```

Asking in `app.yaml` alone is not enough, and neither is the line alone. Both must name the same port. A grants file placed in this folder grants nothing.

- `app.yaml`: `webPort: none`, `volumes: []`, the one lan port.
- `deploy/`: a Deployment (non-root, read-only root filesystem, probes on `/health`) and the LoadBalancer Service.
