# Reference: an env's values

Generated from the JSON Schemas in `schema/` by `python -m jarvis.schema.reference`; edit the schemas, not this page.
Each values file names its schema on its first line, so an editor shows the same descriptions as you type.
Rules that span keys or files are in [schema/CONTRACT.md](../schema/CONTRACT.md), and `jarvis validate` checks them all.

## `values/cluster-vars.yaml`

An env's cluster values: values/cluster-vars.yaml. Rules that span fields (the CIDRs, the App DNS address, LAN ranges, known hosts, the time zone, paths) are checked by jarvis validate.

| Key | Required | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `age_key_file` | no | string | `~/.config/sops/age/keys.txt` | The working age key on the controller: Flux's sops-age is made from it, and the k3s tokens are read with it. SOPS_AGE_KEY_FILE overrides it. |
| `app_dns_cluster_ip` | no; yes when switches.yaml lists app-dns | see Meaning |  | ClusterIP of the App DNS Service, which cluster DNS forwards the zone to. A host address inside service_cidr, not its first address (the Kubernetes API) or its tenth (cluster DNS). |
| `apps_git_repo_url` | no; yes when switches.yaml lists an app | string |  | The hosted apps repository, https, read with no credential. Anyone's: the example apps beside core, or your own. |
| `backup_check_schedule` | no | string | `30 6 * * 3` | Five-field cron for the weekly restic repository check (CronJob platform-backups/restic-check), in time_zone. The check takes no lock, so it never blocks a backup; a prune at the same time can fail one run, and the Job retries. |
| `backup_path` | yes | string |  | Absolute path on the backup node: etcd snapshots under backup_path/etcd-snapshots, and one restic repository per PVC under backup_path/restic/ns-&lt;namespace&gt;/pvc-&lt;pvc&gt;. |
| `backup_retention_daily` | yes | integer |  | Daily snapshots restic keeps, per PVC. |
| `backup_retention_monthly` | yes | integer |  | Monthly snapshots restic keeps, per PVC. |
| `backup_retention_weekly` | yes | integer |  | Weekly snapshots restic keeps, per PVC. |
| `backup_schedule` | yes | string |  | Five-field cron for each PVC's backup (its VolSync ReplicationSource), in time_zone. |
| `chrony_nts` | no | see Meaning | `true` | Whether chrony asks the pools for NTS. false for a LAN or router time source that does not speak it. Host-only (Ansible). |
| `core_git_ref` | yes | string |  | The core release the env runs: refs/tags/&lt;tag&gt;. Every jarvis command runs that release, and refuses a branch ref. |
| `core_git_repo_url` | yes | string |  | The public core repository, Flux's core source. https, read with no credential. |
| `dns_suffix` | yes | string |  | The env's internal DNS zone: services are &lt;name&gt;.&lt;dns_suffix&gt;, hosted apps &lt;app&gt;.apps.&lt;dns_suffix&gt;. A lowercase DNS name. |
| `flux_git_ref` | yes | string |  | The root source's Git ref, e.g. refs/heads/main. |
| `flux_git_repo_url` | yes | string |  | The env repository, Flux's root source. https:// with no userinfo, query or fragment, or ssh://&lt;user&gt;@&lt;host&gt;/&lt;owner&gt;/&lt;repo&gt;, read with the sealed deploy key. |
| `flux_known_hosts` | no; yes for an ssh:// root not on github.com | see Meaning |  | SSH known_hosts lines Flux trusts for an ssh:// root; the root's host must be among them. Unset, GitHub's published key. Host-only (Ansible). |
| `jarvis_env` | yes | string |  | The env's name: its folder under envs/, the /etc/jarvis/env marker on its nodes, and the name its guards check. |
| `k3s_kubelet_eviction_hard` | yes | string |  | kubelet eviction-hard. Its memory.available is raised to node_memory_reserve_percent when that is larger. A host may override it. |
| `k3s_kubelet_kube_reserved` | yes | string |  | kubelet kube-reserved, without the kube-reserved= prefix. A host may override it. |
| `k3s_kubelet_system_reserved` | yes | string |  | kubelet system-reserved, without the system-reserved= prefix. A host may override it. |
| `k3s_sha256` | no; yes with k3s_version | string |  | SHA-256 of that release's amd64 k3s binary, checked on install. |
| `k3s_version` | no; yes with k3s_sha256 | string |  | A k3s release in place of core's (v&lt;major&gt;.&lt;minor&gt;.&lt;patch&gt;+k3s&lt;n&gt;). Set with k3s_sha256, or neither. The host binary changes only by a pull request. |
| `lan_cidrs` | no; yes when apps.yaml grants lan-all or lan-target | see Meaning |  | The LAN's ranges: IPv4 CIDRs inside RFC 1918, clear of pod_cidr and service_cidr. Hosted apps' internet egress excludes them; lan-all opens them and a lan-target range sits inside them. Not published to the cluster; the app wiring render carries it. |
| `local_path_data` | yes | string |  | Where the k3s local-path provisioner keeps volumes: a subdirectory of /var/lib/jarvis, not the mount itself. |
| `node_memory_reserve_percent` | no | integer | `10` | The share of every node's memory no pod is scheduled into (0 to 50): the kubelet's memory.available hard eviction threshold. Under it, pods over their requests are evicted first, by priority, before the kernel's OOM killer acts. 0 leaves eviction-hard as written. Host-only (Ansible). |
| `ntp_pools` | yes | see Meaning |  | Chrony pool hostnames for the nodes, one pool line each (with nts while chrony_nts is on). |
| `pod_cidr` | yes | string |  | The pod network, set once per env. |
| `service_cidr` | yes | string |  | The service network, set once per env. |
| `ssh_user` | no | string | `ubuntu` | The user Ansible logs in to the nodes as; tasks run as root through sudo. jarvis init writes the user who ran it. A host's own ssh_user overrides it. |
| `time_zone` | yes | string |  | IANA time zone for the nodes and for every schedule (VolSync, the restic check). Unknown names fail. |

## `values/switches.yaml`

What an env turns on, by name only. jarvis validate also checks each add-on and app name against core and the apps repository.

| Key | Required | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `addons` | no | array |  | Add-ons this env turns on, by folder name under core's addons/. At most one per slot. |
| `apps` | no | array |  | Hosted apps this env runs, by folder name in the apps repository. Listing an app turns it on and grants it nothing; needs the app-hosting feature. |
| `features` | no | array of `app-dns`, `app-hosting`, `apps-backup-server`, `offsite-backup-copy` |  | Core features this env turns on, by name. Off until listed. |

## `values/apps.yaml`

What an env decides for its hosted apps. jarvis validate also checks each grant, selection and pin against the app list and the apps repository.

| Key | Required | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `backups` | no | object |  | Which listed apps register a backup credential. Without this section, none does. |
| `exceptions` | no | array |  | Grants, one per line as &lt;app&gt;: &lt;grant&gt;, for a listed app whose app file asks for the same item: privileged, root, host-network, host-path, hostname &lt;name&gt;, lan-port &lt;port&gt;/&lt;TCP\|UDP&gt;, lan-all, lan-target &lt;name&gt; &lt;cidr&gt; &lt;port&gt;/&lt;TCP\|UDP&gt;, no-uptime-check. |
| `versions` | no | object |  | Each listed app's release: &lt;app&gt;: v&lt;x.y.z&gt;, the apps repository's tag &lt;app&gt;/v&lt;x.y.z&gt;. Every listed app needs one. |

## `values/apps.yaml: backups`

| Key | Required | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `exclude` | no | array | `[]` | With mode all_except: the apps that do not. |
| `mode` | yes | `selected` or `all_except` |  | selected: only the apps on selected. all_except: every backup-eligible listed app except those on exclude. |
| `overrides` | no | object |  | Per app: backup_schedule and backup_retention_daily, _weekly and _monthly for its volumes, in place of cluster-vars.yaml's. |
| `selected` | no | array | `[]` | With mode selected: the apps that back up. Each must declare a backup: true volume in its app file. |

## `inventory/hosts.yml: each host under all.hosts`

| Key | Required | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `data_disk` | no | `true` |  | true when a filesystem of its own is mounted at /var/lib/jarvis; omit it otherwise. node-disk checks the mount before any change. |
| `k3s_kubelet_eviction_hard` | no | string |  | This host's kubelet eviction-hard, in place of the env's. The env's node memory reserve still applies. |
| `k3s_kubelet_kube_reserved` | no | string |  | This host's kubelet kube-reserved, in place of the env's. |
| `k3s_kubelet_system_reserved` | no | string |  | This host's kubelet system-reserved, in place of the env's. |
| `node_roles` | yes | array of `backup`, `control_plane`, `edge`, `gpu`, `workers` |  | The host's roles. Across hosts: exactly one control_plane, exactly one backup, at least one edge. A host may hold several, and no role implies another. |
| `ssh_user` | no | string |  | The user Ansible logs in to this host as, in place of the env's ssh_user. |
