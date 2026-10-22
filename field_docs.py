"""Hover help ("ⓘ" tooltips) for every form field, section, table column and selector (no GUI dependency).

Each entry gives: what the field is / what it does, an example, the YAML key path (manifests) or
config key / CLI flag (cluster configs) it maps to, and a one-line explanation per dropdown option.
``apply()`` copies the text onto the Field objects (``Field.description``, ``Field.maps_to``,
``Field.option_help``) so the GUI and tests read it from the field definitions; ``tooltip(field)``
renders the final 2-8 line text.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Shared option explanations
# --------------------------------------------------------------------------- #
PULL = {"IfNotPresent": "use the node's cached image; pull only if missing (default for pinned tags)",
        "Always": "check the registry on every container start (default for :latest)",
        "Never": "only use an image already on the node; fail otherwise"}
PROTO = {"TCP": "default; HTTP, gRPC, databases", "UDP": "datagrams, e.g. DNS (53/UDP), syslog",
         "SCTP": "multi-stream transport; needs kernel + CNI support"}
ACCESS = {"ReadWriteOnce": "RWO: read-write by pods on ONE node (block storage, most CSI drivers)",
          "ReadOnlyMany": "ROX: read-only by many nodes",
          "ReadWriteMany": "RWX: read-write by many nodes (NFS, CephFS, EFS, Azure Files)",
          "ReadWriteOncePod": "RWOP: read-write by a single pod only (K8s 1.29+ GA, CSI only)"}
PROBE = {"": "auto: httpGet when an HTTP path is set, otherwise no probe",
         "httpGet": "GET the path on the port; any 200-399 status = success",
         "tcpSocket": "success if a TCP connection to the port opens",
         "exec": "run the command in the container; exit code 0 = success",
         "grpc": "standard gRPC health-check service on a numeric port (K8s 1.27+)"}
SC = {"": "omit storageClassName: the cluster's default StorageClass is used",
      "standard": "kind / minikube / GKE (pd-standard) default class name",
      "default": "AKS / some clusters' default class name", "gp2": "AWS EBS gp2 (older EKS default)",
      "gp3": "AWS EBS gp3 (needs the EBS CSI driver + a gp3 class)", "local-path": "Rancher local-path (k3s, kind)",
      "longhorn": "Longhorn distributed block storage", "nfs-client": "NFS subdir external provisioner",
      "managed-csi": "Azure Disk CSI", "standard-rwo": "GKE balanced PD (CSI)", "premium-rwo": "GKE SSD PD (CSI)",
      "nutanix-volume": "Nutanix CSI", "hostpath": "Docker Desktop / microk8s hostpath"}
OPERATOR_AFF = {"In": "label value is one of Values", "NotIn": "label value is none of Values",
                "Exists": "label key present (no Values)", "DoesNotExist": "label key absent (no Values)",
                "Gt": "value greater than the single integer in Values", "Lt": "value less than the single integer in Values"}
TOPO = {"kubernetes.io/hostname": "per node", "topology.kubernetes.io/zone": "per availability zone",
        "topology.kubernetes.io/region": "per region"}
REQPREF = {"required": "hard rule (requiredDuringSchedulingIgnoredDuringExecution): pod stays Pending if unmet",
           "preferred": "soft rule (preferredDuringScheduling...): scheduler scores nodes by Weight"}
CNI_OPTS = {"": "platform default", "none": "no CNI: install your own (Cilium, Calico...) afterwards"}
PROXY = {"": "platform default (iptables)", "iptables": "classic iptables rules (default on most platforms)",
         "ipvs": "IPVS load balancing; scales to many Services (deprecated upstream in favour of nftables)",
         "nftables": "nftables backend (GA in K8s 1.33; needs a 5.13+ kernel)",
         "none": "don't deploy kube-proxy (e.g. Cilium kube-proxy replacement)"}

# path placeholders: <pod> = pod spec, <ctr> = main container, <wl> = workload spec
POD_PATH = {"CronJob": "spec.jobTemplate.spec.template.spec", "_": "spec.template.spec"}
WL_PATH = {"CronJob": "spec.jobTemplate.spec", "_": "spec"}

# --------------------------------------------------------------------------- #
# Manifests: key -> (description, yaml path, option help)
# --------------------------------------------------------------------------- #
M = {
    "name": ("The object's name, unique per kind within its namespace. Other objects refer to it by this name "
             "(e.g. a Service backend, a PVC claimName). Example: web-frontend", "metadata.name", {}),
    "namespace": ("The namespace the object is created in. Namespaces scope names, RBAC and quotas; blank leaves "
                  "it out so `kubectl apply -n <ns>` / your current context decides. Example: shop", "metadata.namespace", {}),
    "labels": ("Key/value tags used to select and group objects (Services, selectors, `kubectl get -l`). "
               "Example: app=web, tier=frontend", "metadata.labels", {}),
    "annotations": ("Free-form key/value metadata for tools (ingress controllers, Prometheus, owners); not used "
                    "for selection. Example: prometheus.io/scrape=true", "metadata.annotations", {}),
    "replicas": ("How many identical pods the controller keeps running; scale up/down by changing it. "
                 "Example: 3", "spec.replicas", {}),
    "selector": ("Labels that identify the pods this controller owns. Must match the pod template labels and "
                 "can't be changed after creation. Example: app=web", "spec.selector.matchLabels", {}),
    "strategy": ("How pods are replaced when the pod template changes.", "spec.strategy.type",
                 {"RollingUpdate": "replace pods gradually (maxSurge / maxUnavailable) - zero downtime",
                  "Recreate": "delete all old pods, then create new ones (needed for RWO volumes)"}),
    "max_surge": ("During a rolling update, how many extra pods may be created above `replicas`. Number or "
                  "percentage. Example: 1 or 25%", "spec.strategy.rollingUpdate.maxSurge", {}),
    "max_unavailable": ("During a rolling update, how many pods may be unavailable at once. Number or percentage. "
                        "Example: 0 (never drop below replicas)", "spec.strategy.rollingUpdate.maxUnavailable", {}),
    "revision_history": ("How many old ReplicaSets to keep for `kubectl rollout undo`. Example: 10 (the default)",
                         "spec.revisionHistoryLimit", {}),
    "container_name": ("Name of the main container (used by `kubectl logs -c` / `exec -c`). Blank = the resource "
                       "name. Example: app", "<pod>.containers[0].name", {}),
    "image": ("Container image to run: [registry/]repository[:tag][@sha256:digest]. Pin a tag (or digest) "
              "instead of :latest for repeatable rollouts. Example: nginx:1.30.6", "<pod>.containers[0].image", {}),
    "image_pull_policy": ("When the kubelet pulls the image from the registry.", "<pod>.containers[0].imagePullPolicy", PULL),
    "restart_policy": ("What the kubelet does when a container exits. Deployments/StatefulSets/DaemonSets only "
                       "allow Always; Jobs use OnFailure or Never.", "<pod>.restartPolicy",
                       {"Always": "restart whenever it exits (long-running services)",
                        "OnFailure": "restart in place only on a non-zero exit (Jobs)",
                        "Never": "don't restart; a failed pod is replaced by the Job controller"}),
    "ports": ("Ports the container listens on, one per line as [name:]port[/PROTO]. Named ports can be used as "
              "Service targetPort and probe port. Example: http:8080", "<pod>.containers[0].ports", {}),
    "port_protocol": ("Protocol used for ports that don't specify /TCP, /UDP or /SCTP.",
                      "<pod>.containers[0].ports[].protocol", PROTO),
    "command": ("Overrides the image ENTRYPOINT. One line is split like a shell; several lines = one argument per "
                "line. Example: sh -c 'echo hi && sleep 3600'", "<pod>.containers[0].command", {}),
    "args": ("Overrides the image CMD (arguments to the entrypoint). $(VAR) expands env vars. "
             "Example: --port=8080", "<pod>.containers[0].args", {}),
    "env": ("Environment variables, one KEY=VALUE per line; KEY=@secret:name/key or KEY=@configmap:name/key "
            "reads a value from a Secret/ConfigMap. Example: DB_PASS=@secret:db/password", "<pod>.containers[0].env", {}),
    "env_from": ("Import every key of a ConfigMap or Secret as env vars, one per line. "
                 "Example: secret:db-credentials", "<pod>.containers[0].envFrom", {}),
    "working_dir": ("Directory the container's process starts in (overrides the image WORKDIR). Example: /app",
                    "<pod>.containers[0].workingDir", {}),
    "cpu_request": ("CPU reserved for scheduling: the node must have this much free. 1 = one core, 100m = 0.1 core. "
                    "Example: 250m", "<pod>.containers[0].resources.requests.cpu", {}),
    "memory_request": ("Memory reserved for scheduling. Use Mi/Gi (binary units). Example: 256Mi",
                       "<pod>.containers[0].resources.requests.memory", {}),
    "cpu_limit": ("Maximum CPU; the container is throttled above it. Blank = no limit. Example: 1",
                  "<pod>.containers[0].resources.limits.cpu", {}),
    "memory_limit": ("Maximum memory; the container is OOM-killed above it. Must be >= the request. Example: 512Mi",
                     "<pod>.containers[0].resources.limits.memory", {}),
    "readiness_type": ("How readiness is checked. Until it passes the pod gets no Service traffic.",
                       "<pod>.containers[0].readinessProbe", PROBE),
    "readiness_path": ("HTTP path for an httpGet readiness probe. Example: /healthz",
                       "<pod>.containers[0].readinessProbe.httpGet.path", {}),
    "readiness_command": ("Command for an exec readiness probe (exit 0 = ready). "
                          "Example: sh -c 'pg_isready -U \"$POSTGRES_USER\"'", "<pod>.containers[0].readinessProbe.exec.command", {}),
    "liveness_type": ("How liveness is checked. When it keeps failing the kubelet restarts the container.",
                      "<pod>.containers[0].livenessProbe", PROBE),
    "liveness_path": ("HTTP path for an httpGet liveness probe. Example: /livez",
                      "<pod>.containers[0].livenessProbe.httpGet.path", {}),
    "liveness_command": ("Command for an exec liveness probe (non-zero exit = unhealthy). "
                         "Example: redis-cli ping", "<pod>.containers[0].livenessProbe.exec.command", {}),
    "probe_port": ("Port the httpGet/tcpSocket/grpc probes connect to: a number or a container port name. "
                   "Blank = the first container port. Example: http", "<pod>.containers[0].*Probe.*.port", {}),
    "readiness_initial_delay": ("Seconds after container start before the first readiness check. Example: 5",
                                "<pod>.containers[0].readinessProbe.initialDelaySeconds", {}),
    "liveness_initial_delay": ("Seconds after start before the first liveness check - give slow apps time so they "
                               "aren't restarted while booting. Example: 30", "<pod>.containers[0].livenessProbe.initialDelaySeconds", {}),
    "probe_period": ("Seconds between probe attempts (both probes). Example: 10",
                     "<pod>.containers[0].*Probe.periodSeconds", {}),
    "probe_timeout": ("Seconds a single probe attempt may take before it counts as failed. Example: 5",
                      "<pod>.containers[0].*Probe.timeoutSeconds", {}),
    "probe_failure_threshold": ("Consecutive failures before the probe is considered failed (not ready / restart). "
                                "Example: 6", "<pod>.containers[0].*Probe.failureThreshold", {}),
    "volumes": ("Pod volumes and where the main container mounts them, one per line as "
                "name:type:source:mountPath[:ro]. Example: cfg:configMap:app-config:/etc/app:ro",
                "<pod>.volumes + <pod>.containers[0].volumeMounts", {}),
    "extra_containers": ("Additional containers in the same pod: init containers (run to completion first, in "
                         "order), native sidecars, regular sidecars or extra app containers. Double-click a row to edit.",
                         "<pod>.initContainers / <pod>.containers", {}),
    "service_account": ("ServiceAccount the pod runs as; decides its Kubernetes API permissions (RBAC) and cloud "
                        "identity (IRSA / Workload Identity). Blank = 'default'. Example: web-sa", "<pod>.serviceAccountName", {}),
    "fs_group": ("Supplementary group added to all containers; mounted volumes are made group-owned and writable "
                 "by it. Example: 1000", "<pod>.securityContext.fsGroup", {}),
    "run_as_user": ("UID all containers' processes run as (overrides the image USER). Example: 1000",
                    "<pod>.securityContext.runAsUser", {}),
    "run_as_group": ("Primary GID for all containers' processes. Example: 1000", "<pod>.securityContext.runAsGroup", {}),
    "run_as_non_root": ("When ticked the kubelet refuses to start a container that would run as UID 0 "
                        "(required by the 'restricted' Pod Security level). Example: tick with runAsUser 1000",
                        "<pod>.securityContext.runAsNonRoot", {}),
    "enable_service_links": ("When ticked (Kubernetes' default) every Service in the namespace is injected as "
                             "<NAME>_SERVICE_HOST / <NAME>_PORT env vars. Untick for images that misread them "
                             "(e.g. Kafka). Example: untick for apache/kafka", "<pod>.enableServiceLinks", {}),
    "node_selector": ("Only schedule on nodes that have all these labels. One key=value per line. "
                      "Example: kubernetes.io/arch=arm64", "<pod>.nodeSelector", {}),
    "image_pull_secrets": ("Secrets (type dockerconfigjson) used to pull from private registries, comma-separated. "
                           "Example: regcred", "<pod>.imagePullSecrets", {}),
    "termination_grace": ("Seconds between SIGTERM and SIGKILL when a pod is deleted, for graceful shutdown. "
                          "Example: 60 (default 30)", "<pod>.terminationGracePeriodSeconds", {}),
    "tolerations": ("Allow the pod onto nodes with matching taints (e.g. dedicated or control-plane nodes). "
                    "Tolerating a taint permits, but doesn't force, scheduling there.", "<pod>.tolerations", {}),
    "tolerations.key": ("Taint key to tolerate; blank with operator Exists tolerates every taint. "
                        "Example: node-role.kubernetes.io/control-plane", "<pod>.tolerations[].key", {}),
    "tolerations.operator": ("How the taint value is matched.", "<pod>.tolerations[].operator",
                             {"Equal": "taint value must equal Value", "Exists": "any value (leave Value blank)"}),
    "tolerations.value": ("Taint value to match when the operator is Equal. Example: gpu", "<pod>.tolerations[].value", {}),
    "tolerations.effect": ("Which taint effect this toleration covers.", "<pod>.tolerations[].effect",
                           {"": "all effects", "NoSchedule": "new pods aren't scheduled on the node",
                            "PreferNoSchedule": "scheduler avoids the node if it can",
                            "NoExecute": "running pods are also evicted (after tolerationSeconds)"}),
    "tolerations.toleration_seconds": ("For NoExecute: how long the pod may stay after the taint appears. "
                                       "Example: 300", "<pod>.tolerations[].tolerationSeconds", {}),
    "node_affinity": ("Rules on node labels deciding where the pod may (required) or should (preferred) run - a "
                      "more expressive nodeSelector.", "<pod>.affinity.nodeAffinity", {}),
    "node_affinity.type": ("Hard or soft rule.", "<pod>.affinity.nodeAffinity.*", REQPREF),
    "node_affinity.key": ("Node label key to test. Example: topology.kubernetes.io/zone",
                          "...matchExpressions[].key", {}),
    "node_affinity.operator": ("How the node label is compared with Values.", "...matchExpressions[].operator", OPERATOR_AFF),
    "node_affinity.values": ("Comma-separated values for In/NotIn (one integer for Gt/Lt). Example: us-west-2a, us-west-2b",
                             "...matchExpressions[].values", {}),
    "node_affinity.weight": ("1-100 score added to nodes matching a preferred rule (higher = stronger). Example: 50",
                             "...preferredDuringSchedulingIgnoredDuringExecution[].weight", {}),
    "node_affinity.term": ("Required rows with the same term # are ANDed into one nodeSelectorTerm; different "
                           "numbers are ORed. Example: 1", "...requiredDuringScheduling...nodeSelectorTerms[]", {}),
    "pod_affinity": ("Schedule near pods that match a label selector (same node / zone), e.g. a cache next to its "
                     "app.", "<pod>.affinity.podAffinity", {}),
    "pod_anti_affinity": ("Keep away from pods matching a label selector - typically spread replicas across nodes "
                          "(app In <name> on kubernetes.io/hostname).", "<pod>.affinity.podAntiAffinity", {}),
    "pod_affinity.type": ("Hard or soft rule.", "<pod>.affinity.podAffinity.*", REQPREF),
    "pod_affinity.key": ("Label key of the OTHER pods to match. Example: app", "...labelSelector.matchExpressions[].key", {}),
    "pod_affinity.operator": ("How the other pods' label is compared.", "...labelSelector.matchExpressions[].operator",
                              {k: v for k, v in OPERATOR_AFF.items() if k not in ("Gt", "Lt")}),
    "pod_affinity.values": ("Comma-separated values for In/NotIn. Example: redis", "...matchExpressions[].values", {}),
    "pod_affinity.topology_key": ("Node label defining 'near': pods count as co-located when nodes share this "
                                  "label value. Example: kubernetes.io/hostname", "...topologyKey", TOPO),
    "pod_affinity.weight": ("1-100 score for preferred rules. Example: 100", "...podAffinityTerm weight", {}),
    "pod_affinity.namespaces": ("Namespaces to look for matching pods in, comma-separated; blank = this pod's "
                                "namespace. Example: cache, web", "...podAffinityTerm.namespaces", {}),
    "topology_spread": ("Spread replicas evenly across nodes/zones: the pod count difference between any two "
                        "domains stays <= maxSkew.", "<pod>.topologySpreadConstraints", {}),
    "topology_spread.max_skew": ("Largest allowed difference in matching pods between two domains. Example: 1",
                                 "<pod>.topologySpreadConstraints[].maxSkew", {}),
    "topology_spread.topology_key": ("Node label that defines a domain. Example: topology.kubernetes.io/zone",
                                     "<pod>.topologySpreadConstraints[].topologyKey", TOPO),
    "topology_spread.when_unsatisfiable": ("What to do when the skew can't be met.",
                                           "<pod>.topologySpreadConstraints[].whenUnsatisfiable",
                                           {"DoNotSchedule": "keep the pod Pending (hard)",
                                            "ScheduleAnyway": "schedule but prefer lower skew (soft)"}),
    "topology_spread.match_labels": ("Which pods are counted, comma-separated key=value; blank = this pod's own "
                                     "labels. Example: app=web", "<pod>.topologySpreadConstraints[].labelSelector", {}),
    "service_name": ("Name of the headless Service (clusterIP: None) that gives each pod a stable DNS name "
                     "<pod>.<service>.<ns>.svc. Create that Service too. Example: postgres-headless", "spec.serviceName", {}),
    "partition": ("Rolling update partition: only pods with ordinal >= this number are updated - for canary/staged "
                  "rollouts. Example: 2", "spec.updateStrategy.rollingUpdate.partition", {}),
    "pod_management": ("Order in which pods are created and deleted.", "spec.podManagementPolicy",
                       {"OrderedReady": "one at a time, pod-0 first, each waits for the previous to be Ready",
                        "Parallel": "create/delete all pods at once (faster; no ordering)"}),
    "vct_name": ("Name of the per-pod PersistentVolumeClaim template; each pod gets <name>-<sts>-<ordinal>. Blank = "
                 "no claim template. Example: data", "spec.volumeClaimTemplates[0].metadata.name", {}),
    "vct_mount": ("Where the main container mounts the per-pod volume. Example: /var/lib/postgresql",
                  "<pod>.containers[0].volumeMounts[].mountPath", {}),
    "vct_size": ("Storage requested for each pod's claim. Example: 10Gi",
                 "spec.volumeClaimTemplates[0].spec.resources.requests.storage", {}),
    "vct_access_mode": ("Access mode of each pod's claim.", "spec.volumeClaimTemplates[0].spec.accessModes", ACCESS),
    "vct_storage_class": ("StorageClass that provisions each pod's volume. Example: gp3",
                          "spec.volumeClaimTemplates[0].spec.storageClassName", SC),
    "host_network": ("Run the pods in the node's network namespace (they see/bind the node's interfaces and ports). "
                     "Used by node agents and CNIs. Example: tick for a node-exporter", "<pod>.hostNetwork", {}),
    "tolerate_all": ("Adds a toleration for every taint so the DaemonSet also runs on control-plane and tainted "
                     "nodes. Example: tick for log/monitoring agents", "<pod>.tolerations: [{operator: Exists}]", {}),
    "backoff_limit": ("Retries before the Job is marked failed. Example: 4 (default 6)", "<wl>.backoffLimit", {}),
    "completions": ("Number of successful pod runs needed for the Job to complete. Example: 5", "<wl>.completions", {}),
    "parallelism": ("Maximum pods running at the same time. Example: 2", "<wl>.parallelism", {}),
    "active_deadline": ("Seconds the Job may run in total before it's terminated and failed. Example: 3600",
                        "<wl>.activeDeadlineSeconds", {}),
    "ttl_after_finished": ("Seconds after the Job finishes before it (and its pods) are deleted automatically. "
                           "Example: 86400", "<wl>.ttlSecondsAfterFinished", {}),
    "schedule": ("When the CronJob runs, in cron syntax: minute hour day-of-month month day-of-week, or a macro. "
                 "Example: 30 2 * * * (02:30 daily)", "spec.schedule", {}),
    "time_zone": ("IANA time zone the schedule is interpreted in; blank = the kube-controller-manager's zone "
                  "(usually UTC). Example: America/Los_Angeles", "spec.timeZone", {}),
    "concurrency_policy": ("What happens when a run is due while the previous one is still running.",
                           "spec.concurrencyPolicy",
                           {"Allow": "run concurrently", "Forbid": "skip the new run",
                            "Replace": "cancel the running Job and start the new one"}),
    "starting_deadline": ("If a run is missed (controller down), it's still started up to this many seconds late. "
                          "Example: 300", "spec.startingDeadlineSeconds", {}),
    "successful_history": ("Finished Jobs to keep for inspection. Example: 3", "spec.successfulJobsHistoryLimit", {}),
    "failed_history": ("Failed Jobs to keep for debugging. Example: 1", "spec.failedJobsHistoryLimit", {}),
    "suspend": ("Pause the CronJob: no new runs are started until unticked. Example: tick during maintenance",
                "spec.suspend", {}),
    "service_type": ("How the Service is exposed.", "spec.type",
                     {"ClusterIP": "virtual IP reachable only inside the cluster (default)",
                      "NodePort": "ClusterIP + a port (30000-32767) open on every node",
                      "LoadBalancer": "NodePort + an external load balancer from the cloud / MetalLB",
                      "ExternalName": "DNS CNAME to an external host; no proxying or selector"}),
    "Service.selector": ("Pods whose labels match ALL of these receive the Service's traffic. Must match the pod "
                         "template labels of your workload. Example: app=web", "spec.selector", {}),
    "port_name": ("Name of the main port (required when the Service has several; Ingress can reference it). "
                  "Example: http", "spec.ports[0].name", {}),
    "port": ("Port the Service listens on (clients connect to <service>:<port>). Example: 80", "spec.ports[0].port", {}),
    "target_port": ("Container port traffic is forwarded to: a number or a container port name. "
                    "Example: http or 8080", "spec.ports[0].targetPort", {}),
    "node_port": ("Fixed port opened on every node for NodePort/LoadBalancer (30000-32767); blank = auto-assigned. "
                  "Example: 30080", "spec.ports[0].nodePort", {}),
    "protocol": ("Transport protocol of the main port.", "spec.ports[0].protocol", PROTO),
    "extra_ports": ("More Service ports, one per line as name:port:targetPort[:nodePort][/PROTO]. "
                    "Example: metrics:9113:9113", "spec.ports[1..]", {}),
    "cluster_ip": ("Fixed virtual IP, or None for a headless Service (DNS returns the pod IPs directly - used by "
                   "StatefulSets).", "spec.clusterIP",
                   {"": "auto-assign a cluster IP (normal Service)", "None": "headless: no virtual IP, per-pod DNS records"}),
    "external_name": ("For type ExternalName: the DNS name this Service aliases (CNAME). Example: db.example.com",
                      "spec.externalName", {}),
    "external_traffic_policy": ("For NodePort/LoadBalancer: whether external traffic may hop to pods on other "
                                "nodes.", "spec.externalTrafficPolicy",
                                {"": "omit (Kubernetes default: Cluster)",
                                 "Cluster": "spread to all pods; client source IP is SNATed",
                                 "Local": "only pods on the receiving node; preserves the client IP"}),
    "session_affinity": ("Stickiness of client connections to one pod.", "spec.sessionAffinity",
                         {"None": "each connection may go to any pod (default)",
                          "ClientIP": "same client IP -> same pod (3h timeout by default)"}),
    "publish_not_ready": ("Publish DNS records for pods even before they're Ready - needed when peers must find each "
                          "other to become ready (Cassandra, etcd). Example: tick on a headless Service",
                          "spec.publishNotReadyAddresses", {}),
    "ingress_class": ("Which ingress controller implements this Ingress (must match an IngressClass in the cluster). "
                      "Example: nginx", "spec.ingressClassName",
                      {"": "omit: the cluster's default IngressClass", "nginx": "ingress-nginx / NGINX Ingress",
                       "traefik": "Traefik (k3s default)", "haproxy": "HAProxy Ingress", "alb": "AWS Load Balancer Controller",
                       "gce": "GKE external HTTP(S) LB", "kong": "Kong", "istio": "Istio ingress gateway",
                       "cilium": "Cilium ingress"}),
    "host": ("Host name this rule answers for (virtual hosting); blank = every host. Example: shop.example.com",
             "spec.rules[0].host", {}),
    "path": ("URL path routed to the backend. Example: / or /api", "spec.rules[0].http.paths[0].path", {}),
    "path_type": ("How the path is matched.", "spec.rules[0].http.paths[0].pathType",
                  {"Prefix": "matches the path and anything below it, element-wise (/api matches /api/v1)",
                   "Exact": "matches exactly this path only",
                   "ImplementationSpecific": "matching left to the ingress controller"}),
    "backend_service": ("Service that receives the traffic (same namespace). Example: web",
                        "spec.rules[0].http.paths[0].backend.service.name", {}),
    "backend_port": ("Port of that Service: number or Service port name. Example: 80 or http",
                     "...backend.service.port.number / .name", {}),
    "extra_rules": ("More routes, one per line: host path pathType service port ('-' = the main Host). "
                    "Example: - /api Prefix api 8080", "spec.rules[]", {}),
    "tls_secret": ("Secret of type kubernetes.io/tls holding the certificate and key for HTTPS; blank = no TLS. "
                   "Example: shop-tls", "spec.tls[0].secretName", {}),
    "tls_hosts": ("Host names covered by that certificate, comma-separated; blank = Host. "
                  "Example: shop.example.com, www.shop.example.com", "spec.tls[0].hosts", {}),
    "ConfigMap.data": ("Plain configuration values, one KEY=VALUE per line (\\n inside a value becomes a newline). "
                       "Example: LOG_LEVEL=info", "data", {}),
    "Secret.data": ("Secret values typed as plaintext KEY=VALUE per line; they're base64-encoded into data: for you. "
                    "Example: password=s3cr3t", "data (base64)", {}),
    "file_key": ("Key of an optional multi-line entry (usually a file name, mounted as a file when the "
                 "ConfigMap/Secret is a volume). Example: nginx.conf", "data.<file key>", {}),
    "file_content": ("The multi-line content stored under File key, kept verbatim. Example: a whole nginx.conf",
                     "data.<file key>", {}),
    "immutable": ("Lock the data: updates are rejected (delete and recreate instead). Protects against accidental "
                  "changes and reduces API server load. Example: tick for release configs", "immutable", {}),
    "secret_type": ("Secret type; built-in types require specific keys.", "type",
                    {"Opaque": "arbitrary key/values (default)",
                     "kubernetes.io/dockerconfigjson": "registry credentials (.dockerconfigjson) for imagePullSecrets",
                     "kubernetes.io/tls": "tls.crt + tls.key for Ingress / webhooks",
                     "kubernetes.io/basic-auth": "username + password keys",
                     "kubernetes.io/ssh-auth": "ssh-privatekey key",
                     "kubernetes.io/service-account-token": "long-lived token for the named ServiceAccount"}),
    "file_key2": ("Key of a second multi-line entry (e.g. the private key next to a certificate). Example: tls.key",
                  "data.<file key 2>", {}),
    "file_content2": ("Multi-line content for File key 2. Example: a PEM block such as tls.key", "data.<file key 2>", {}),
    "already_base64": ("Tick if the values you typed are already base64 so they aren't encoded twice. "
                       "Example: tick when pasting from `kubectl get secret -o yaml`", "data", {}),
    "registry_server": ("Registry host for the generated .dockerconfigjson. Example: ghcr.io",
                        "data[.dockerconfigjson].auths.<server>", {}),
    "registry_user": ("User name for the registry login (robot/service account). Example: my-bot", "data[.dockerconfigjson].auths.<server>.username", {}),
    "registry_password": ("Registry password or access token. Example: a GitHub PAT with read:packages",
                          "data[.dockerconfigjson].auths.<server>.password", {}),
    "registry_email": ("Optional e-mail stored with the registry credentials. Example: ci@example.com",
                       "data[.dockerconfigjson].auths.<server>.email", {}),
    "sa_token_account": ("ServiceAccount the token is issued for (the token controller fills in token/ca.crt). "
                         "Example: ci-deployer", "metadata.annotations[kubernetes.io/service-account.name]", {}),
    "size": ("Storage requested from the provisioner. Example: 20Gi", "spec.resources.requests.storage", {}),
    "access_mode": ("How the volume may be mounted.", "spec.accessModes", ACCESS),
    "storage_class": ("StorageClass that dynamically provisions the volume; blank = the cluster default. "
                      "Example: gp3", "spec.storageClassName", SC),
    "volume_mode": ("Whether the volume is formatted with a filesystem or handed over as a raw block device.",
                    "spec.volumeMode", {"Filesystem": "mounted as a directory (default)",
                                        "Block": "raw block device (databases that manage the disk themselves)"}),
    "volume_name": ("Bind to this specific pre-created PersistentVolume instead of provisioning a new one. "
                    "Example: pv-nfs-01", "spec.volumeName", {}),
    "pss_enforce": ("Pod Security Admission level enforced in the namespace: pods violating it are rejected.",
                    "metadata.labels[pod-security.kubernetes.io/enforce]",
                    {"": "no label (cluster default, usually privileged)",
                     "privileged": "no restrictions",
                     "baseline": "blocks known privilege escalations (hostNetwork, privileged...)",
                     "restricted": "hardened: non-root, no privilege escalation, seccomp, dropped caps"}),
    # extra container columns that differ from the main-container fields
    "extra_containers.role": ("Where this container goes in the pod.", "<pod>.initContainers / <pod>.containers",
                              {"sidecar (regular container)": "extra entry in containers: next to the main one",
                               "sidecar (native)": "initContainer with restartPolicy: Always - starts before and "
                                                   "outlives the app (K8s 1.29+)",
                               "init": "runs to completion before the app starts (list order = run order)",
                               "main": "an additional application container"}),
    "extra_containers.name": ("Container name, unique within the pod (used by kubectl logs/exec -c). Example: proxy",
                              "<pod>.containers[] / initContainers[].name", {}),
    "extra_containers.volume_mounts": ("Volumes this container mounts, one volume:/path[:ro] per line; the volume "
                                       "must exist in Volumes (or be the claim template). Example: logs:/var/log/app:ro",
                                       "...volumeMounts", {}),
}

STS_DS_STRATEGY = {"RollingUpdate": "replace pods automatically one at a time when the template changes",
                   "OnDelete": "only replace a pod when you delete it yourself (manual control)"}
M.update({
    "StatefulSet.strategy": ("How pods are updated when the pod template changes.", "spec.updateStrategy.type",
                             STS_DS_STRATEGY),
    "DaemonSet.strategy": ("How the per-node pods are updated when the pod template changes.",
                           "spec.updateStrategy.type", STS_DS_STRATEGY),
    "DaemonSet.max_unavailable": ("During a rolling update, how many nodes may be without a running pod at once. "
                                  "Example: 1 or 10%", "spec.updateStrategy.rollingUpdate.maxUnavailable", {}),
    "Namespace.name": ("Name of the namespace to create (DNS-1123 label). Objects are placed in it with "
                       "metadata.namespace. Example: shop", "metadata.name", {}),
    "Namespace.labels": ("Labels on the namespace; used by NetworkPolicy namespaceSelectors, Pod Security "
                         "Admission and tooling. Example: team=payments", "metadata.labels", {}),
})

# --------------------------------------------------------------------------- #
# Section headers ("<type>:<title>" first, then "<title>")
# --------------------------------------------------------------------------- #
SECTIONS = {
    "Metadata": "Identity of the object: name, namespace, labels and annotations (metadata:).",
    "Deployment spec": "Replica count, pod selector and rollout strategy of the Deployment (spec:).",
    "StatefulSet spec": "StatefulSet settings: replicas, headless serviceName, update strategy, pod ordering and "
                        "the per-pod volumeClaimTemplate (spec:).",
    "DaemonSet spec": "DaemonSet settings: one pod per (matching) node; selector, update strategy, hostNetwork.",
    "Job spec": "Run-to-completion settings: retries, completions, parallelism and deadlines.",
    "CronJob spec": "When the Job runs (cron schedule, time zone) and how runs overlap / are kept.",
    "Main container": "The primary container: image, ports, command/args and environment "
                      "(spec.template.spec.containers[0]).",
    "Resources": "CPU/memory requests (used for scheduling) and limits (enforced at runtime) of the main container.",
    "Probes": "Health checks: readiness gates Service traffic, liveness restarts a hung container.",
    "Volumes": "Volumes the pod can use (PVC, ConfigMap, Secret, emptyDir, hostPath) and the main container's mounts.",
    "Init & sidecar containers": "Extra containers in the same pod: init containers, native or regular sidecars.",
    "Scheduling": "Who the pod runs as (ServiceAccount, securityContext) and where it may run (nodeSelector).",
    "Tolerations": "Let pods schedule onto tainted nodes (dedicated, GPU, control-plane...).",
    "Node affinity": "Required/preferred rules on node labels (zone, instance type, arch...).",
    "Pod affinity / anti-affinity": "Place pods near (affinity) or away from (anti-affinity) other pods.",
    "Topology spread": "Spread replicas evenly across nodes, zones or regions.",
    "Service spec": "How the Service is exposed (type), which pods it selects and the port mapping.",
    "Ingress spec": "HTTP(S) routing from host/path to a Service, the ingress class and TLS.",
    "Data": "The key/value data stored in the object (single values plus an optional multi-line file).",
    "Secret data": "Secret type and extra multi-line keys (certificates / keys); values are base64-encoded.",
    "dockerconfigjson helper": "Fills .dockerconfigjson from registry, user and password "
                               "(type kubernetes.io/dockerconfigjson).",
    "service-account-token": "For type kubernetes.io/service-account-token: which ServiceAccount the token is for.",
    "PVC spec": "Size, access mode and StorageClass of the PersistentVolumeClaim.",
    "Pod Security (optional)": "Pod Security Admission labels that restrict what pods may do in this namespace.",
    # cluster config
    "kind:Cluster": "kind cluster name and feature gates (kind-config.yaml top level).",
    "kind:Nodes": "The node containers kind creates: control-plane / worker roles, image and labels.",
    "kind:extraPortMappings (first control-plane)": "Forward host ports into the first control-plane container "
                                                    "(e.g. 80/443 for an ingress controller).",
    "kind:Networking": "Pod/Service subnets, IP family, API server address/port, CNI and kube-proxy mode.",
    "minikube:Output": "Which files to generate: minikube config.json defaults and/or a start script.",
    "minikube:Machine": "The VM/container minikube creates: driver, CPUs, memory, disk, node count, HA.",
    "minikube:Kubernetes": "Kubernetes version, container runtime, CNI, feature gates and component flags.",
    "minikube:Addons": "minikube addons enabled at start (ingress, metrics-server, dashboard...).",
    "minikube:Networking & registries": "Port publishing, static IP / subnet, host mounts and registry settings.",
    "minikube:Start options": "How `minikube start` behaves: what to wait for, kubeconfig handling.",
    "k3s / RKE2:Distribution": "k3s or RKE2, server or agent, and whether this server initialises embedded etcd.",
    "k3s / RKE2:Join / identity": "Cluster token, server URL to join, and this node's name / IPs.",
    "k3s / RKE2:Server networking": "API cert SANs, pod/service CIDRs, cluster DNS, CNI and ingress controller.",
    "k3s / RKE2:Disable components (k3s)": "Packaged k3s components to skip (`disable:` list).",
    "k3s / RKE2:Disable components (RKE2)": "Packaged RKE2 charts to skip (`disable:` list).",
    "k3s / RKE2:Node labels / taints": "Labels and taints registered on this node at join time.",
    "k0s:Output": "Which files to generate (k0sctl.yaml and/or k0s.yaml), cluster name and k0s version.",
    "k0s:Hosts (k0sctl)": "The machines k0sctl installs over SSH and their roles.",
    "k0s:API & storage": "API server address/port/SANs and the control-plane datastore (etcd or kine).",
    "k0s:Network": "CNI provider, pod/service CIDRs, kube-proxy and node-local load balancing.",
    "k0s:Telemetry & options": "Anonymous telemetry and k0sctl wait behaviour.",
    "MicroK8s:Install": "Launch configuration version and snap channel.",
    "MicroK8s:Addons": "Addons to enable / disable when the node starts.",
    "MicroK8s:Kubernetes arguments": "Extra flags for the Kubernetes services and API cert SANs.",
    "MicroK8s:Registries & cluster": "Registry mirrors, cluster join token and joining an existing cluster.",
    "kubeadm:Output": "Generate the init config (first control plane) or a join config (extra nodes).",
    "kubeadm:InitConfiguration": "Node-local settings for `kubeadm init`: API advertise address, node "
                                 "registration, bootstrap token.",
    "kubeadm:ClusterConfiguration": "Cluster-wide settings: version, endpoint, networking, cert SANs, etcd.",
    "kubeadm:KubeletConfiguration": "Optional KubeletConfiguration document (cgroup driver).",
    "kubeadm:KubeProxyConfiguration": "Optional KubeProxyConfiguration document (proxy mode).",
    "kubeadm:JoinConfiguration": "Settings for `kubeadm join`: endpoint, token, CA pinning, control-plane join.",
    "Talos:Cluster": "`talosctl gen config` inputs: cluster name, endpoint, versions, install disk/image, mirrors.",
    "Talos:Network (patch)": "Machine-config patch: pod/service subnets, CNI, kube-proxy, DNS and NTP.",
    "Talos:Nodes (patch)": "Machine-config patch for nodes: scheduling on control planes, labels, taints, kubelet.",
    "Kubespray:Cluster": "Inventory name, Kubernetes version, runtime, etcd type and the hosts with their roles.",
    "Kubespray:Networking": "Network plugin, kube-proxy, service/pod ranges, DNS and API port (group_vars).",
    "Kubespray:Options & add-ons": "Kubeconfig copy-back, secret encryption, cert renewal and add-ons.",
    "Cluster API:Cluster": "Cluster API (CAPD) cluster: name, namespace, version and machine counts.",
    "Cluster API:Networking": "clusterNetwork pods/services ranges, service domain and API cert SANs.",
    "Cluster API:Machines": "Node image and kubelet settings for the DockerMachineTemplates.",
    "OpenShift:Cluster": "install-config.yaml basics: name, base domain, platform, replicas, FIPS, publish.",
    "OpenShift:Credentials": "Red Hat pull secret and the SSH public key put on the nodes.",
    "OpenShift:Networking": "OVN-Kubernetes cluster/service/machine networks.",
    "OpenShift:Platform: aws / azure / gcp": "Cloud-specific install-config fields (region, instance type, ...).",
    "OpenShift:Platform: vsphere": "vCenter connection and placement for platform vsphere.",
    "OpenShift:Platform: baremetal / vsphere VIPs": "API / ingress VIPs, provisioning network and bare-metal hosts.",
    "EKS:Cluster": "eksctl ClusterConfig metadata: name, region, version, tags, Auto Mode.",
    "EKS:Managed node groups": "EC2 managed node groups: instance types, sizes, disks, AMI, spot.",
    "EKS:VPC & networking": "VPC CIDR, NAT, AZs, API endpoint access and IP family.",
    "EKS:Access, IAM & add-ons": "Authentication mode, OIDC/IRSA, EKS add-ons and control-plane logging.",
    "GKE:Cluster": "`gcloud container clusters create` basics: mode, name, project, location, channel.",
    "GKE:Default node pool (Standard)": "Machine type, size, autoscaling, disks and labels of the default pool.",
    "GKE:Networking": "VPC network/subnet, pod/service ranges, Dataplane V2 and private nodes.",
    "GKE:Security & add-ons": "Workload Identity, Shielded nodes and GKE add-ons.",
    "AKS:Cluster": "`az aks create` basics: name, resource group, location, version, tier, tags.",
    "AKS:System node pool": "VM size, count, zones, OS and autoscaling of the system pool.",
    "AKS:Networking": "Network plugin/mode, dataplane, policy, CIDRs, outbound type, private API.",
    "AKS:Identity, upgrades & add-ons": "OIDC, Workload Identity, Entra ID, upgrade channels, add-ons, SSH key.",
    "kubeconfig:Cluster": "The clusters: entry: API server URL and how its certificate is trusted.",
    "kubeconfig:User / auth": "The users: entry: credentials kubectl sends (token, client cert, basic, exec).",
    "kubeconfig:Exec plugin": "Credential plugin kubectl runs to get a token (aws, gke-gcloud-auth-plugin, kubelogin).",
    "kubeconfig:Context": "The contexts: entry tying cluster + user + default namespace together.",
}

# --------------------------------------------------------------------------- #
# Top selectors and template parameters
# --------------------------------------------------------------------------- #
TOP = {
    "mode": ("Mode", "Switch between Manifests (Kubernetes resources: Deployments, Services...) and Cluster "
                     "config (files to create or connect to a cluster: kind, kubeadm, k3s, EKS, kubeconfig...). "
                     "Values you entered are kept per type when you switch."),
    "kind": ("Resource type / Config type", "Pick which kind (Manifests) or platform (Cluster config) to edit; "
                                            "only that form is shown. Each keeps its own values until you Reset it. "
                                            "Group headers (── Local ──) jump to the group's first entry. "
                                            f"Shortcut: Ctrl/Cmd+K."),
    "template": ("Template", "Apply a ready-made application (PostgreSQL, nginx, Kafka...) as a coherent set of "
                             "kinds: workload + Services + Secret + ConfigMap + PVC/Ingress. Parameters below re-render "
                             "the whole set; 'Save template set…' writes every file plus all-in-one.yaml."),
}
TEMPLATE_PARAMS = {
    "name": ("Prefix for every object in the set: the workload and Service are <name>, others <name>-secret, "
             "<name>-config, <name>-headless... Example: orders-db", "metadata.name of every object"),
    "namespace": ("Namespace for every object; anything other than 'default' also adds a Namespace object at the "
                  "top of the set. Example: shop", "metadata.namespace (+ Namespace object)"),
    "replicas": ("Number of pods for apps that scale horizontally (web servers, caches). Example: 3",
                 "spec.replicas"),
    "storage_size": ("Persistent storage for the app's data: the volumeClaimTemplate (StatefulSets) or PVC size. "
                     "Example: 20Gi", "volumeClaimTemplates[].spec.resources.requests.storage / PVC spec.resources"),
    "storage_class": ("StorageClass that provisions the data volume; blank = the cluster default. Example: gp3",
                      "storageClassName"),
    "ingress": ("Add an Ingress that routes Ingress host to the app's web port (needs an ingress controller in "
                "the cluster). Example: tick for a public Grafana", "Ingress object"),
    "host": ("Host name the Ingress answers for (only used when 'Expose via Ingress' is ticked). "
             "Example: grafana.example.com", "Ingress spec.rules[0].host"),
    "ingress_class": ("IngressClass (controller) that serves the Ingress; blank = cluster default. Example: nginx",
                      "Ingress spec.ingressClassName"),
}
TEMPLATE_PARAM_OPTIONS = {"ingress_class": M["ingress_class"][2]}

# --------------------------------------------------------------------------- #
# Cluster config: type -> key -> (description, maps to, option help); "" maps_to = MAP_TEMPLATE
# --------------------------------------------------------------------------- #
MAP_TEMPLATE = {
    "minikube": "minikube start --{flag}  /  config.json \"{flag}\"",
    "GKE": "gcloud container clusters create {label}",
    "AKS": "az aks create {label}",
    "k3s / RKE2": "config.yaml: {label}",
    "Kubespray": "inventory group_vars: {label}",
    "MicroK8s": "launch config: {label}",
    "EKS": "eksctl ClusterConfig: {label}",
    "OpenShift": "install-config.yaml: {label}",
    "kind": "kind-config.yaml: {label}",
    "k0s": "k0s ClusterConfig spec.{label}",
    "Talos": "talosctl gen config {label}",
    "Cluster API": "Cluster / KubeadmControlPlane: {label}",
    "kubeadm": "kubeadm config: {label}",
    "kubeconfig": "kubeconfig: {label}",
}
YESNO = {}

C = {
"kind": {
    "name": ("Name of the kind cluster; node containers are <name>-control-plane... and the kubectl context is "
             "kind-<name>. Example: dev", "kind-config.yaml: name", {}),
    "feature_gates": ("Kubernetes feature gates applied to every component, one Name=true|false per line. "
                      "Example: InPlacePodVerticalScaling=true", "kind-config.yaml: featureGates", {}),
    "nodes": ("Node containers to create; each row expands to Count identical nodes. You need at least one "
              "control-plane.", "kind-config.yaml: nodes[]", {}),
    "nodes.role": ("Role of the node(s).", "nodes[].role",
                   {"control-plane": "runs the API server/etcd; 3 gives an HA control plane",
                    "worker": "runs workloads only"}),
    "nodes.count": ("How many identical nodes this row creates. Example: 2", "repeated nodes[] entries", {}),
    "nodes.image": ("kindest/node image (decides the Kubernetes version); blank = kind's built-in default. "
                    "Example: kindest/node:v1.37.0", "nodes[].image",
                    {"": "kind's default node image for this kind release"}),
    "nodes.labels": ("Labels set on these nodes, comma-separated key=value. Example: ingress-ready=true",
                     "nodes[].labels", {}),
    "port_mappings": ("Ports on your machine forwarded into the first control-plane container - how you reach "
                      "NodePorts or an ingress controller from the host.", "nodes[0].extraPortMappings", {}),
    "port_mappings.container_port": ("Port inside the node container (a NodePort or hostPort). Example: 80",
                                     "extraPortMappings[].containerPort", {}),
    "port_mappings.host_port": ("Port opened on your machine. Example: 8080", "extraPortMappings[].hostPort", {}),
    "port_mappings.listen_address": ("Host address to bind; 127.0.0.1 keeps it local. Example: 127.0.0.1",
                                     "extraPortMappings[].listenAddress",
                                     {"": "kind default (0.0.0.0, all interfaces)", "127.0.0.1": "this machine only",
                                      "0.0.0.0": "all interfaces (reachable from the LAN)"}),
    "port_mappings.protocol": ("Protocol forwarded.", "extraPortMappings[].protocol", PROTO),
    "ip_family": ("IP family of the cluster network.", "networking.ipFamily",
                  {"ipv4": "IPv4 only (default)", "ipv6": "IPv6 only", "dual": "dual-stack IPv4 + IPv6"}),
    "api_server_address": ("Host address the API server is published on; keep 127.0.0.1 unless remote machines "
                           "must reach it. Example: 127.0.0.1", "networking.apiServerAddress", {}),
    "api_server_port": ("Host port for the API server; blank = random free port. Example: 6443",
                        "networking.apiServerPort", {}),
    "pod_subnet": ("CIDR pod IPs are allocated from. Example: 10.244.0.0/16", "networking.podSubnet", {}),
    "service_subnet": ("CIDR Service cluster IPs are allocated from. Example: 10.96.0.0/16",
                       "networking.serviceSubnet", {}),
    "disable_default_cni": ("Don't install kindnet; nodes stay NotReady until you install a CNI (Calico, Cilium). "
                            "Example: tick to test Cilium", "networking.disableDefaultCNI", {}),
    "kube_proxy_mode": ("kube-proxy backend.", "networking.kubeProxyMode", PROXY),
},
"minikube": {
    "mk_output": ("Which files to generate.", "output files",
                  {"both (config.json + start-minikube.sh)": "persistent defaults + a start script",
                   "config.json": "~/.minikube/config/config.json, read by every `minikube start`",
                   "start script (start-minikube.sh)": "a `minikube start ...` script with all flags"}),
    "mk_profile": ("Profile (cluster) name; also the kubectl context name. Example: dev", "minikube -p <profile>", {}),
    "mk_driver": ("How minikube runs the node.", "",
                  {"docker": "node in a Docker container (default on most hosts)", "podman": "node in a Podman container",
                   "kvm2": "KVM VM (Linux)", "qemu2": "QEMU VM (macOS/Linux)", "virtualbox": "VirtualBox VM",
                   "vfkit": "Apple Virtualization.framework VM (macOS)", "hyperkit": "HyperKit VM (Intel macOS)",
                   "hyperv": "Hyper-V VM (Windows)", "vmware": "VMware VM", "none": "directly on this Linux host (root)",
                   "ssh": "existing machine over SSH", "": "auto-detect the best available driver"}),
    "mk_cpus": ("Number of CPUs given to each minikube node VM/container. Example: 4", "", {"max": "all host CPUs", "no-limit": "no cap (docker/podman)"}),
    "mk_memory": ("Memory given to the node (MB or with a unit). Example: 8g", "",
                  {"max": "all host memory", "no-limit": "no cap (docker/podman)"}),
    "mk_disk_size": ("Disk size of the VM (ignored by docker/podman). Example: 40g", "", {}),
    "mk_nodes": ("Total number of nodes to start (multi-node cluster). Example: 3", "minikube start --nodes", {}),
    "mk_ha": ("Create a highly-available control plane (3+ control-plane nodes behind kube-vip). Example: tick for "
              "HA testing", "minikube start --ha", {}),
    "mk_k8s_version": ("Kubernetes version to run. Example: v1.37.0", "",
                       {"stable": "minikube's default stable version", "latest": "newest version minikube supports"}),
    "mk_runtime": ("Container runtime inside the node.", "",
                   {"": "auto (docker on most drivers)", "containerd": "containerd", "docker": "Docker Engine via cri-dockerd",
                    "cri-o": "CRI-O"}),
    "mk_cni": ("CNI (pod network) plugin minikube installs; blank = auto. Example: calico", "minikube start --cni",
               {"": "auto (kindnet for multi-node, bridge otherwise)", "auto": "let minikube choose",
                "bridge": "simple bridge CNI", "calico": "Calico", "cilium": "Cilium", "flannel": "Flannel",
                "kindnet": "kindnet", "false": "no CNI"}),
    "mk_feature_gates": ("Feature gates for all components, comma-separated Name=true|false. "
                         "Example: InPlacePodVerticalScaling=true", "", {}),
    "mk_extra_config": ("Component flags as component.key=value per line. Example: kubelet.max-pods=200",
                        "minikube start --extra-config", {}),
    "mk_apiserver_names": ("Extra DNS names in the API server certificate. Example: k8s.lab.local",
                           "", {}),
    "mk_apiserver_port": ("API server port inside the node. Example: 8443", "", {}),
    "mk_service_cidr": ("Service cluster IP range. Example: 10.96.0.0/12", "minikube start --service-cluster-ip-range", {}),
    "mk_dns_domain": ("Cluster DNS domain. Example: cluster.local", "", {}),
    "mk_addons": ("Addons enabled at start. Example: ingress, metrics-server", "minikube start --addons", {}),
    "mk_ports": ("Ports published from the node container to the host (docker/podman). Example: 80:80,443:443",
                 "", {}),
    "mk_listen_address": ("Host address published ports bind to (docker/podman).", "",
                          {"": "default (127.0.0.1)", "127.0.0.1": "this machine only", "0.0.0.0": "all interfaces"}),
    "mk_static_ip": ("Fixed node IP on the docker/podman network. Example: 192.168.200.200", "", {}),
    "mk_subnet": ("Subnet for the docker/podman network minikube creates. Example: 192.168.58.0/24", "", {}),
    "mk_network": ("Attach to an existing docker/podman/kvm/qemu network. Example: minikube-net", "", {}),
    "mk_mount_string": ("Host folder mounted into the node as host:guest. Example: /home/me/src:/src",
                        "minikube start --mount --mount-string", {}),
    "mk_insecure_registry": ("Registries allowed over plain HTTP. Example: 10.0.0.5:5000", "", {}),
    "mk_registry_mirror": ("Docker Hub mirror URLs. Example: https://mirror.gcr.io", "minikube start --registry-mirror", {}),
    "mk_image_mirror_country": ("Use a country-local image mirror. Example: cn", "", {}),
    "mk_wait": ("Which components `minikube start` waits for before returning.", "",
                {"": "default: apiserver,system_pods", "all": "every check", "none": "return immediately"}),
    "mk_keep_context": ("Don't switch kubectl's current-context to the new cluster. Example: tick in CI", "", {}),
    "mk_embed_certs": ("Embed certificates in the kubeconfig instead of referencing files. Example: tick to copy the "
                       "kubeconfig elsewhere", "", {}),
    "mk_delete_on_failure": ("Delete and recreate the cluster automatically if start fails. Example: tick in CI",
                             "", {}),
},
"k3s / RKE2": {
    "distro": ("Which distribution the config.yaml is for.", "/etc/rancher/<distro>/config.yaml",
               {"k3s": "lightweight single binary (Traefik, ServiceLB, local-path built in)",
                "rke2": "hardened, CIS-oriented distribution (Canal, ingress-nginx)"}),
    "role": ("Whether this node runs the control plane.", "k3s server / k3s agent (rke2 server/agent)",
             {"server": "control plane (+ workloads unless tainted)", "agent": "worker that joins a server"}),
    "cluster_init": ("Initialise a new HA cluster with embedded etcd on this first server (others join with "
                     "server: + token). Example: tick on server 1", "cluster-init: true", {}),
    "token": ("Shared secret nodes use to join the cluster. Example: a 32+ char random string", "token", {}),
    "server_url": ("URL of an existing server to join; blank on the first server. "
                   "Example: https://10.0.0.10:6443 (RKE2: :9345)", "server", {}),
    "node_name": ("Name this node registers as; blank = hostname. Example: worker-1", "node-name", {}),
    "node_ip": ("IP(s) the node advertises inside the cluster, comma-separated for dual-stack. Example: 10.0.0.11",
                "node-ip", {}),
    "node_external_ip": ("Public IP advertised for the node. Example: 203.0.113.10", "node-external-ip", {}),
    "tls_san": ("Extra hostnames/IPs in the API server certificate (load balancer, DNS). Example: k8s.example.com",
                "tls-san", {}),
    "cluster_cidr": ("Pod network CIDR; each node gets a slice of it. Example: 10.42.0.0/16", "cluster-cidr", {}),
    "service_cidr": ("Service cluster IP range. Example: 10.43.0.0/16", "service-cidr", {}),
    "cluster_dns": ("Cluster IP of CoreDNS; must be inside service-cidr. Example: 10.43.0.10", "cluster-dns", {}),
    "cluster_domain": ("Cluster DNS domain. Example: cluster.local", "cluster-domain", {}),
    "flannel_backend": ("k3s flannel encapsulation.", "flannel-backend",
                        {"": "default (vxlan)", "vxlan": "VXLAN overlay", "host-gw": "routes via node IPs (same L2 network)",
                         "wireguard-native": "encrypted WireGuard tunnels", "none": "no flannel: install your own CNI"}),
    "cni": ("RKE2 CNI plugin.", "cni",
            {"": "default (canal)", "canal": "Calico policy + flannel", "calico": "Calico", "cilium": "Cilium",
             "flannel": "flannel", "none": "install your own CNI"}),
    "multus": ("Add Multus in front of the CNI so pods can get several network interfaces. Example: tick for SR-IOV",
               "cni: [multus, <cni>]", {}),
    "ingress_controller": ("RKE2 bundled ingress controller.", "ingress-controller",
                           {"": "default (ingress-nginx)", "ingress-nginx": "ingress-nginx", "traefik": "Traefik",
                            "none": "no bundled ingress controller"}),
    "write_kubeconfig_mode": ("File mode of the admin kubeconfig the server writes. Example: 0644 lets non-root "
                              "users read it", "write-kubeconfig-mode", {}),
    "node_label": ("Labels registered on the node, key=value per line. Example: zone=lab", "node-label", {}),
    "node_taint": ("Taints registered on the node, key=value:Effect per line. Example: dedicated=gpu:NoSchedule",
                   "node-taint", {}),
},
"k0s": {
    "k0_output": ("Which files to generate.", "output files",
                  {"k0sctl.yaml (multi-node over SSH)": "k0sctl Cluster: installs every host over SSH",
                   "k0s.yaml (ClusterConfig only)": "for `k0s install controller --config k0s.yaml`",
                   "both": "both files"}),
    "k0_cluster_name": ("Name of the k0sctl cluster object. Example: lab", "k0sctl.yaml: metadata.name", {}),
    "k0_version": ("k0s version k0sctl installs; blank = latest stable. Example: v1.36.4+k0s.1",
                   "k0sctl.yaml: spec.k0s.version", {"": "latest stable chosen by k0sctl"}),
    "k0s_hosts": ("Machines k0sctl connects to over SSH and their roles.", "k0sctl.yaml: spec.hosts[]", {}),
    "k0s_hosts.role": ("Role of the host.", "spec.hosts[].role",
                       {"controller": "control plane only", "worker": "workloads only",
                        "controller+worker": "control plane that also runs workloads", "single": "one-node cluster"}),
    "k0s_hosts.address": ("SSH address (IP or hostname). Example: 10.0.0.10", "spec.hosts[].ssh.address", {}),
    "k0s_hosts.user": ("SSH user (needs passwordless sudo). Example: ubuntu", "spec.hosts[].ssh.user", {}),
    "k0s_hosts.port": ("SSH port k0sctl connects to on this host. Example: 22", "spec.hosts[].ssh.port", {}),
    "k0s_hosts.key_path": ("Private key file for SSH; blank = default keys / ssh-agent. Example: ~/.ssh/lab_ed25519",
                           "spec.hosts[].ssh.keyPath", {}),
    "k0s_hosts.hostname": ("Override the Kubernetes node name (default: hostname). Example: cp-1", "spec.hosts[].hostname", {}),
    "k0s_hosts.install_flags": ("Extra `k0s install` flags, comma-separated. Example: --debug,--labels=a=b",
                                "spec.hosts[].installFlags", {}),
    "k0_api_address": ("Address the API server advertises; blank = first controller's IP. Example: 10.0.0.10",
                       "", {}),
    "k0_api_port": ("Port the Kubernetes API server listens on. Example: 6443", "", {}),
    "k0_api_sans": ("Extra IPs/DNS names in the API certificate. Example: k8s.lab.local", "", {}),
    "k0_external_address": ("Load balancer address for an HA control plane. Example: k8s-lb.lab.local", "", {}),
    "k0_storage": ("Control-plane datastore.", "",
                   {"etcd": "embedded etcd (HA capable)", "kine": "kine: SQLite/MySQL/PostgreSQL (single controller)"}),
    "k0_kine_ds": ("kine data source URL. Example: sqlite:///var/lib/k0s/db/state.db", "spec.storage.kine.dataSource", {}),
    "k0_provider": ("CNI provider.", "",
                    {"kuberouter": "kube-router (k0s default)", "calico": "Calico", "custom": "bring your own CNI"}),
    "k0_pod_cidr": ("Pod network CIDR. Example: 10.244.0.0/16", "spec.network.podCIDR", {}),
    "k0_service_cidr": ("Service cluster IP range. Example: 10.96.0.0/12", "spec.network.serviceCIDR", {}),
    "k0_cluster_domain": ("Cluster DNS domain. Example: cluster.local", "spec.network.clusterDomain", {}),
    "k0_proxy_mode": ("kube-proxy backend.", "spec.network.kubeProxy.mode",
                      {k: v for k, v in PROXY.items() if k in ("iptables", "ipvs", "nftables")}),
    "k0_proxy_disabled": ("Don't run kube-proxy (e.g. Cilium kube-proxy replacement). Example: tick with Cilium",
                          "spec.network.kubeProxy.disabled", {}),
    "k0_nllb": ("Node-local load balancing: workers reach all controllers via a local Envoy proxy - HA without "
                "an external LB. Example: tick for 3 controllers", "spec.network.nodeLocalLoadBalancing.enabled", {}),
    "k0_telemetry": ("Send anonymous usage statistics to the k0s project. Example: untick for air-gapped sites",
                     "spec.telemetry.enabled", {}),
    "k0_disable_wait": ("Don't wait for nodes to become ready after k0sctl apply. Example: tick for slow nodes",
                        "k0sctl.yaml: spec.options.wait.enabled: false", {}),
},
"MicroK8s": {
    "m8_version": ("Launch configuration schema version.", "launch config: version",
                   {"0.1.0": "original schema", "0.2.0": "newer schema (adds more keys)"}),
    "m8_channel": ("Snap channel used in the install hint (decides the Kubernetes version). Example: 1.36/stable",
                   "snap install microk8s --channel", {}),
    "m8_addons": ("Addons to enable, one per line, optional args after ':'. Example: metallb:10.64.140.43-10.64.140.49",
                  "addons[] (name, args)", {}),
    "m8_disable": ("Addons to disable, comma-separated. Example: ha-cluster", "addons[] (disable: true)", {}),
    "m8_apiserver_args": ("Extra kube-apiserver flags, --flag=value per line ('--flag' alone removes it). "
                          "Example: --event-ttl=2h", "extraKubeAPIServerArgs", {}),
    "m8_kubelet_args": ("Extra kubelet flags, --flag=value per line. Example: --max-pods=200", "extraKubeletArgs", {}),
    "m8_proxy_args": ("Extra kube-proxy flags. Example: --proxy-mode=ipvs", "extraKubeProxyArgs", {}),
    "m8_cm_args": ("Extra kube-controller-manager flags. Example: --node-monitor-grace-period=20s",
                   "extraKubeControllerManagerArgs", {}),
    "m8_sched_args": ("Extra kube-scheduler flags. Example: --v=2", "extraKubeSchedulerArgs", {}),
    "m8_containerd_args": ("Extra containerd flags. Example: --log-level=debug", "extraContainerdArgs", {}),
    "m8_sans": ("Extra IPs/DNS names in the API certificate. Example: 10.0.0.10,k8s.lab", "extraSANs", {}),
    "m8_registry_mirrors": ("Registry mirrors, registry=URL per line. Example: docker.io=https://mirror.gcr.io",
                            "containerdRegistryConfigs", {}),
    "m8_cluster_token": ("Persistent join token (32+ chars) so nodes can `microk8s join <ip>:25000/<token>` anytime. "
                         "Example: output of `openssl rand -hex 16`", "persistentClusterToken", {}),
    "m8_join_url": ("Join an existing cluster at <ip>:25000/<token>. Example: 10.0.0.10:25000/92b2db237428470dc4fcfc4ebbd9dc81", "join.url", {}),
    "m8_join_worker": ("Join as a worker only (no control plane). Example: tick for worker nodes", "join.worker", {}),
},
}

DISABLE_COMPONENTS = {
    "traefik": "the Traefik ingress controller", "servicelb": "ServiceLB (Klipper) LoadBalancer implementation",
    "local-storage": "the local-path StorageClass provisioner", "metrics-server": "metrics-server (kubectl top, HPA)",
    "coredns": "CoreDNS cluster DNS", "rke2-coredns": "CoreDNS cluster DNS", "rke2-metrics-server": "metrics-server",
    "rke2-snapshot-controller": "the CSI snapshot controller",
    "rke2-snapshot-controller-crd": "the VolumeSnapshot CRDs",
    "rke2-snapshot-validation-webhook": "the snapshot validation webhook",
    "rke2-security-responder": "the security-responder (version check) job",
    "rke2-gateway-api-crd": "the Gateway API CRDs",
}

C.update({
"kubeadm": {
    "output": ("Which kubeadm config to generate.", "output file",
               {"init (InitConfiguration + ClusterConfiguration)": "kubeadm-config.yaml for `kubeadm init --config`",
                "join (JoinConfiguration)": "kubeadm-join-config.yaml for `kubeadm join --config`"}),
    "advertise_address": ("IP this control-plane node's API server advertises to the cluster. Example: 192.168.1.10",
                          "InitConfiguration.localAPIEndpoint.advertiseAddress", {}),
    "bind_port": ("Port the API server listens on. Example: 6443", "InitConfiguration.localAPIEndpoint.bindPort", {}),
    "node_name": ("Name the node registers as; blank = hostname. Example: cp-1",
                  "InitConfiguration.nodeRegistration.name", {}),
    "cri_socket": ("Container runtime socket kubeadm/kubelet talk to. Example: unix:///var/run/containerd/containerd.sock",
                   "InitConfiguration.nodeRegistration.criSocket",
                   {"unix:///var/run/containerd/containerd.sock": "containerd",
                    "unix:///var/run/crio/crio.sock": "CRI-O", "unix:///var/run/cri-dockerd.sock": "Docker via cri-dockerd"}),
    "taints": ("Taints for this node, key[=value]:Effect per line; 'none' = schedulable control plane. "
               "Example: node-role.kubernetes.io/control-plane:NoSchedule", "nodeRegistration.taints", {}),
    "kubelet_extra_args": ("Extra kubelet flags for this node, name=value per line. Example: node-ip=192.168.1.10",
                           "nodeRegistration.kubeletExtraArgs", {}),
    "bootstrap_token": ("Token nodes use to join, format [a-z0-9]{6}.[a-z0-9]{16}; blank = generated. "
                        "Example: abcdef.0123456789abcdef", "InitConfiguration.bootstrapTokens[0].token", {}),
    "token_ttl": ("How long the bootstrap token stays valid. Example: 24h0m0s", "bootstrapTokens[0].ttl", {}),
    "kubernetes_version": ("Kubernetes version to install. Example: v1.37.1", "ClusterConfiguration.kubernetesVersion",
                           {"stable": "latest stable release (looked up online)",
                            "stable-1.37": "latest patch of 1.37 (looked up online)"}),
    "cluster_name": ("Cluster name used in kubeconfigs. Example: lab", "ClusterConfiguration.clusterName", {}),
    "control_plane_endpoint": ("Stable host[:port] (load balancer / VIP / DNS) for all control planes - set it now "
                               "if you may go HA later. Example: k8s-api.lab:6443",
                               "ClusterConfiguration.controlPlaneEndpoint", {}),
    "pod_subnet": ("Pod network CIDR; must match your CNI's config. Example: 10.244.0.0/16",
                   "ClusterConfiguration.networking.podSubnet", {}),
    "service_subnet": ("Service cluster IP range. Example: 10.96.0.0/12", "ClusterConfiguration.networking.serviceSubnet", {}),
    "dns_domain": ("Cluster DNS domain. Example: cluster.local", "ClusterConfiguration.networking.dnsDomain", {}),
    "image_repository": ("Registry the control-plane images are pulled from. Example: registry.k8s.io",
                         "ClusterConfiguration.imageRepository",
                         {"registry.k8s.io": "current official registry", "k8s.gcr.io": "legacy registry (frozen)"}),
    "cert_sans": ("Extra IPs/DNS names in the API server certificate. Example: k8s-api.lab, 10.0.0.100",
                  "ClusterConfiguration.apiServer.certSANs", {}),
    "etcd_data_dir": ("Where the local etcd stores data. Example: /var/lib/etcd", "ClusterConfiguration.etcd.local.dataDir", {}),
    "include_kubelet": ("Add a KubeletConfiguration document to the file. Example: tick to set cgroupDriver",
                        "KubeletConfiguration document", {}),
    "cgroup_driver": ("cgroup driver; must match the container runtime.", "KubeletConfiguration.cgroupDriver",
                      {"systemd": "recommended on systemd hosts (containerd/CRI-O default)", "cgroupfs": "legacy driver"}),
    "include_kube_proxy": ("Add a KubeProxyConfiguration document. Example: tick to pick ipvs/nftables",
                           "KubeProxyConfiguration document", {}),
    "proxy_mode": ("kube-proxy backend.", "KubeProxyConfiguration.mode",
                   {k: v for k, v in PROXY.items() if k in ("iptables", "ipvs", "nftables")}),
    "join_endpoint": ("host:port of the API server to join. Example: k8s-api.lab:6443",
                      "JoinConfiguration.discovery.bootstrapToken.apiServerEndpoint", {}),
    "join_token": ("Bootstrap token from `kubeadm token create --print-join-command`. Example: abcdef.0123456789abcdef",
                   "JoinConfiguration.discovery.bootstrapToken.token", {}),
    "ca_cert_hashes": ("Pins the cluster CA so the joining node can trust it, sha256:<hex> per line. "
                       "Example: sha256:7c9e2a1b04f3d6e8 (64 hex chars)", "discovery.bootstrapToken.caCertHashes", {}),
    "unsafe_skip_ca": ("Join without CA pinning (vulnerable to MITM - labs only). Example: untick in production",
                       "discovery.bootstrapToken.unsafeSkipCAVerification", {}),
    "join_control_plane": ("Join as an additional control-plane node (needs certificateKey). Example: tick for HA",
                           "JoinConfiguration.controlPlane", {}),
    "certificate_key": ("Key that decrypts the uploaded control-plane certificates (64 hex chars from "
                        "`kubeadm init phase upload-certs --upload-certs`). Example: e6a2eb8581237ab72a4f (64 hex chars)",
                        "JoinConfiguration.controlPlane.certificateKey", {}),
},
"Talos": {
    "tl_cluster_name": ("Cluster name (used in kubeconfig and talosconfig contexts). Example: lab", "talosctl gen config <name> <endpoint>", {}),
    "tl_endpoint": ("Kubernetes API endpoint every node uses (VIP, LB or first control plane). "
                    "Example: https://10.0.0.10:6443", "talosctl gen config <name> <endpoint>", {}),
    "tl_k8s_version": ("Kubernetes version the control plane runs; blank = Talos default. Example: 1.37.1", "", {}),
    "tl_talos_version": ("Talos version the config targets (older targets use legacy patch formats).", "",
                         {"": "this talosctl's version", "v1.14": "Talos 1.14", "v1.13": "Talos 1.13"}),
    "tl_sans": ("Extra API certificate SANs. Example: k8s.lab.local", "", {}),
    "tl_dns_domain": ("Cluster DNS domain. Example: cluster.local", "", {}),
    "tl_install_disk": ("Disk Talos installs to. Example: /dev/nvme0n1", "", {}),
    "tl_install_image": ("Installer image; use an Image Factory URL to include system extensions. "
                         "Example: factory.talos.dev/installer/<schematic>:v1.14.0", "", {}),
    "tl_kubespan": ("Enable KubeSpan: WireGuard mesh between all nodes. Example: tick across sites", "", {}),
    "tl_discovery": ("Enable cluster discovery (needed by KubeSpan). Example: tick", "", {}),
    "tl_registry_mirrors": ("Registry mirrors, registry=URL per line. Example: docker.io=https://mirror.gcr.io",
                            "talosctl gen config --registry-mirror", {}),
    "tl_validate_mode": ("Mode used when validating the generated configs.", "talosctl validate --mode",
                         {"metal": "bare metal", "cloud": "cloud VMs", "container": "Talos in Docker"}),
    "tl_out_dir": ("Folder talosctl writes controlplane.yaml / worker.yaml / talosconfig to. Example: _out",
                   "talosctl gen config --output-dir", {}),
    "tl_pod_subnets": ("Pod network CIDR(s), comma-separated for dual-stack. Example: 10.244.0.0/16", "cluster.network.podSubnets", {}),
    "tl_service_subnets": ("Service ClusterIP CIDR(s), comma-separated for dual-stack. Example: 10.96.0.0/12", "cluster.network.serviceSubnets", {}),
    "tl_cni": ("CNI Talos deploys.", "cluster.network.cni.name",
               {"flannel (default)": "Talos-managed flannel", "none (bring your own: Cilium, Calico...)": "no CNI"}),
    "tl_flannel_backend": ("Flannel backend.", "flannel extraArgs / net-conf backend",
                           {"vxlan": "VXLAN overlay", "host-gw": "routes via node IPs", "wireguard": "encrypted"}),
    "tl_proxy_mode": ("kube-proxy backend.", "cluster.proxy.mode",
                      {k: v for k, v in PROXY.items() if k in ("iptables", "ipvs", "nftables")}),
    "tl_proxy_disabled": ("Don't run kube-proxy (Cilium replacement). Example: tick with Cilium",
                          "KubeProxyConfig enabled: false", {}),
    "tl_nameservers": ("DNS servers for the nodes. Example: 1.1.1.1, 8.8.8.8", "ResolverConfig nameservers", {}),
    "tl_ntp": ("NTP servers. Example: time.cloudflare.com", "TimeSyncConfig ntp.servers", {}),
    "tl_schedule_cp": ("Allow workloads on control-plane nodes (removes their NoSchedule taint). "
                       "Example: tick for 3-node clusters", "cluster.allowSchedulingOnControlPlanes", {}),
    "tl_node_labels": ("Labels on all nodes, key=value per line. Example: site=lab", "machine.nodeLabels", {}),
    "tl_node_taints": ("Taints on worker nodes, key=value:Effect per line. Example: gpu=true:NoSchedule",
                       "machine.nodeTaints", {}),
    "tl_max_pods": ("Maximum pods the kubelet runs on each node. Example: 250", "machine.kubelet.extraConfig.maxPods", {}),
    "tl_kubelet_args": ("Extra kubelet flags, name=value per line. Example: rotate-server-certificates=true",
                        "machine.kubelet.extraArgs", {}),
},
"Kubespray": {
    "ks_inventory": ("Inventory folder name; files go to inventory/<name>/. Example: lab",
                     "inventory/<name>/", {}),
    "ks_version": ("Kubernetes version Kubespray installs. Example: 1.35.8", "kube_version", {}),
    "ks_cluster_name": ("Cluster DNS domain. Example: cluster.local", "cluster_name", {}),
    "ks_runtime": ("Container runtime.", "container_manager",
                   {"containerd": "containerd (default)", "crio": "CRI-O", "docker": "Docker via cri-dockerd"}),
    "ks_etcd": ("How etcd is deployed.", "etcd_deployment_type",
                {"host": "systemd service on the etcd hosts (default)", "kubeadm": "static pods managed by kubeadm",
                 "docker": "etcd in Docker containers"}),
    "ks_user": ("SSH user Ansible connects as. Example: ubuntu", "all.vars.ansible_user", {}),
    "ks_hosts": ("Machines in the inventory and their groups.", "inventory.ini / hosts.yaml", {}),
    "ks_hosts.name": ("Inventory host name (becomes the node name). Example: node1", "hosts.<name>", {}),
    "ks_hosts.ansible_host": ("Address Ansible SSHes to. Example: 10.0.0.11", "hosts.<name>.ansible_host", {}),
    "ks_hosts.ip": ("Address Kubernetes binds to on that host; blank = default interface. Example: 10.0.0.11",
                    "hosts.<name>.ip", {}),
    "ks_hosts.roles": ("Inventory groups the host belongs to.", "kube_control_plane / etcd / kube_node",
                       {"control-plane+etcd": "control plane and etcd member", "node": "worker",
                        "control-plane+etcd+node": "all-in-one", "control-plane": "control plane only", "etcd": "etcd only"}),
    "ks_hosts.ansible_user": ("SSH user for this host only. Example: rocky", "hosts.<name>.ansible_user", {}),
    "ks_plugin": ("Network plugin.", "kube_network_plugin",
                  {"calico": "Calico (default)", "cilium": "Cilium", "flannel": "flannel", "kube-ovn": "Kube-OVN",
                   "kube-router": "kube-router", "macvlan": "macvlan", "cni": "generic CNI (you provide config)",
                   "custom_cni": "your own manifests/Helm chart", "cloud": "cloud provider routes", "none": "no CNI"}),
    "ks_proxy": ("kube-proxy backend.", "kube_proxy_mode",
                 {k: v for k, v in PROXY.items() if k in ("iptables", "ipvs", "nftables")}),
    "ks_strict_arp": ("Enable strict ARP for kube-proxy (required by MetalLB with ipvs). Example: tick with MetalLB",
                      "kube_proxy_strict_arp", {}),
    "ks_svc": ("Service cluster IP range. Example: 10.233.0.0/18", "kube_service_addresses", {}),
    "ks_pods": ("Pod network CIDR. Example: 10.233.64.0/18", "kube_pods_subnet", {}),
    "ks_node_prefix": ("Size of each node's pod subnet. Example: 24", "kube_network_node_prefix", {}),
    "ks_ipv6": ("Enable dual-stack IPv4/IPv6. Example: tick on IPv6 networks", "ipv6_stack", {}),
    "ks_dns": ("Cluster DNS setup.", "dns_mode",
               {"coredns": "CoreDNS (default)", "coredns_dual": "two CoreDNS deployments",
                "manual": "use upstream_dns_servers you specify", "none": "no cluster DNS"}),
    "ks_nodelocaldns": ("Run NodeLocal DNSCache on every node (faster, fewer conntrack issues). Example: tick",
                        "enable_nodelocaldns", {}),
    "ks_api_port": ("Port the Kubernetes API server listens on. Example: 6443", "kube_apiserver_port", {}),
    "ks_sans": ("Extra API certificate SANs (LB IP/DNS). Example: k8s.lab.local",
                "supplementary_addresses_in_ssl_keys", {}),
    "ks_kubeconfig_localhost": ("Copy admin.conf back to the inventory artifacts folder. Example: tick",
                                "kubeconfig_localhost", {}),
    "ks_encrypt": ("Encrypt Secrets at rest in etcd. Example: tick for production", "kube_encrypt_secret_data", {}),
    "ks_autorenew": ("Renew control-plane certificates monthly via systemd timer. Example: tick",
                     "auto_renew_certificates", {}),
    "ks_addons": ("Add-ons to enable, comma-separated (sets <name>_enabled: true). Example: metrics_server, helm",
                  "<addon>_enabled", {}),
    "ks_metallb_range": ("Address pool for MetalLB LoadBalancer Services. Example: 192.168.10.200-192.168.10.220",
                         "metallb_config.address_pools", {}),
},
"Cluster API": {
    "ca_name": ("Name of the Cluster object (and prefix of machines). Example: dev", "Cluster.metadata.name", {}),
    "ca_namespace": ("Namespace on the management cluster holding the objects. Example: default",
                     "metadata.namespace", {}),
    "ca_provider": ("Infrastructure provider.", "Cluster.spec.infrastructureRef",
                    {"docker (CAPD)": "Docker provider: machines are containers (dev/test)"}),
    "ca_version": ("Kubernetes version of the workload cluster. Example: v1.35.0",
                   "KubeadmControlPlane.spec.version / MachineDeployment", {}),
    "ca_cp_replicas": ("Number of control-plane machines (odd for etcd quorum).", "KubeadmControlPlane.spec.replicas",
                       {"1": "single control plane", "3": "HA (tolerates 1 failure)", "5": "HA (tolerates 2 failures)"}),
    "ca_workers": ("Worker machines in the MachineDeployment. Example: 2", "MachineDeployment.spec.replicas", {}),
    "ca_md_name": ("Suffix of the MachineDeployment name (<cluster>-<suffix>). Example: md-0",
                   "MachineDeployment.metadata.name", {}),
    "ca_labels": ("Labels on the Cluster (ClusterResourceSets match on these). Example: cni=calico",
                  "Cluster.metadata.labels", {}),
    "ca_pods": ("Pod CIDR blocks. Example: 192.168.0.0/16", "Cluster.spec.clusterNetwork.pods.cidrBlocks", {}),
    "ca_services": ("Service CIDR blocks. Example: 10.128.0.0/12", "Cluster.spec.clusterNetwork.services.cidrBlocks", {}),
    "ca_domain": ("Service DNS domain. Example: cluster.local", "Cluster.spec.clusterNetwork.serviceDomain", {}),
    "ca_sans": ("Extra API certificate SANs. Example: dev.lab.local",
                "KubeadmControlPlane ... clusterConfiguration.apiServer.certSANs", {}),
    "ca_image": ("Node image for CAPD machines; blank = kindest/node:<version>. Example: kindest/node:v1.35.0",
                 "DockerMachineTemplate.spec.template.spec.customImage", {}),
    "ca_relax_eviction": ("Relax kubelet disk eviction thresholds (CAPD quickstart default, avoids evictions on "
                          "laptops). Example: tick", "kubeletExtraArgs eviction-hard", {}),
    "ca_worker_labels": ("Labels kubelet registers on worker nodes. Example: role=worker",
                         "KubeadmConfigTemplate ... kubeletExtraArgs node-labels", {}),
},
})

C.update({
"OpenShift": {
    "oc_name": ("Cluster name; the API becomes api.<name>.<baseDomain>. Example: ocp", "metadata.name", {}),
    "oc_base_domain": ("DNS zone the cluster lives under. Example: example.com", "baseDomain", {}),
    "oc_platform": ("Where the installer provisions the cluster.", "platform",
                    {"none": "user-provisioned / single node, no platform integration", "aws": "Amazon Web Services",
                     "azure": "Microsoft Azure", "gcp": "Google Cloud", "vsphere": "VMware vSphere",
                     "baremetal": "installer-provisioned bare metal (IPMI/Redfish)"}),
    "oc_cp_replicas": ("Number of control-plane (master) nodes.", "controlPlane.replicas",
                       {"3": "standard HA", "1": "Single Node OpenShift (SNO)", "4": "4-node control plane (4.17+)",
                        "5": "5-node control plane (4.17+)"}),
    "oc_workers": ("Compute (worker) node count; 0 = compact cluster where masters run workloads. Example: 3",
                   "compute[0].replicas", {}),
    "oc_hyperthreading": ("SMT on the machines.", "controlPlane/compute.hyperthreading",
                          {"Enabled": "default", "Disabled": "turn SMT off (some security baselines)"}),
    "oc_fips": ("Install in FIPS 140 validated crypto mode (cannot be changed later). Example: tick for FedRAMP",
                "fips", {}),
    "oc_publish": ("How API and ingress endpoints are exposed.", "publish",
                   {"External": "public endpoints", "Internal": "private, reachable only inside the network"}),
    "oc_pull_secret": ("Pull secret JSON from console.redhat.com, required to pull OpenShift images. "
                       "Example: {\"auths\":{...}}", "pullSecret", {}),
    "oc_ssh_key": ("Public SSH key added to core@ on every node for debugging. Example: ssh-ed25519 AAAA... me",
                   "sshKey", {}),
    "oc_network_type": ("Cluster network plugin.", "networking.networkType",
                        {"OVNKubernetes": "OVN-Kubernetes, the only supported plugin in 4.15+"}),
    "oc_cluster_net": ("Pod network CIDR. Example: 10.128.0.0/14", "networking.clusterNetwork[0].cidr", {}),
    "oc_host_prefix": ("Prefix length of each node's pod subnet (23 = 510 pods). Example: 23",
                       "networking.clusterNetwork[0].hostPrefix", {}),
    "oc_service_net": ("Service network CIDR. Example: 172.30.0.0/16", "networking.serviceNetwork[0]", {}),
    "oc_machine_net": ("CIDR of the nodes' own network. Example: 10.0.0.0/16", "networking.machineNetwork[0].cidr", {}),
    "oc_region": ("Cloud region (aws/azure/gcp). Example: us-east-1", "platform.<cloud>.region", {}),
    "oc_instance_type": ("Cloud VM type for workers. Example: m6i.xlarge", "compute[0].platform.<cloud>.type", {}),
    "oc_azure_rg": ("Resource group holding the base-domain DNS zone. Example: dns-rg",
                    "platform.azure.baseDomainResourceGroupName", {}),
    "oc_azure_cloud": ("Azure cloud environment.", "platform.azure.cloudName",
                       {"AzurePublicCloud": "global Azure", "AzureUSGovernmentCloud": "Azure Government",
                        "AzureChinaCloud": "Azure China (21Vianet)", "AzureStackCloud": "Azure Stack Hub"}),
    "oc_gcp_project": ("GCP project ID to install into. Example: my-proj-123", "platform.gcp.projectID", {}),
    "oc_vc_server": ("vCenter hostname. Example: vcenter.lab.local", "platform.vsphere.vcenters[0].server", {}),
    "oc_vc_user": ("vCenter user. Example: administrator@vsphere.local", "platform.vsphere.vcenters[0].user", {}),
    "oc_vc_password": ("vCenter password (stored in plain text in install-config.yaml).",
                       "platform.vsphere.vcenters[0].password", {}),
    "oc_vc_datacenter": ("vSphere datacenter the VMs are created in. Example: DC1", "platform.vsphere.failureDomains[0].topology.datacenter", {}),
    "oc_vc_cluster": ("vSphere compute cluster path. Example: /DC1/host/Cluster1",
                      "failureDomains[0].topology.computeCluster", {}),
    "oc_vc_datastore": ("Datastore path for VM disks. Example: /DC1/datastore/ds1", "failureDomains[0].topology.datastore", {}),
    "oc_vc_network": ("Port group the VMs attach to. Example: VM Network", "failureDomains[0].topology.networks[0]", {}),
    "oc_api_vip": ("Virtual IP for the API (vsphere/baremetal). Example: 10.0.0.5", "platform.<p>.apiVIPs", {}),
    "oc_ingress_vip": ("Virtual IP for *.apps ingress (vsphere/baremetal). Example: 10.0.0.6", "platform.<p>.ingressVIPs", {}),
    "oc_provisioning": ("Bare-metal provisioning network mode.", "platform.baremetal.provisioningNetwork",
                        {"Managed": "installer runs DHCP/PXE on a dedicated network",
                         "Unmanaged": "dedicated network, your own DHCP", "Disabled": "virtual media boot, no PXE network"}),
    "ocp_bm_hosts": ("Bare-metal machines the installer powers on via their BMC.", "platform.baremetal.hosts", {}),
    "ocp_bm_hosts.name": ("Name of the BareMetalHost / node. Example: master-0", "hosts[].name", {}),
    "ocp_bm_hosts.role": ("Role of the host.", "hosts[].role", {"master": "control plane", "worker": "compute"}),
    "ocp_bm_hosts.bmc_address": ("BMC URL (ipmi:// or redfish-virtualmedia://). "
                                 "Example: redfish-virtualmedia://10.0.1.10/redfish/v1/Systems/1", "hosts[].bmc.address", {}),
    "ocp_bm_hosts.bmc_user": ("User for the host's BMC (iDRAC, iLO, IPMI). Example: admin", "hosts[].bmc.username", {}),
    "ocp_bm_hosts.bmc_password": ("BMC password (plain text in install-config.yaml).", "hosts[].bmc.password", {}),
    "ocp_bm_hosts.boot_mac": ("MAC of the NIC the host boots from. Example: 52:54:00:aa:bb:01", "hosts[].bootMACAddress", {}),
    "ocp_bm_hosts.insecure": ("Skip BMC TLS certificate verification (self-signed BMCs). Example: tick in labs",
                              "hosts[].bmc.disableCertificateVerification", {}),
},
"EKS": {
    "ek_name": ("EKS cluster name (also prefixes CloudFormation stacks). Example: dev", "metadata.name", {}),
    "ek_region": ("AWS region the cluster and VPC are created in. Example: us-west-2", "metadata.region", {}),
    "ek_version": ("Kubernetes minor version of the EKS control plane. Example: 1.34", "metadata.version", {}),
    "ek_tags": ("AWS tags on all created resources, key=value comma-separated. Example: team=web,env=dev",
                "metadata.tags", {}),
    "ek_auto_mode": ("EKS Auto Mode: AWS manages nodes, storage and load balancers; node groups are optional. "
                     "Example: tick for hands-off clusters", "autoModeConfig.enabled", {}),
    "eks_nodegroups": ("EKS managed node groups (EC2 Auto Scaling groups).", "managedNodeGroups", {}),
    "eks_nodegroups.name": ("Name of the managed node group (unique per cluster). Example: ng-1", "managedNodeGroups[].name", {}),
    "eks_nodegroups.instance_types": ("EC2 type(s), comma-separated for several (useful with spot). Example: m6i.large",
                                      "instanceType / instanceTypes", {}),
    "eks_nodegroups.min": ("Minimum nodes the group scales down to. Example: 1", "minSize", {}),
    "eks_nodegroups.desired": ("Number of nodes the group starts with (between min and max). Example: 2", "desiredCapacity", {}),
    "eks_nodegroups.max": ("Maximum nodes the group scales to. Example: 4", "maxSize", {}),
    "eks_nodegroups.volume_size": ("Root EBS volume size in GiB. Example: 80", "volumeSize", {}),
    "eks_nodegroups.volume_type": ("Root EBS volume type.", "volumeType",
                                   {"gp3": "general SSD (default, cheapest per IOPS)", "gp2": "older general SSD",
                                    "io1": "provisioned-IOPS SSD", "io2": "provisioned-IOPS SSD, higher durability",
                                    "st1": "throughput HDD", "sc1": "cold HDD"}),
    "eks_nodegroups.ami_family": ("Node OS image family.", "amiFamily",
                                  {"AmazonLinux2023": "Amazon Linux 2023 (default)", "Bottlerocket": "minimal container OS",
                                   "Ubuntu2404": "Ubuntu 24.04", "UbuntuPro2404": "Ubuntu Pro 24.04",
                                   "Ubuntu2204": "Ubuntu 22.04", "AmazonLinux2": "Amazon Linux 2 (EKS <=1.32 only)",
                                   "BottlerocketFips": "Bottlerocket with FIPS crypto",
                                   "WindowsServer2022CoreContainer": "Windows Server 2022 Core",
                                   "WindowsServer2025CoreContainer": "Windows Server 2025 Core"}),
    "eks_nodegroups.spot": ("Use Spot instances (cheap, can be reclaimed with 2 min notice). Example: tick for batch",
                            "spot", {}),
    "eks_nodegroups.private": ("Put nodes in private subnets only (egress via NAT). Example: tick", "privateNetworking", {}),
    "eks_nodegroups.labels": ("Kubernetes node labels, k=v comma-separated. Example: role=web", "labels", {}),
    "eks_nodegroups.taints": ("Node taints, key=value:Effect comma-separated. Example: gpu=true:NoSchedule", "taints", {}),
    "ek_vpc_cidr": ("CIDR of the VPC eksctl creates. Example: 192.168.0.0/16", "vpc.cidr", {}),
    "ek_nat": ("NAT gateways for private subnets.", "vpc.nat.gateway",
               {"Single": "one NAT gateway (cheaper)", "HighlyAvailable": "one per AZ",
                "Disable": "no NAT (private nodes have no internet)"}),
    "ek_azs": ("Availability zones, comma-separated; blank = eksctl picks. Example: us-west-2a,us-west-2b",
               "availabilityZones", {}),
    "ek_public": ("Expose the API endpoint publicly. Example: tick", "vpc.clusterEndpoints.publicAccess", {}),
    "ek_private": ("Make the API endpoint reachable from inside the VPC. Example: tick",
                   "vpc.clusterEndpoints.privateAccess", {}),
    "ek_public_cidrs": ("CIDRs allowed to reach the public endpoint. Example: 203.0.113.0/24", "vpc.publicAccessCIDRs", {}),
    "ek_ip_family": ("Address family for pods and services.", "kubernetesNetworkConfig.ipFamily",
                     {"IPv4": "default", "IPv6": "IPv6 pods/services (needs vpc-cni, coredns, kube-proxy addons + OIDC)"}),
    "ek_service_cidr": ("Service cluster IP range. Example: 10.100.0.0/16", "kubernetesNetworkConfig.serviceIPv4CIDR", {}),
    "ek_auth_mode": ("How IAM principals are mapped to Kubernetes access.", "accessConfig.authenticationMode",
                     {"API_AND_CONFIG_MAP": "access entries plus legacy aws-auth ConfigMap",
                      "API": "access entries only (recommended)", "CONFIG_MAP": "legacy aws-auth ConfigMap only"}),
    "ek_oidc": ("Create the IAM OIDC provider so pods can assume IAM roles (IRSA). Example: tick", "iam.withOIDC", {}),
    "ek_addons": ("EKS add-ons to install, comma-separated. Example: vpc-cni, coredns, kube-proxy, eks-pod-identity-agent",
                  "addons[].name", {}),
    "ek_logging": ("Control-plane log types sent to CloudWatch. Example: api, audit, authenticator",
                   "cloudWatch.clusterLogging.enableTypes", {}),
},
"GKE": {
    "gk_mode": ("Cluster mode.", "gcloud container clusters create / create-auto",
                {"Standard": "you manage node pools", "Autopilot": "Google manages nodes; pay per pod"}),
    "gk_name": ("GKE cluster name (lowercase, up to 40 characters). Example: dev", "", {}),
    "gk_project": ("GCP project ID the cluster is billed to and created in. Example: my-proj-123", "", {}),
    "gk_location": ("Region (regional cluster, 3 zones) or zone (zonal). Example: us-central1", "", {}),
    "gk_channel": ("Release channel controlling automatic upgrades.", "",
                   {"regular": "default balance", "rapid": "newest versions first", "stable": "most tested",
                    "extended": "longer support (paid)", "None": "no channel, upgrade manually (Standard only)"}),
    "gk_version": ("Exact cluster version; blank = channel default. Example: 1.34.1-gke.1000", "", {}),
    "gk_labels": ("GCP resource labels, k=v comma-separated. Example: env=dev", "", {}),
    "gk_machine": ("Compute Engine machine type for nodes. Example: e2-standard-4", "", {}),
    "gk_nodes": ("Nodes per zone (regional cluster with 3 zones x 1 = 3 nodes). Example: 1", "", {}),
    "gk_node_locations": ("Zones for nodes, comma-separated. Example: us-central1-a,us-central1-b", "", {}),
    "gk_autoscale": ("Enable the cluster autoscaler for the default pool. Example: tick", "", {}),
    "gk_min": ("Minimum nodes per zone when autoscaling. Example: 1", "", {}),
    "gk_max": ("Maximum nodes per zone when autoscaling. Example: 3", "", {}),
    "gk_disk_type": ("Node boot disk type.", "",
                     {"pd-balanced": "balanced SSD (default)", "pd-standard": "HDD", "pd-ssd": "fast SSD",
                      "hyperdisk-balanced": "Hyperdisk balanced (newer machine types)",
                      "hyperdisk-extreme": "Hyperdisk highest IOPS", "hyperdisk-throughput": "Hyperdisk throughput"}),
    "gk_disk_size": ("Boot disk size of each node in GB (images and emptyDirs). Example: 100", "", {}),
    "gk_image": ("Node OS image.", "",
                 {"COS_CONTAINERD": "Container-Optimized OS (default)", "UBUNTU_CONTAINERD": "Ubuntu",
                  "WINDOWS_LTSC_CONTAINERD": "Windows Server LTSC"}),
    "gk_spot": ("Use Spot VMs (cheap, preemptible). Example: tick for batch", "", {}),
    "gk_node_labels": ("Kubernetes node labels, k=v comma-separated. Example: role=web", "", {}),
    "gk_node_taints": ("Node taints, key=value:Effect comma-separated. Example: gpu=true:NoSchedule", "", {}),
    "gk_network": ("VPC network the nodes attach to. Example: default", "", {}),
    "gk_subnetwork": ("Subnet in that VPC for the nodes (region of the cluster). Example: default", "", {}),
    "gk_pod_cidr": ("Pod IP range (VPC-native). Example: 10.4.0.0/14", "", {}),
    "gk_svc_cidr": ("ClusterIP range for Services (VPC-native). Example: 10.8.0.0/20", "", {}),
    "gk_dpv2": ("Dataplane V2 (eBPF/Cilium-based networking and NetworkPolicy). Example: tick", "", {}),
    "gk_private": ("Nodes get only internal IPs. Example: tick (needs Cloud NAT for egress)", "", {}),
    "gk_master_cidr": ("/28 range for the control-plane peering (private clusters). Example: 172.16.0.0/28", "", {}),
    "gk_authorized": ("CIDRs allowed to reach the control plane. Example: 203.0.113.0/24", "", {}),
    "gk_workload_identity": ("Let Kubernetes service accounts act as Google service accounts "
                             "(--workload-pool=<project>.svc.id.goog). Example: tick", "", {}),
    "gk_shielded": ("Shielded GKE nodes (secure boot, integrity monitoring). Example: tick", "", {}),
    "gk_addons": ("Add-ons, comma-separated (Standard mode). Example: HttpLoadBalancing,GcePersistentDiskCsiDriver", "", {}),
},
"AKS": {
    "ak_name": ("AKS cluster name (unique within the resource group). Example: dev-aks", "", {}),
    "ak_rg": ("Resource group the cluster goes in. Example: rg-dev", "", {}),
    "ak_create_rg": ("Also emit `az group create` before the cluster. Example: tick for a new group",
                     "az group create", {}),
    "ak_location": ("Azure region the cluster is created in. Example: westeurope", "", {}),
    "ak_version": ("Kubernetes version; blank = AKS default. Example: 1.34", "", {}),
    "ak_tier": ("Control-plane pricing tier.", "",
                {"free": "no SLA", "standard": "financially backed uptime SLA",
                 "premium": "SLA plus long-term support"}),
    "ak_tags": ("Azure tags, space-separated k=v. Example: env=dev team=web", "", {}),
    "ak_pool": ("Name of the system node pool (lowercase, <=12 chars). Example: system", "", {}),
    "ak_vm": ("VM size for nodes. Example: Standard_D4s_v5", "", {}),
    "ak_count": ("Number of nodes the system pool starts with. Example: 3", "", {}),
    "ak_zones": ("Availability zones, space-separated. Example: 1 2 3", "", {}),
    "ak_os_sku": ("Node OS.", "",
                  {"Ubuntu": "default Ubuntu", "AzureLinux": "Azure Linux (Mariner)", "AzureLinux3": "Azure Linux 3",
                   "Ubuntu2204": "Ubuntu 22.04", "Ubuntu2404": "Ubuntu 24.04",
                   "AzureContainerLinux": "Azure Container Linux (immutable)"}),
    "ak_autoscale": ("Enable the cluster autoscaler on the pool. Example: tick", "", {}),
    "ak_min": ("Minimum nodes when autoscaling. Example: 1", "", {}),
    "ak_max": ("Maximum nodes when autoscaling. Example: 5", "", {}),
    "ak_max_pods": ("Maximum pods per node (fixed at pool creation). Example: 110", "", {}),
    "ak_labels": ("Node labels, space-separated k=v. Example: role=system", "", {}),
    "ak_taints": ("Node taints, comma-separated key=value:Effect. Example: CriticalAddonsOnly=true:NoSchedule", "", {}),
    "ak_plugin": ("Network plugin.", "",
                  {"azure": "Azure CNI (pods get VNet or overlay IPs)", "kubenet": "basic, being retired",
                   "none": "bring your own CNI"}),
    "ak_overlay": ("Azure CNI Overlay: pod IPs from a private CIDR, not the VNet. Example: tick", "", {}),
    "ak_dataplane": ("Network dataplane.", "", {"azure": "default", "cilium": "Azure CNI powered by Cilium (eBPF)"}),
    "ak_policy": ("NetworkPolicy engine.", "",
                  {"none": "no NetworkPolicy enforcement", "azure": "Azure Network Policy Manager",
                   "cilium": "Cilium (needs cilium dataplane)", "calico": "Calico"}),
    "ak_pod_cidr": ("Pod CIDR (overlay/kubenet). Example: 192.168.0.0/16", "", {}),
    "ak_svc_cidr": ("ClusterIP range for Services; must not overlap the VNet. Example: 10.0.0.0/16", "", {}),
    "ak_dns_ip": ("CoreDNS service IP, inside the service CIDR. Example: 10.0.0.10", "", {}),
    "ak_outbound": ("How nodes reach the internet.", "",
                    {"loadBalancer": "Standard LB SNAT (default)", "managedNATGateway": "AKS-managed NAT gateway",
                     "userAssignedNATGateway": "your NAT gateway on the subnet",
                     "userDefinedRouting": "your route table / firewall", "none": "no outbound configured"}),
    "ak_private": ("Private API server (private endpoint only). Example: tick", "", {}),
    "ak_authorized": ("CIDRs allowed to reach the public API server. Example: 203.0.113.0/24", "", {}),
    "ak_oidc": ("Enable the OIDC issuer (needed for workload identity). Example: tick", "", {}),
    "ak_wi": ("Let pods use Entra workload identities. Example: tick (needs OIDC issuer)", "", {}),
    "ak_aad": ("Entra ID (AAD) integration for kubectl logins. Example: tick", "", {}),
    "ak_azure_rbac": ("Authorize Kubernetes access with Azure RBAC roles. Example: tick (needs Entra ID)", "", {}),
    "ak_upgrade": ("Automatic cluster upgrade channel.", "",
                   {"none": "manual upgrades", "patch": "latest patch of current minor",
                    "stable": "minor N-1, latest patch", "rapid": "latest minor", "node-image": "node images only"}),
    "ak_node_upgrade": ("Automatic node OS update channel.", "",
                        {"NodeImage": "weekly node image", "SecurityPatch": "security patches only",
                         "Unmanaged": "OS's own updates", "None": "no automatic OS updates"}),
    "ak_addons": ("Add-ons, comma-separated. Example: monitoring,azure-keyvault-secrets-provider", "", {}),
    "ak_ssh_key": ("SSH public key or path for node access; blank = --generate-ssh-keys. Example: ~/.ssh/id_ed25519.pub",
                   "", {}),
},
"kubeconfig": {
    "cluster_name": ("Name of the clusters: entry that contexts refer to. Example: lab", "clusters[0].name", {}),
    "server": ("API server URL. Example: https://10.0.0.10:6443", "clusters[0].cluster.server", {}),
    "ca_mode": ("How the client verifies the server certificate.", "clusters[0].cluster",
                {"certificate (path, PEM or base64)": "embed/point to the cluster CA (recommended)",
                 "insecure-skip-tls-verify": "no verification (labs only)",
                 "none (system CAs)": "trust OS CA bundle (publicly signed API certs)"}),
    "ca": ("Cluster CA: file path, PEM text or base64. Example: /etc/kubernetes/pki/ca.crt",
           "certificate-authority / certificate-authority-data", {}),
    "tls_server_name": ("Name checked against the server cert when it differs from the URL host. Example: kubernetes",
                        "clusters[0].cluster.tls-server-name", {}),
    "proxy_url": ("HTTP(S)/SOCKS5 proxy for API requests. Example: socks5://localhost:1080",
                  "clusters[0].cluster.proxy-url", {}),
    "user_name": ("Name of the users: entry that contexts refer to. Example: admin", "users[0].name", {}),
    "auth_method": ("How the client authenticates.", "users[0].user",
                    {"token": "bearer token (service account / OIDC)", "client certificate": "mTLS cert + key",
                     "username/password": "basic auth (rarely enabled)",
                     "exec plugin": "run a command to fetch credentials (aws, gke-gcloud-auth-plugin, kubelogin)",
                     "none": "anonymous"}),
    "token": ("Bearer token. Example: eyJhbGciOiJSUzI1NiIs (a JWT)", "users[0].user.token", {}),
    "client_cert": ("Client certificate: path, PEM or base64.", "client-certificate / client-certificate-data", {}),
    "client_key": ("Client private key: path, PEM or base64.", "client-key / client-key-data", {}),
    "username": ("Username for HTTP basic auth (only if the API server enables it). Example: admin", "users[0].user.username", {}),
    "password": ("Basic-auth password (stored in plain text).", "users[0].user.password", {}),
    "exec_api_version": ("ExecCredential API version the plugin speaks.", "users[0].user.exec.apiVersion",
                         {"client.authentication.k8s.io/v1": "current", "client.authentication.k8s.io/v1beta1": "older plugins"}),
    "exec_command": ("Credential plugin binary kubectl runs (must be on PATH). Example: aws", "users[0].user.exec.command", {}),
    "exec_args": ("Plugin arguments, one per line. Example: eks / get-token / --cluster-name / dev",
                  "users[0].user.exec.args", {}),
    "exec_env": ("Environment for the plugin, NAME=value per line. Example: AWS_PROFILE=dev", "users[0].user.exec.env", {}),
    "exec_interactive": ("Whether the plugin may prompt on the terminal.", "users[0].user.exec.interactiveMode",
                         {"IfAvailable": "prompt if stdin is a terminal", "Never": "never prompt",
                          "Always": "require a terminal"}),
    "exec_provide_cluster_info": ("Pass cluster info to the plugin via KUBERNETES_EXEC_INFO. Example: untick usually",
                                  "users[0].user.exec.provideClusterInfo", {}),
    "context_name": ("Context name tying cluster + user + namespace. Example: lab-admin", "contexts[0].name", {}),
    "namespace": ("Default namespace for this context. Example: dev", "contexts[0].context.namespace", {}),
    "set_current": ("Make this context the current-context. Example: tick", "current-context", {}),
},
})


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
PLACEHOLDER_RE = re.compile(r"\b(TODO|TBD|FIXME|lorem|ipsum|xxx)\b|\?\?\?|\.\.\.\s*$", re.I)
_MAIN_C = "<pod>.containers[0]."


def _fill(path: str, kind: str) -> str:
    return (path.replace("<pod>", POD_PATH.get(kind, POD_PATH["_"]))
                .replace("<wl>", WL_PATH.get(kind, WL_PATH["_"])))


def _strip_flag(label: str) -> str:
    return re.sub(r"\s*\(.*\)\s*$", "", label).strip()


def manifest_doc(kind: str, key: str, table: str | None = None):
    """(description, yaml path, option help) for a manifest field or table column, or None."""
    if table:
        for cand in (f"{kind}.{table}.{key}", f"{table}.{key}",
                     f"pod_affinity.{key}" if table == "pod_anti_affinity" else None):
            if cand and cand in M:
                d, p, o = M[cand]
                if table == "pod_anti_affinity":
                    p = p.replace("podAffinity", "podAntiAffinity")
                return d, p, o
        if table == "extra_containers" and key in M:          # sidecar/init columns reuse main-container docs
            d, p, o = M[key]
            p = p.replace(_MAIN_C, "containers[] / initContainers[].").replace("<pod>.", "")
            return d, p, o
        return None
    for cand in (f"{kind}.{key}", key):
        if cand in M:
            d, p, o = M[cand]
            return d, _fill(p, kind), o
    return None


def _disable_doc(key: str, label: str):
    comp = DISABLE_COMPONENTS.get(label, label)
    dist = "RKE2" if key.startswith("rke2_") else "k3s"
    ex = DISABLE_EXAMPLES.get(label.replace("rke2-", ""), "tick when you deploy your own replacement")
    return (f"Don't deploy {comp}, which {dist} installs by default; tick it when you bring your own "
            f"replacement. Example: {ex}", f"config.yaml: disable: [{label}]", {})


DISABLE_EXAMPLES = {
    "traefik": "tick to use ingress-nginx instead", "servicelb": "tick when you install MetalLB or kube-vip",
    "local-storage": "tick when Longhorn / a CSI driver provides storage",
    "metrics-server": "tick when kube-prometheus-stack ships its own", "coredns": "tick only if you run another DNS",
    "snapshot-controller": "tick when your CSI driver bundles one",
    "snapshot-controller-crd": "tick when the CRDs are installed by another chart",
    "snapshot-validation-webhook": "tick when your CSI bundle includes it",
    "security-responder": "tick on air-gapped nodes", "gateway-api-crd": "tick when you manage Gateway API CRDs yourself",
}


def cluster_doc(ctype: str, key: str, label: str = "", table: str | None = None):
    docs = C.get(ctype, {})
    k = f"{table}.{key}" if table else key
    if k in docs:
        d, p, o = docs[k]
    elif re.match(r"(k3s|rke2)_disable_", key):
        d, p, o = _disable_doc(key, label)
    else:
        return None
    if not p and ctype in MAP_TEMPLATE and not table:
        lab = _strip_flag(label)
        p = MAP_TEMPLATE[ctype].format(label=lab, flag=lab.lstrip("-"))
    return d, p, o


def _set(f, doc):
    if doc:
        f.description, f.maps_to, f.option_help = doc[0], doc[1], dict(doc[2])


def apply_manifest(schema: dict) -> None:
    for kind, sections in schema.items():
        for _title, fields in sections:
            for f in fields:
                _set(f, manifest_doc(kind, f.key))
                for c in f.columns or []:
                    _set(c, manifest_doc(kind, c.key, table=f.key))


def apply_cluster(schema: dict) -> None:
    for ctype, sections in schema.items():
        for _title, fields in sections:
            for f in fields:
                _set(f, cluster_doc(ctype, f.key, f.label))
                for c in f.columns or []:
                    _set(c, cluster_doc(ctype, c.key, c.label, table=f.key))


ADDON_SCOPES: set = set()


def addon_doc(atype: str, key: str, table: str | None = None, param: str | None = None):
    """(description, maps_to, option help) for a Networking & Storage field / column, or None."""
    from addons import docs as _ad
    k = f"{table}.{key}" if table else key
    for src in (_ad.TYPE_OVERRIDES.get((atype, k)), _ad.A.get(atype, {}).get(k),
                _ad.CSI_FIELDS.get(k) if atype not in _ad.A else None,
                _ad.CSI_COMMON.get(k) if atype not in _ad.A else None):
        if src:
            d, p, o = src
            if not p and param:
                p = f"StorageClass parameters.{param}"
            return d, p, o
    return None


def apply_addons(schema: dict) -> None:
    """Hover help for the Networking & Storage mode (texts in addons/docs.py)."""
    from addons import csi as _csi
    ADDON_SCOPES.update(schema)
    for atype, sections in schema.items():
        drv = _csi.DRIVERS.get(atype)
        pmap = {p.key: p.param for p in drv.params} if drv else {}
        for _title, fields in sections:
            for f in fields:
                _set(f, addon_doc(atype, f.key, param=pmap.get(f.key)))
                for c in f.columns or []:
                    _set(c, addon_doc(atype, c.key, table=f.key))


def apply_param(p) -> None:
    if p.key in TEMPLATE_PARAMS:
        p.description, p.maps_to = TEMPLATE_PARAMS[p.key]
        p.option_help = dict(TEMPLATE_PARAM_OPTIONS.get(p.key, {}))


def tooltip(f, max_options: int = 12) -> str:
    """Hover text for a Field or template Param: what/why, options, format, YAML path."""
    lines = [f.label.strip() or f.key]
    desc = (getattr(f, "description", "") or "").strip()
    if desc:
        lines.append(desc)
    oh = getattr(f, "option_help", {}) or {}
    shown = 0
    for opt in (f.options or list(oh)):
        if opt in oh and shown < max_options:
            lines.append(f"• {opt if opt != '' else '(blank)'} – {oh[opt]}")
            shown += 1
    hlp = (getattr(f, "help", "") or "").strip()
    if hlp and hlp.lower() not in desc.lower():
        lines.append(f"Format: {hlp}")
    mp = (getattr(f, "maps_to", "") or "").strip()
    if mp:
        lines.append(("Maps to: " if any(s in mp for s in (" ", "--", ":")) and not mp.startswith(("spec", "metadata", "<"))
                      else "YAML: ") + mp)
    return "\n".join(lines)


def section_help(scope: str, title: str) -> str:
    if scope in ADDON_SCOPES:
        from addons import docs as _ad
        return _ad.section_help(scope, title)
    return SECTIONS.get(f"{scope}:{title}") or SECTIONS.get(title, "")


def top_help(key: str) -> str:
    t, d = TOP[key]
    return f"{t}\n{d}"


def iter_fields(gen_schema: dict, cluster_schema: dict, addon_schema: dict | None = None):
    """Yield (mode, scope, section, field, table_key) for every field and table column."""
    modes = [("manifest", gen_schema), ("cluster", cluster_schema)]
    if addon_schema is not None:
        modes.append(("addon", addon_schema))
    for mode, schema in modes:
        for scope, sections in schema.items():
            for title, fields in sections:
                for f in fields:
                    yield mode, scope, title, f, None
                    for c in f.columns or []:
                        yield mode, scope, title, c, f.key


# --------------------------------------------------------------------------- NetworkPolicy / StorageClass /
# VolumeSnapshotClass kinds (Manifests mode)
_NP_PEER = {"pods (podSelector)": "pods with the labels below, in this policy's namespace",
            "namespaces (namespaceSelector)": "every pod in namespaces with the namespace labels below",
            "pods in namespaces": "pods with the pod labels inside namespaces with the namespace labels",
            "ipBlock (CIDR)": "an address range (outside the cluster, or node IPs); pod IPs are ephemeral",
            "any (all peers)": "no from/to restriction: only the ports below are limited"}
_NP_COL = {
    "peer": ("Who the rule allows traffic from (ingress) or to (egress).", "{d}[].{p}[]", _NP_PEER),
    "pod_labels": ("Pod labels for podSelector peers, comma-separated key=value; blank = all pods. Example: "
                   "app=frontend", "{d}[].{p}[].podSelector.matchLabels", {}),
    "ns_labels": ("Namespace labels for namespaceSelector peers; blank = all namespaces. Example: "
                  "kubernetes.io/metadata.name=monitoring", "{d}[].{p}[].namespaceSelector.matchLabels", {}),
    "cidr": ("CIDR for ipBlock peers. Example: 10.0.0.0/8 or 0.0.0.0/0", "{d}[].{p}[].ipBlock.cidr", {}),
    "except_cidrs": ("Ranges inside the CIDR to exclude, comma-separated. Example: 169.254.169.254/32",
                     "{d}[].{p}[].ipBlock.except[]", {}),
    "ports": ("Allowed ports, comma-separated port[/protocol]; ranges and named ports work; blank = all ports. "
              "Example: 80/TCP, 443, 30000-32767/TCP, 53/UDP", "{d}[].ports[] (port, endPort, protocol)", {}),
    "rule": ("Rows with the same rule number merge into one rule (peers OR-ed, ports shared); blank = its own "
             "rule. Example: 1", "{d}[] grouping", {}),
}
for _d, _p, _t in (("ingress", "from", "ingress_rules"), ("egress", "to", "egress_rules")):
    for _k, (_desc, _path, _opts) in _NP_COL.items():
        M[f"{_t}.{_k}"] = (_desc, "spec." + _path.format(d=_d, p=_p), _opts)
M.update({
    "NetworkPolicy.pod_selector": ("Pods this policy applies to, one key=value per line; blank = every pod in the "
                                   "namespace. Example: app=web", "spec.podSelector.matchLabels", {}),
    "policy_ingress": ("Isolate selected pods for incoming traffic: only the ingress rules (or presets) are "
                       "allowed in.", "spec.policyTypes: Ingress", {}),
    "policy_egress": ("Isolate selected pods for outgoing traffic: only the egress rules are allowed out "
                      "(remember DNS).", "spec.policyTypes: Egress", {}),
    "np_default_deny": ("Deny everything in the chosen direction(s) not allowed by other rules - the usual "
                        "first policy of a namespace (combine with an empty pod selector).",
                        "spec.policyTypes with no rules",
                        {"": "no default-deny preset", "ingress": "deny all incoming traffic",
                         "egress": "deny all outgoing traffic", "all": "deny both directions"}),
    "np_allow_same_ns": ("Allow traffic from every pod in the same namespace (adds an ingress rule with an "
                         "empty podSelector).", "spec.ingress[].from[].podSelector: {}", {}),
    "np_allow_namespaces": ("Also allow ingress from these namespaces (comma-separated names). Example: "
                            "monitoring, ingress-nginx",
                            "spec.ingress[].from[].namespaceSelector kubernetes.io/metadata.name In [...]", {}),
    "np_allow_dns": ("Allow DNS egress to kube-dns / CoreDNS in kube-system on 53/UDP + 53/TCP - needed by "
                     "almost every egress policy.", "spec.egress[] to k8s-app=kube-dns port 53", {}),
    "ingress_rules": ("Allowed incoming traffic; one row per peer, rows with the same rule number merge.",
                      "spec.ingress[]", {}),
    "egress_rules": ("Allowed outgoing traffic; one row per peer, rows with the same rule number merge.",
                     "spec.egress[]", {}),
    "provisioner": ("CSI driver (or in-tree provisioner) that creates volumes for this class. The Networking & "
                    "Storage mode has per-driver forms. Example: ebs.csi.aws.com", "provisioner", {}),
    "default_class": ("Mark as the cluster's default StorageClass (used by PVCs without storageClassName); only "
                      "one class should be default.",
                      "metadata.annotations: storageclass.kubernetes.io/is-default-class", {}),
    "StorageClass.parameters": ("Driver-specific parameters, one key=value per line. Example: type=gp3",
                                "parameters", {}),
    "reclaim_policy": ("What happens to the volume when its PVC is deleted.", "reclaimPolicy",
                       {"Delete": "delete the backing volume with the PVC (default)",
                        "Retain": "keep the volume; an admin cleans up / re-binds the PV"}),
    "binding_mode": ("When volumes are provisioned.", "volumeBindingMode",
                     {"WaitForFirstConsumer": "wait for the pod's scheduling so the volume lands in its zone / node",
                      "Immediate": "provision as soon as the PVC exists"}),
    "allow_expansion": ("Allow PVCs of this class to grow by editing their storage request (driver must "
                        "support resizing).", "allowVolumeExpansion", {}),
    "StorageClass.mount_options": ("Mount options for volumes of this class, one per line or comma-separated. "
                                   "Example: nfsvers=4.1", "mountOptions[]", {}),
    "allowed_topologies": ("Restrict where volumes can be provisioned (zones / nodes); empty = anywhere.",
                           "allowedTopologies[].matchLabelExpressions[]", {}),
    "allowed_topologies.key": ("Topology label key. Example: topology.kubernetes.io/zone",
                               "allowedTopologies[].matchLabelExpressions[].key", {}),
    "allowed_topologies.values": ("Allowed values, comma-separated. Example: us-east-1a, us-east-1b",
                                  "allowedTopologies[].matchLabelExpressions[].values", {}),
    "driver": ("CSI driver that takes the snapshots; must match the StorageClass provisioner. Example: "
               "ebs.csi.aws.com", "driver", {}),
    "deletion_policy": ("What happens to the storage snapshot when the VolumeSnapshot is deleted.",
                        "deletionPolicy", {"Delete": "delete the backing snapshot too",
                                           "Retain": "keep it (VolumeSnapshotContent stays)"}),
    "default_snapshot_class": ("Mark as the default VolumeSnapshotClass for this driver.",
                               "metadata.annotations: snapshot.storage.kubernetes.io/is-default-class", {}),
    "VolumeSnapshotClass.parameters": ("Driver-specific snapshot parameters, one key=value per line. Example: "
                                       "incremental=true", "parameters", {}),
})
SECTIONS.update({
    "Policy": "Which pods the NetworkPolicy selects and which directions (Ingress / Egress) it isolates.",
    "Quick presets": "Common rules in one click: default-deny, allow same namespace / namespaces, allow DNS.",
    "Ingress rules": "Allowed incoming traffic: peers (pods, namespaces, CIDRs) and ports.",
    "Egress rules": "Allowed outgoing traffic: peers (pods, namespaces, CIDRs) and ports.",
    "StorageClass": "Provisioner, parameters, reclaim / binding policy, expansion and mount options.",
    "Allowed topologies": "Restrict provisioning to some zones / nodes (allowedTopologies).",
    "VolumeSnapshotClass": "CSI driver, deletion policy, default flag and parameters for VolumeSnapshots.",
})
TOP["mode"] = ("Mode", "Switch between Manifests (Kubernetes resources: Deployments, Services, NetworkPolicies...), "
                       "Cluster config (files to create or connect to a cluster: kind, kubeadm, k3s, EKS, "
                       "kubeconfig...) and Networking & Storage (CNI plugins such as Calico / Cilium and CSI "
                       "StorageClasses with install scripts). Values are kept per type when you switch.")
TOP["kind"] = ("Resource type / Config type / Add-on type",
               "Pick which kind (Manifests), platform (Cluster config) or CNI / CSI add-on (Networking & Storage) "
               "to edit; only that form is shown. Each keeps its own values until you Reset it. Group headers "
               "(── CNI ──) jump to the group's first entry. Shortcut: Ctrl/Cmd+K.")
