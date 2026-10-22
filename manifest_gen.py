"""
manifest_gen.py - headless Kubernetes manifest generator.

This module has NO GUI dependency so it can be imported and tested on a
server / CI box. PyYAML is only needed for to_yaml(); build/validate work
without it.

Public API
----------
KINDS                     list of supported kinds (selector order)
SCHEMA[kind]              list of (section_title, [Field, ...]) used to build the GUI
defaults(kind)            dict of default field values for a kind
generate(kind, values)    -> Result(manifest: dict | None, errors: list[str], warnings: list[str])
to_yaml(manifest)         -> YAML string (single document)
to_multi_yaml(manifests)  -> multi-document YAML string ("---" separated)
filename_for(manifest)    -> "<kind-lower>-<name>.yaml"
"""
from __future__ import annotations

import base64
import binascii
import copy
import ipaddress
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "KINDS", "API_VERSIONS", "SCHEMA", "Field", "Result", "defaults", "generate",
    "to_yaml", "to_multi_yaml", "filename_for", "restart_policies_for",
    "TABLE_FIELDS", "row_defaults", "check_row",
]

# --------------------------------------------------------------------------- #
# Constants / dropdown values
# --------------------------------------------------------------------------- #
KINDS = [
    "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Service",
    "Ingress", "ConfigMap", "Secret", "PersistentVolumeClaim", "Namespace",
    "NetworkPolicy", "StorageClass", "VolumeSnapshotClass",
]
API_VERSIONS = {
    "Deployment": "apps/v1", "StatefulSet": "apps/v1", "DaemonSet": "apps/v1",
    "Job": "batch/v1", "CronJob": "batch/v1", "Service": "v1",
    "Ingress": "networking.k8s.io/v1", "ConfigMap": "v1", "Secret": "v1",
    "PersistentVolumeClaim": "v1", "Namespace": "v1",
    "NetworkPolicy": "networking.k8s.io/v1", "StorageClass": "storage.k8s.io/v1",
    "VolumeSnapshotClass": "snapshot.storage.k8s.io/v1",
}
CLUSTER_SCOPED = ("Namespace", "StorageClass", "VolumeSnapshotClass")
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")

IMAGE_PULL_POLICIES = ["IfNotPresent", "Always", "Never"]
_RESTART = {
    "Deployment": ["Always"], "StatefulSet": ["Always"], "DaemonSet": ["Always"],
    "Job": ["OnFailure", "Never"], "CronJob": ["OnFailure", "Never"],
}
SERVICE_TYPES = ["ClusterIP", "NodePort", "LoadBalancer", "ExternalName"]
SECRET_TYPES = [
    "Opaque", "kubernetes.io/dockerconfigjson", "kubernetes.io/tls",
    "kubernetes.io/basic-auth", "kubernetes.io/ssh-auth",
    "kubernetes.io/service-account-token",
]
ACCESS_MODES = ["ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany", "ReadWriteOncePod"]
STORAGE_CLASSES = ["", "standard", "default", "gp2", "gp3", "local-path", "longhorn",
                   "nfs-client", "managed-csi", "standard-rwo", "premium-rwo",
                   "nutanix-volume", "hostpath"]
VOLUME_MODES = ["Filesystem", "Block"]
PROTOCOLS = ["TCP", "UDP", "SCTP"]
DEPLOY_STRATEGIES = ["RollingUpdate", "Recreate"]
STS_DS_STRATEGIES = ["RollingUpdate", "OnDelete"]
CONCURRENCY_POLICIES = ["Allow", "Forbid", "Replace"]
PATH_TYPES = ["Prefix", "Exact", "ImplementationSpecific"]
INGRESS_CLASSES = ["", "nginx", "traefik", "haproxy", "alb", "gce", "kong", "istio", "cilium"]
POD_MANAGEMENT = ["OrderedReady", "Parallel"]
EXT_TRAFFIC = ["", "Cluster", "Local"]

# multi-container + scheduling
ROLE_MAIN, ROLE_SIDECAR, ROLE_NATIVE, ROLE_INIT = "main", "sidecar (regular container)", "sidecar (native)", "init"
CONTAINER_ROLES = [ROLE_SIDECAR, ROLE_NATIVE, ROLE_INIT, ROLE_MAIN]
TOLERATION_OPERATORS = ["Equal", "Exists"]
TOLERATION_EFFECTS = ["", "NoSchedule", "PreferNoSchedule", "NoExecute"]
NODE_OPERATORS = ["In", "NotIn", "Exists", "DoesNotExist", "Gt", "Lt"]
SELECTOR_OPERATORS = ["In", "NotIn", "Exists", "DoesNotExist"]
AFFINITY_TYPES = ["required", "preferred"]
TOPOLOGY_KEYS = ["kubernetes.io/hostname", "topology.kubernetes.io/zone", "topology.kubernetes.io/region"]
NODE_LABEL_KEYS = ["", "kubernetes.io/hostname", "kubernetes.io/os", "kubernetes.io/arch",
                   "node.kubernetes.io/instance-type", "topology.kubernetes.io/zone",
                   "topology.kubernetes.io/region", "node-role.kubernetes.io/control-plane"]
TAINT_KEYS = ["", "node-role.kubernetes.io/control-plane", "node.kubernetes.io/not-ready",
              "node.kubernetes.io/unreachable", "node.kubernetes.io/disk-pressure",
              "node.kubernetes.io/memory-pressure", "node.kubernetes.io/unschedulable", "nvidia.com/gpu",
              "dedicated"]
WHEN_UNSATISFIABLE = ["DoNotSchedule", "ScheduleAnyway"]

# network policies + storage classes
NP_PEERS = ["pods (podSelector)", "namespaces (namespaceSelector)", "pods in namespaces", "ipBlock (CIDR)",
            "any (all peers)"]
NP_DEFAULT_DENY = ["", "ingress", "egress", "all"]
RECLAIM_POLICIES = ["Delete", "Retain"]
BINDING_MODES = ["WaitForFirstConsumer", "Immediate"]
DELETION_POLICIES = ["Delete", "Retain"]
PROVISIONERS = ["ebs.csi.aws.com", "efs.csi.aws.com", "pd.csi.storage.gke.io", "disk.csi.azure.com",
                "file.csi.azure.com", "csi.vsphere.vmware.com", "csi.nutanix.com", "driver.longhorn.io",
                "rook-ceph.rbd.csi.ceph.com", "rook-ceph.cephfs.csi.ceph.com", "openebs.io/local",
                "nfs.csi.k8s.io", "smb.csi.k8s.io", "rancher.io/local-path", "topolvm.io",
                "kubernetes.io/no-provisioner"]
SNAPSHOT_DRIVERS = ["ebs.csi.aws.com", "pd.csi.storage.gke.io", "disk.csi.azure.com", "file.csi.azure.com",
                    "csi.vsphere.vmware.com", "csi.nutanix.com", "driver.longhorn.io", "rook-ceph.rbd.csi.ceph.com",
                    "rook-ceph.cephfs.csi.ceph.com", "nfs.csi.k8s.io", "hostpath.csi.k8s.io"]
TOPOLOGY_LABEL_KEYS = ["topology.kubernetes.io/zone", "topology.kubernetes.io/region", "kubernetes.io/hostname",
                       "topology.ebs.csi.aws.com/zone", "topology.gke.io/zone", "topology.disk.csi.azure.com/zone"]


def restart_policies_for(kind: str) -> list[str]:
    return list(_RESTART.get(kind, ["Always"]))


# --------------------------------------------------------------------------- #
# Field schema (drives the GUI)
# --------------------------------------------------------------------------- #
@dataclass
class Field:
    key: str
    label: str
    kind: str = "entry"          # entry | combo (readonly) | ecombo (editable) | text | check | table
    default: Any = ""
    options: list = field(default_factory=list)
    help: str = ""
    required: bool = False
    height: int = 3
    columns: list = field(default_factory=list)   # kind == "table": sub-fields of one row
    summary: list = field(default_factory=list)   # kind == "table": column keys shown in the list
    # hover help (filled in by field_docs.apply_*): what it is/does, YAML path or config key, per-option notes
    description: str = ""
    maps_to: str = ""
    option_help: dict = field(default_factory=dict)


def _meta_fields(kind: str, default_name: str) -> list[Field]:
    f = [Field("name", "Name", default=default_name, required=True,
               help="DNS-1123 name, lowercase a-z 0-9 '-'")]
    if kind not in CLUSTER_SCOPED:
        f.append(Field("namespace", "Namespace", default="default",
                       help="blank = omit (uses current kubectl namespace)"))
    f += [
        Field("labels", "Labels", "text",
              default=f"app={default_name}" if kind not in CLUSTER_SCOPED + ("NetworkPolicy",) else "",
              help="key=value, one per line (or comma-separated)"),
        Field("annotations", "Annotations", "text", default="", help="key=value, one per line"),
    ]
    return f


def _container_fields(default_image: str, default_ports: str, kind: str) -> list[Field]:
    rp = restart_policies_for(kind)
    return [
        Field("container_name", "Container name", default="", help="blank = resource name"),
        Field("image", "Image", default=default_image, required=True, help="e.g. nginx:1.27, ghcr.io/org/app:v1"),
        Field("image_pull_policy", "imagePullPolicy", "combo", default="IfNotPresent", options=IMAGE_PULL_POLICIES),
        Field("restart_policy", "restartPolicy", "combo", default=rp[0], options=rp,
              help="Jobs/CronJobs cannot use Always" if kind in ("Job", "CronJob") else "only Always is valid for this kind"),
        Field("ports", "Container ports", "text", default=default_ports,
              help="one per line: 8080 | http:8080 | dns:53/UDP"),
        Field("port_protocol", "Default port protocol", "combo", default="TCP", options=PROTOCOLS),
        Field("command", "Command", "text", default="", height=2,
              help="single line is shell-split; multiple lines = one arg per line"),
        Field("args", "Args", "text", default="", height=2, help="same format as Command"),
        Field("env", "Env vars", "text", default="",
              help="KEY=VALUE per line; KEY=@configmap:name/key or KEY=@secret:name/key"),
        Field("env_from", "envFrom", "text", default="", height=2, help="configmap:name or secret:name per line"),
        Field("working_dir", "workingDir", default=""),
    ]


def _resource_fields() -> list[Field]:
    return [
        Field("cpu_request", "CPU request", default="100m", help="e.g. 100m, 0.5, 1"),
        Field("memory_request", "Memory request", default="128Mi", help="e.g. 128Mi, 1Gi"),
        Field("cpu_limit", "CPU limit", default="500m"),
        Field("memory_limit", "Memory limit", default="256Mi"),
    ]


PROBE_TYPES = ["", "httpGet", "tcpSocket", "exec", "grpc"]


def _probe_fields() -> list[Field]:
    return [
        Field("readiness_type", "Readiness probe type", "combo", default="", options=PROBE_TYPES,
              help="blank = httpGet when a path is set, otherwise no probe"),
        Field("readiness_path", "Readiness HTTP path", default="", help="e.g. /healthz (blank = no probe)"),
        Field("readiness_command", "Readiness exec command", "text", default="", height=2,
              help="exec probes; one line is shell-split, e.g. sh -c 'pg_isready -U \"$POSTGRES_USER\"'"),
        Field("liveness_type", "Liveness probe type", "combo", default="", options=PROBE_TYPES),
        Field("liveness_path", "Liveness HTTP path", default=""),
        Field("liveness_command", "Liveness exec command", "text", default="", height=2),
        Field("probe_port", "Probe port", default="", help="number or port name; blank = first container port"),
        Field("readiness_initial_delay", "Readiness initialDelaySeconds", default="", help="blank = 5"),
        Field("liveness_initial_delay", "Liveness initialDelaySeconds", default="", help="blank = 15"),
        Field("probe_period", "periodSeconds", default="", help="blank = 10"),
        Field("probe_timeout", "timeoutSeconds", default="", help="blank = Kubernetes default (1)"),
        Field("probe_failure_threshold", "failureThreshold", default="", help="blank = Kubernetes default (3)"),
    ]


def _volume_fields() -> list[Field]:
    return [Field("volumes", "Volumes / mounts", "text", default="", height=4,
                  help="name:type:source:mountPath[:ro]  types: pvc, configMap, secret, emptyDir, hostPath\n"
                       "e.g. data:pvc:my-claim:/data   cfg:configMap:app-cfg:/etc/app:ro   tmp:emptyDir::/tmp\n"
                       "mountPath = where the MAIN container mounts it; leave blank (shared:emptyDir::) to only define it")]


def _scheduling_fields() -> list[Field]:
    return [
        Field("service_account", "serviceAccountName", default=""),
        Field("fs_group", "securityContext.fsGroup", default="", help="pod-level; makes volumes group-writable"),
        Field("run_as_user", "securityContext.runAsUser", default=""),
        Field("run_as_group", "securityContext.runAsGroup", default=""),
        Field("run_as_non_root", "securityContext.runAsNonRoot", "check", default=False),
        Field("enable_service_links", "enableServiceLinks", "check", default=True,
              help="untick to stop Kubernetes injecting <SERVICE>_PORT-style env vars (they confuse e.g. Kafka images)"),
        Field("node_selector", "nodeSelector", "text", default="", height=2, help="key=value per line"),
        Field("image_pull_secrets", "imagePullSecrets", default="", help="comma-separated secret names"),
        Field("termination_grace", "terminationGracePeriodSeconds", default=""),
    ]


def _extra_container_columns() -> list[Field]:
    return [
        Field("role", "Role", "combo", default=ROLE_SIDECAR, options=CONTAINER_ROLES,
              help="sidecar (native) = initContainer with restartPolicy: Always (K8s 1.29+)"),
        Field("name", "Name", default="", required=True, help="DNS-1123 label, unique in the pod"),
        Field("image", "Image", default="busybox:1.36", required=True),
        Field("image_pull_policy", "imagePullPolicy", "combo", default="IfNotPresent", options=IMAGE_PULL_POLICIES),
        Field("command", "Command", "text", default="", height=2,
              help="single line is shell-split; multiple lines = one arg per line"),
        Field("args", "Args", "text", default="", height=2),
        Field("ports", "Ports", "text", default="", height=2, help="8080 | http:8080 | dns:53/UDP"),
        Field("port_protocol", "Default port protocol", "combo", default="TCP", options=PROTOCOLS),
        Field("env", "Env vars", "text", default="", height=3,
              help="KEY=VALUE; KEY=@configmap:name/key or KEY=@secret:name/key"),
        Field("cpu_request", "CPU request", default=""),
        Field("memory_request", "Memory request", default=""),
        Field("cpu_limit", "CPU limit", default=""),
        Field("memory_limit", "Memory limit", default=""),
        Field("volume_mounts", "volumeMounts", "text", default="", height=2,
              help="volume:/mount/path[:ro] per line; volume must be defined in Volumes"),
        Field("working_dir", "workingDir", default=""),
    ]


def _toleration_columns() -> list[Field]:
    return [
        Field("key", "Key", "ecombo", default="", options=TAINT_KEYS, help="blank + Exists = tolerate every taint"),
        Field("operator", "Operator", "combo", default="Equal", options=TOLERATION_OPERATORS),
        Field("value", "Value", default="", help="must be blank for Exists"),
        Field("effect", "Effect", "combo", default="", options=TOLERATION_EFFECTS, help="blank = all effects"),
        Field("toleration_seconds", "tolerationSeconds", default="", help="NoExecute only"),
    ]


def _node_affinity_columns() -> list[Field]:
    return [
        Field("type", "Type", "combo", default="required", options=AFFINITY_TYPES),
        Field("key", "Node label key", "ecombo", default="", options=NODE_LABEL_KEYS, required=True),
        Field("operator", "Operator", "combo", default="In", options=NODE_OPERATORS),
        Field("values", "Values", default="", help="comma-separated; none for Exists/DoesNotExist; one integer for Gt/Lt"),
        Field("weight", "Weight (preferred)", default="50", help="1-100, preferred only"),
        Field("term", "Term # (required)", default="1",
              help="required rows with the same term # are ANDed; different terms are ORed"),
    ]


def _pod_affinity_columns() -> list[Field]:
    return [
        Field("type", "Type", "combo", default="required", options=AFFINITY_TYPES),
        Field("key", "Pod label key", default="app", required=True),
        Field("operator", "Operator", "combo", default="In", options=SELECTOR_OPERATORS),
        Field("values", "Values", default="", help="comma-separated; none for Exists/DoesNotExist"),
        Field("topology_key", "topologyKey", "ecombo", default="kubernetes.io/hostname", options=TOPOLOGY_KEYS,
              required=True),
        Field("weight", "Weight (preferred)", default="100", help="1-100, preferred only"),
        Field("namespaces", "Namespaces", default="", help="comma-separated; blank = this pod's namespace"),
    ]


def _spread_columns() -> list[Field]:
    return [
        Field("max_skew", "maxSkew", default="1", required=True),
        Field("topology_key", "topologyKey", "ecombo", default="topology.kubernetes.io/zone", options=TOPOLOGY_KEYS,
              required=True),
        Field("when_unsatisfiable", "whenUnsatisfiable", "combo", default="DoNotSchedule", options=WHEN_UNSATISFIABLE),
        Field("match_labels", "Match labels", default="", help="key=value, comma-separated; blank = pod labels"),
    ]


def _np_rule_columns(direction: str) -> list[Field]:
    return [
        Field("peer", "Peer", "combo", default=NP_PEERS[0], options=NP_PEERS),
        Field("pod_labels", "Pod labels", default="", help="key=value, comma-separated; blank = all pods"),
        Field("ns_labels", "Namespace labels", default="",
              help="key=value, comma-separated; blank = all namespaces; "
                   "kubernetes.io/metadata.name=<ns> selects one namespace by name"),
        Field("cidr", "CIDR (ipBlock)", default="", help="e.g. 10.0.0.0/8 or 0.0.0.0/0"),
        Field("except_cidrs", "Except CIDRs", default="", help="comma-separated, inside the CIDR"),
        Field("ports", "Ports", default="", help="port[/PROTO] or start-end[/PROTO], comma-separated; "
                                                  "blank = all ports. e.g. 80/TCP, 443, 5432, 30000-32767/TCP"),
        Field("rule", "Rule #", default="", help="rows with the same number form ONE rule (peers ORed, ports "
                                                 "combined); blank = a rule of its own"),
    ]


TABLE_FIELDS: dict[str, list[Field]] = {
    "extra_containers": _extra_container_columns(),
    "tolerations": _toleration_columns(),
    "node_affinity": _node_affinity_columns(),
    "pod_affinity": _pod_affinity_columns(),
    "pod_anti_affinity": _pod_affinity_columns(),
    "topology_spread": _spread_columns(),
    "ingress_rules": _np_rule_columns("from"),
    "egress_rules": _np_rule_columns("to"),
    "allowed_topologies": [
        Field("key", "Label key", "ecombo", default="topology.kubernetes.io/zone", options=TOPOLOGY_LABEL_KEYS,
              required=True),
        Field("values", "Values", default="", required=True, help="comma-separated, e.g. us-east-1a, us-east-1b"),
    ],
}


def row_defaults(table_key: str) -> dict:
    return {f.key: f.default for f in TABLE_FIELDS[table_key]}


def _table(key, label, summary, help_="", default=None):
    return Field(key, label, "table", default=list(default or []), columns=TABLE_FIELDS[key],
                 summary=summary, help=help_)


def _extra_container_fields() -> list[Field]:
    return [_table("extra_containers", "Containers", ["role", "name", "image"],
                   "Extra containers besides the main one. Init containers run in list order before the app; "
                   "native sidecars start first and keep running (K8s 1.29+).")]


def _advanced_scheduling_sections() -> list:
    return [
        ("Tolerations", [_table("tolerations", "Tolerations", ["key", "operator", "value", "effect", "toleration_seconds"])]),
        ("Node affinity", [_table("node_affinity", "Node affinity", ["type", "key", "operator", "values", "weight", "term"])]),
        ("Pod affinity / anti-affinity", [
            _table("pod_affinity", "Pod affinity", ["type", "key", "operator", "values", "topology_key", "weight"],
                   "co-locate with pods matching the selector"),
            _table("pod_anti_affinity", "Pod anti-affinity", ["type", "key", "operator", "values", "topology_key", "weight"],
                   "keep away from pods matching the selector (e.g. app In <name> on kubernetes.io/hostname)"),
        ]),
        ("Topology spread", [_table("topology_spread", "Constraints",
                                    ["max_skew", "topology_key", "when_unsatisfiable", "match_labels"])]),
    ]


def _job_fields() -> list[Field]:
    return [
        Field("backoff_limit", "backoffLimit", default="6"),
        Field("completions", "completions", default="1"),
        Field("parallelism", "parallelism", default="1"),
        Field("active_deadline", "activeDeadlineSeconds", default=""),
        Field("ttl_after_finished", "ttlSecondsAfterFinished", default=""),
    ]


def _workload_schema(kind: str, name: str, image: str, ports: str, specific: list[Field]):
    return [
        ("Metadata", _meta_fields(kind, name)),
        (f"{kind} spec", specific),
        ("Main container", _container_fields(image, ports, kind)),
        ("Resources", _resource_fields()),
        ("Probes", _probe_fields()),
        ("Volumes", _volume_fields()),
        ("Init & sidecar containers", _extra_container_fields()),
        ("Scheduling", _scheduling_fields()),
    ] + _advanced_scheduling_sections()


SCHEMA: dict[str, list[tuple[str, list[Field]]]] = {
    "Deployment": _workload_schema("Deployment", "my-app", "nginx:1.27", "http:80", [
        Field("replicas", "Replicas", default="2", required=True),
        Field("selector", "Selector labels", "text", default="", height=2, help="blank = use Labels (or app=<name>)"),
        Field("strategy", "Strategy", "combo", default="RollingUpdate", options=DEPLOY_STRATEGIES),
        Field("max_surge", "maxSurge", default="25%", help="RollingUpdate only; int or %"),
        Field("max_unavailable", "maxUnavailable", default="25%"),
        Field("revision_history", "revisionHistoryLimit", default=""),
    ]),
    "StatefulSet": _workload_schema("StatefulSet", "my-db", "postgres:16", "pg:5432", [
        Field("replicas", "Replicas", default="1", required=True),
        Field("service_name", "serviceName (headless svc)", default="my-db", required=True),
        Field("selector", "Selector labels", "text", default="", height=2, help="blank = use Labels (or app=<name>)"),
        Field("strategy", "updateStrategy", "combo", default="RollingUpdate", options=STS_DS_STRATEGIES),
        Field("partition", "RollingUpdate partition", default=""),
        Field("pod_management", "podManagementPolicy", "combo", default="OrderedReady", options=POD_MANAGEMENT),
        Field("vct_name", "volumeClaimTemplate name", default="data", help="blank = no volumeClaimTemplate"),
        Field("vct_mount", "VCT mountPath", default="/var/lib/postgresql/data"),
        Field("vct_size", "VCT size", default="10Gi"),
        Field("vct_access_mode", "VCT access mode", "combo", default="ReadWriteOnce", options=ACCESS_MODES),
        Field("vct_storage_class", "VCT storageClass", "ecombo", default="", options=STORAGE_CLASSES,
              help="blank = cluster default"),
    ]),
    "DaemonSet": _workload_schema("DaemonSet", "node-agent", "fluent/fluent-bit:3.1", "", [
        Field("selector", "Selector labels", "text", default="", height=2, help="blank = use Labels (or app=<name>)"),
        Field("strategy", "updateStrategy", "combo", default="RollingUpdate", options=STS_DS_STRATEGIES),
        Field("max_unavailable", "maxUnavailable", default="1"),
        Field("host_network", "hostNetwork", "check", default=False),
        Field("tolerate_all", "Tolerate all taints (run on control-plane too)", "check", default=False),
    ]),
    "Job": _workload_schema("Job", "my-job", "busybox:1.36", "", _job_fields()),
    "CronJob": _workload_schema("CronJob", "my-cron", "busybox:1.36", "", [
        Field("schedule", "Schedule (cron)", default="*/5 * * * *", required=True,
              help="5-field cron or @hourly/@daily/@weekly/@monthly/@yearly"),
        Field("time_zone", "timeZone", default="", help="e.g. America/Los_Angeles (blank = controller TZ)"),
        Field("concurrency_policy", "concurrencyPolicy", "combo", default="Forbid", options=CONCURRENCY_POLICIES),
        Field("starting_deadline", "startingDeadlineSeconds", default=""),
        Field("successful_history", "successfulJobsHistoryLimit", default="3"),
        Field("failed_history", "failedJobsHistoryLimit", default="1"),
        Field("suspend", "suspend", "check", default=False),
    ] + _job_fields()),
    "Service": [
        ("Metadata", _meta_fields("Service", "my-app")),
        ("Service spec", [
            Field("service_type", "type", "combo", default="ClusterIP", options=SERVICE_TYPES),
            Field("selector", "Selector", "text", default="app=my-app", height=2, help="key=value per line"),
            Field("port_name", "Port name", default="http"),
            Field("port", "port", default="80", required=True),
            Field("target_port", "targetPort", default="80", help="number or container port name"),
            Field("node_port", "nodePort", default="", help="NodePort/LoadBalancer only, 30000-32767; blank = auto"),
            Field("protocol", "protocol", "combo", default="TCP", options=PROTOCOLS),
            Field("extra_ports", "Additional ports", "text", default="", height=3,
                  help="name:port:targetPort[:nodePort][/PROTO] per line"),
            Field("cluster_ip", "clusterIP", "ecombo", default="", options=["", "None"],
                  help="'None' = headless service"),
            Field("external_name", "externalName", default="", help="required for ExternalName, e.g. db.example.com"),
            Field("external_traffic_policy", "externalTrafficPolicy", "combo", default="", options=EXT_TRAFFIC),
            Field("session_affinity", "sessionAffinity", "combo", default="None", options=["None", "ClientIP"]),
            Field("publish_not_ready", "publishNotReadyAddresses", "check", default=False,
                  help="publish DNS for pods before they are Ready (peer discovery behind a headless Service)"),
        ]),
    ],
    "Ingress": [
        ("Metadata", _meta_fields("Ingress", "my-app")),
        ("Ingress spec", [
            Field("ingress_class", "ingressClassName", "ecombo", default="nginx", options=INGRESS_CLASSES),
            Field("host", "Host", default="app.example.com", help="blank = all hosts"),
            Field("path", "Path", default="/"),
            Field("path_type", "pathType", "combo", default="Prefix", options=PATH_TYPES),
            Field("backend_service", "Backend service", default="my-app", required=True),
            Field("backend_port", "Backend port", default="80", required=True, help="number or service port name"),
            Field("extra_rules", "Additional paths", "text", default="", height=3,
                  help="host path pathType service port  (host '-' = same as Host)"),
            Field("tls_secret", "TLS secret", default="", help="blank = no TLS"),
            Field("tls_hosts", "TLS hosts", default="", help="comma-separated; blank = Host"),
        ]),
    ],
    "ConfigMap": [
        ("Metadata", _meta_fields("ConfigMap", "app-config")),
        ("Data", [
            Field("data", "Data", "text", default="LOG_LEVEL=info\nFEATURE_X=true", height=5,
                  help="KEY=VALUE per line (\\n in a value becomes a newline)"),
            Field("file_key", "File key", default="", help="optional multi-line entry, e.g. app.properties"),
            Field("file_content", "File content", "text", default="", height=6),
            Field("immutable", "immutable", "check", default=False),
        ]),
    ],
    "Secret": [
        ("Metadata", _meta_fields("Secret", "app-secret")),
        ("Secret data", [
            Field("secret_type", "type", "combo", default="Opaque", options=SECRET_TYPES),
            Field("data", "Data (plaintext)", "text", default="username=admin\npassword=changeme", height=5,
                  help="KEY=VALUE per line, plaintext - base64-encoded into data:"),
            Field("file_key", "File key", default="", help="optional multi-line entry, e.g. tls.crt or ssh-privatekey"),
            Field("file_content", "File content", "text", default="", height=6),
            Field("file_key2", "File key 2", default="", help="e.g. tls.key"),
            Field("file_content2", "File content 2", "text", default="", height=6),
            Field("already_base64", "Values are already base64", "check", default=False),
            Field("immutable", "immutable", "check", default=False),
        ]),
        ("dockerconfigjson helper", [
            Field("registry_server", "Registry server", default="", help="fills .dockerconfigjson, e.g. ghcr.io"),
            Field("registry_user", "Registry username", default=""),
            Field("registry_password", "Registry password/token", default=""),
            Field("registry_email", "Registry email", default=""),
        ]),
        ("service-account-token", [
            Field("sa_token_account", "ServiceAccount name", default="",
                  help="sets kubernetes.io/service-account.name annotation"),
        ]),
    ],
    "PersistentVolumeClaim": [
        ("Metadata", _meta_fields("PersistentVolumeClaim", "data")),
        ("PVC spec", [
            Field("size", "Size", default="10Gi", required=True, help="e.g. 1Gi, 500Mi"),
            Field("access_mode", "Access mode", "combo", default="ReadWriteOnce", options=ACCESS_MODES),
            Field("storage_class", "storageClassName", "ecombo", default="", options=STORAGE_CLASSES,
                  help="blank = cluster default"),
            Field("volume_mode", "volumeMode", "combo", default="Filesystem", options=VOLUME_MODES),
            Field("volume_name", "volumeName (bind to PV)", default=""),
        ]),
    ],
    "NetworkPolicy": [
        ("Metadata", _meta_fields("NetworkPolicy", "my-policy")),
        ("Policy", [
            Field("pod_selector", "Pod selector", "text", default="app=web", height=2,
                  help="key=value, one per line or comma-separated; blank = every pod in the namespace"),
            Field("policy_ingress", "policyTypes: Ingress", "check", default=True),
            Field("policy_egress", "policyTypes: Egress", "check", default=False),
        ]),
        ("Quick presets", [
            Field("np_default_deny", "Default deny", "combo", default="", options=NP_DEFAULT_DENY),
            Field("np_allow_same_ns", "Allow ingress from the same namespace", "check", default=False),
            Field("np_allow_namespaces", "Allow ingress from namespaces", default="",
                  help="namespace names, comma-separated, e.g. monitoring, ingress-nginx"),
            Field("np_allow_dns", "Allow DNS egress (kube-dns, 53/UDP+TCP)", "check", default=False),
        ]),
        ("Ingress rules", [_table("ingress_rules", "Ingress rules", ["peer", "pod_labels", "ns_labels", "cidr", "ports", "rule"],
                                  "who may connect TO the selected pods (from:)")]),
        ("Egress rules", [_table("egress_rules", "Egress rules", ["peer", "pod_labels", "ns_labels", "cidr", "ports", "rule"],
                                 "where the selected pods may connect (to:)")]),
    ],
    "StorageClass": [
        ("Metadata", _meta_fields("StorageClass", "fast")),
        ("StorageClass", [
            Field("provisioner", "provisioner", "ecombo", default="ebs.csi.aws.com", options=PROVISIONERS,
                  required=True, help="CSI driver name"),
            Field("default_class", "Default StorageClass", "check", default=False),
            Field("parameters", "parameters", "text", default="type=gp3", height=4,
                  help="key=value per line (driver-specific; values are passed as strings)"),
            Field("reclaim_policy", "reclaimPolicy", "combo", default="Delete", options=RECLAIM_POLICIES),
            Field("binding_mode", "volumeBindingMode", "combo", default="WaitForFirstConsumer", options=BINDING_MODES),
            Field("allow_expansion", "allowVolumeExpansion", "check", default=True),
            Field("mount_options", "mountOptions", "text", default="", height=2,
                  help="one per line or comma-separated, e.g. nfsvers=4.1"),
        ]),
        ("Allowed topologies", [_table("allowed_topologies", "allowedTopologies", ["key", "values"],
                                       "restrict where volumes can be provisioned (zones / nodes)")]),
    ],
    "VolumeSnapshotClass": [
        ("Metadata", _meta_fields("VolumeSnapshotClass", "csi-snapclass")),
        ("VolumeSnapshotClass", [
            Field("driver", "driver", "ecombo", default="ebs.csi.aws.com", options=SNAPSHOT_DRIVERS, required=True,
                  help="CSI driver name (same as the StorageClass provisioner)"),
            Field("deletion_policy", "deletionPolicy", "combo", default="Delete", options=DELETION_POLICIES),
            Field("default_snapshot_class", "Default VolumeSnapshotClass", "check", default=False),
            Field("parameters", "parameters", "text", default="", height=3, help="key=value per line (driver-specific)"),
        ]),
    ],
    "Namespace": [
        ("Metadata", _meta_fields("Namespace", "my-namespace")),
        ("Pod Security (optional)", [
            Field("pss_enforce", "pod-security enforce", "combo", default="",
                  options=["", "privileged", "baseline", "restricted"],
                  help="adds pod-security.kubernetes.io/enforce label"),
        ]),
    ],
}


def defaults(kind: str) -> dict:
    return {f.key: copy.deepcopy(f.default) for _, fields in SCHEMA[kind] for f in fields}


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS1123_SUB = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
_DNS1035_LABEL = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
_LABEL_NAME = re.compile(r"^([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9]$")
_QUANTITY = re.compile(r"^([0-9]+(\.[0-9]*)?|\.[0-9]+)(m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei|[eE][0-9]+)?$")
_ENV_NAME = re.compile(r"^[-._a-zA-Z][-._a-zA-Z0-9]*$")
_DATA_KEY = re.compile(r"^[-._a-zA-Z0-9]+$")
_CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,\-?LW#]+$")
_CRON_MACROS = {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
_PERCENT_OR_INT = re.compile(r"^([0-9]+%?)$")
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")


@dataclass
class Result:
    manifest: dict | None
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


class _Ctx:
    def __init__(self, values: dict):
        self.v = values
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def s(self, key) -> str:
        val = self.v.get(key, "")
        if val is None:
            return ""
        if isinstance(val, bool):
            return "true" if val else ""
        if isinstance(val, (list, dict)):
            return ""
        return str(val).strip()

    def raw(self, key) -> str:
        val = self.v.get(key, "")
        return "" if val is None else str(val)

    def b(self, key) -> bool:
        val = self.v.get(key, False)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    def int(self, key, label, lo=None, hi=None, required=False):
        s = self.s(key)
        if not s:
            if required:
                self.err(f"{label} is required.")
            return None
        try:
            n = int(s)
        except ValueError:
            self.err(f"{label} must be a whole number (got '{s}').")
            return None
        if lo is not None and n < lo:
            self.err(f"{label} must be >= {lo} (got {n}).")
            return None
        if hi is not None and n > hi:
            self.err(f"{label} must be <= {hi} (got {n}).")
            return None
        return n


class _SubCtx(_Ctx):
    """Context for one table row: shares the parent's error lists, prefixes messages."""

    def __init__(self, parent: _Ctx, values: dict, prefix: str):
        super().__init__(values)
        self.errors, self.warnings, self.prefix = parent.errors, parent.warnings, prefix

    def err(self, msg):
        self.errors.append(self.prefix + msg)

    def warn(self, msg):
        self.warnings.append(self.prefix + msg)


def _rows(c: _Ctx, key: str) -> list[dict]:
    rows = c.v.get(key) or []
    if isinstance(rows, str):  # allow JSON for scripting convenience
        try:
            rows = json.loads(rows)
        except ValueError:
            c.err(f"{key} must be a list of rows.")
            return []
    base = row_defaults(key)
    return [{**base, **(r or {})} for r in rows]


def _csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _check_dns(c: _Ctx, value: str, label: str, rule: str = "subdomain", max_len: int | None = None):
    if rule == "label":
        rx, ml, desc = _DNS1123_LABEL, 63, "a DNS-1123 label (lowercase letters, digits and '-', start/end alphanumeric, max 63)"
    elif rule == "1035":
        rx, ml, desc = _DNS1035_LABEL, 63, "a DNS-1035 label (lowercase letters, digits and '-', must START with a letter, max 63)"
    else:
        rx, ml, desc = _DNS1123_SUB, 253, "a DNS-1123 subdomain (lowercase letters, digits, '-' and '.', start/end alphanumeric)"
    ml = max_len or ml
    if not value:
        c.err(f"{label} is required.")
        return False
    if len(value) > ml:
        c.err(f"{label} '{value}' is too long ({len(value)} chars, max {ml}).")
        return False
    if not rx.match(value):
        hint = ""
        if value.lower() != value:
            hint = " Hint: use lowercase."
        elif "_" in value:
            hint = " Hint: replace '_' with '-'."
        c.err(f"{label} '{value}' must be {desc}.{hint}")
        return False
    return True


def _check_label_key(c: _Ctx, key: str, what: str):
    prefix, _, name = key.rpartition("/")
    if prefix and (len(prefix) > 253 or not _DNS1123_SUB.match(prefix)):
        c.err(f"{what} key '{key}': prefix '{prefix}' must be a DNS subdomain.")
    if not name or len(name) > 63 or not _LABEL_NAME.match(name):
        c.err(f"{what} key '{key}' is invalid (max 63 chars, alphanumeric, '-', '_', '.', start/end alphanumeric).")


def _parse_kv(c: _Ctx, text: str, what: str, *, split_commas=True, label_rules=False) -> dict:
    out: dict[str, str] = {}
    if not text.strip():
        return out
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        items.extend([p.strip() for p in line.split(",")] if split_commas else [line])
    for item in items:
        if not item:
            continue
        if "=" in item:
            k, val = item.split("=", 1)
        elif ":" in item and label_rules:
            k, val = item.split(":", 1)
        else:
            c.err(f"{what}: '{item}' is not in key=value form.")
            continue
        k, val = k.strip(), val.strip()
        if not k:
            c.err(f"{what}: empty key in '{item}'.")
            continue
        if label_rules:
            _check_label_key(c, k, what)
            if len(val) > 63 or (val and not _LABEL_NAME.match(val)):
                c.err(f"{what} value '{val}' for '{k}' is invalid (max 63 chars, alphanumeric, '-', '_', '.').")
        if k in out:
            c.warn(f"{what}: duplicate key '{k}', last value wins.")
        out[k] = val
    return out


def _check_quantity(c: _Ctx, value: str, label: str):
    if value and not _QUANTITY.match(value):
        c.err(f"{label} '{value}' is not a valid Kubernetes quantity (e.g. 100m, 0.5, 128Mi, 1Gi).")
        return False
    return bool(value)


def _q_to_float(q: str) -> float | None:
    m = _QUANTITY.match(q or "")
    if not m:
        return None
    num = float(m.group(1))
    suf = m.group(3) or ""
    mult = {"": 1, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
            "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
    if suf.lower().startswith("e") and suf not in mult:
        return num * 10 ** int(suf[1:])
    return num * mult.get(suf, 1)


def _split_args(text: str) -> list[str]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    if len(lines) == 1:
        try:
            return shlex.split(lines[0])
        except ValueError:
            return [lines[0].strip()]
    return [ln.strip() for ln in lines]


def _port_value(c: _Ctx, s: str, label: str):
    """Return int for numeric ports, str for named ports."""
    s = s.strip()
    if s.isdigit():
        n = int(s)
        if not 1 <= n <= 65535:
            c.err(f"{label} {n} must be between 1 and 65535.")
        return n
    if s and not _is_port_name(s):
        c.err(f"{label} '{s}' must be a number 1-65535 or a port name (max 15 chars, lowercase alphanumeric/'-').")
    return s


def _is_port_name(s: str) -> bool:
    return (len(s) <= 15 and bool(_DNS1123_LABEL.match(s)) and "--" not in s
            and any(ch.isalpha() for ch in s))


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _metadata(c: _Ctx, kind: str) -> dict:
    name = c.s("name")
    rule, ml = "subdomain", None
    if kind == "Namespace":
        rule = "label"
    elif kind == "Service":
        rule = "1035"
    elif kind == "CronJob":
        ml = 52
    elif kind == "Job":
        ml = 63
    _check_dns(c, name, f"{kind} name", rule, ml)
    if kind == "StatefulSet" and len(name) > 52:
        c.warn("StatefulSet names longer than 52 chars can break pod hostname/controller-revision labels.")
    md: dict[str, Any] = {"name": name}
    if kind not in CLUSTER_SCOPED:
        ns = c.s("namespace")
        if ns:
            _check_dns(c, ns, "Namespace", "label")
            md["namespace"] = ns
    labels = _parse_kv(c, c.raw("labels"), "Labels", label_rules=True)
    if kind == "Namespace" and c.s("pss_enforce"):
        labels["pod-security.kubernetes.io/enforce"] = c.s("pss_enforce")
    if labels:
        md["labels"] = labels
    ann = _parse_kv(c, c.raw("annotations"), "Annotations", split_commas=False)
    for k in ann:
        _check_label_key(c, k, "Annotation")
    if ann:
        md["annotations"] = ann
    return md


def _parse_container_ports(c: _Ctx) -> list[dict]:
    default_proto = c.s("port_protocol") or "TCP"
    ports = []
    seen_names = set()
    for raw in re.split(r"[\n,]", c.raw("ports")):
        raw = raw.strip()
        if not raw:
            continue
        proto = default_proto
        if "/" in raw:
            raw, proto = raw.rsplit("/", 1)
            proto = proto.strip().upper()
            if proto not in PROTOCOLS:
                c.err(f"Container port protocol '{proto}' must be one of {', '.join(PROTOCOLS)}.")
        pname = None
        if ":" in raw:
            pname, raw = raw.split(":", 1)
            pname = pname.strip()
        raw = raw.strip()
        if not raw.isdigit():
            c.err(f"Container port '{raw}' must be a number (format: [name:]port[/PROTO]).")
            continue
        n = int(raw)
        if not 1 <= n <= 65535:
            c.err(f"Container port {n} must be between 1 and 65535.")
        p: dict[str, Any] = {}
        if pname:
            if not _is_port_name(pname):
                c.err(f"Container port name '{pname}' must be <=15 chars, lowercase alphanumeric/'-', contain a letter.")
            if pname in seen_names:
                c.err(f"Container port name '{pname}' is duplicated.")
            seen_names.add(pname)
            p["name"] = pname
        p["containerPort"] = n
        p["protocol"] = proto
        ports.append(p)
    return ports


def _parse_env(c: _Ctx) -> list[dict]:
    env = []
    for line in c.raw("env").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            c.err(f"Env var '{line}' must be KEY=VALUE.")
            continue
        k, val = line.split("=", 1)
        k = k.strip()
        if not _ENV_NAME.match(k):
            c.err(f"Env var name '{k}' is invalid (letters, digits, '_', '-', '.', not starting with a digit).")
            continue
        m = re.match(r"^@(configmap|secret):([^/]+)/(.+)$", val.strip(), re.I)
        if m:
            src, ref, key = m.group(1).lower(), m.group(2).strip(), m.group(3).strip()
            _check_dns(c, ref, f"Env {k} {src} name")
            sel = "configMapKeyRef" if src == "configmap" else "secretKeyRef"
            env.append({"name": k, "valueFrom": {sel: {"name": ref, "key": key}}})
        else:
            env.append({"name": k, "value": val})
    return env


def _parse_env_from(c: _Ctx) -> list[dict]:
    out = []
    for line in c.raw("env_from").splitlines():
        line = line.strip()
        if not line:
            continue
        typ, _, ref = line.partition(":")
        typ = typ.strip().lower()
        ref = ref.strip()
        if typ not in ("configmap", "secret") or not ref:
            c.err(f"envFrom '{line}' must be configmap:<name> or secret:<name>.")
            continue
        _check_dns(c, ref, f"envFrom {typ} name")
        out.append({"configMapRef" if typ == "configmap" else "secretRef": {"name": ref}})
    return out


def _parse_volumes(c: _Ctx) -> tuple[list[dict], list[dict]]:
    vols, mounts = [], []
    names = set()
    for line in c.raw("volumes").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 4:
            c.err(f"Volume '{line}' must be name:type:source:mountPath[:ro].")
            continue
        vname, vtype, src, mpath = parts[:4]
        ro = len(parts) > 4 and parts[4].lower() in ("ro", "readonly", "true")
        _check_dns(c, vname, "Volume name", "label")
        if vname in names:
            c.err(f"Volume name '{vname}' is duplicated.")
        names.add(vname)
        if mpath and not mpath.startswith("/"):
            c.err(f"Volume '{vname}' mountPath '{mpath}' must be an absolute path.")
        t = vtype.lower()
        vol: dict[str, Any] = {"name": vname}
        if t in ("pvc", "persistentvolumeclaim"):
            if not src:
                c.err(f"Volume '{vname}': pvc needs a claim name as source.")
            else:
                _check_dns(c, src, f"Volume '{vname}' claimName")
            vol["persistentVolumeClaim"] = {"claimName": src}
            if ro:
                vol["persistentVolumeClaim"]["readOnly"] = True
        elif t in ("configmap", "cm"):
            if not src:
                c.err(f"Volume '{vname}': configMap needs a ConfigMap name as source.")
            vol["configMap"] = {"name": src}
        elif t == "secret":
            if not src:
                c.err(f"Volume '{vname}': secret needs a Secret name as source.")
            vol["secret"] = {"secretName": src}
        elif t == "emptydir":
            ed: dict[str, Any] = {}
            if src.lower() == "memory":
                ed["medium"] = "Memory"
            elif src:
                if _check_quantity(c, src, f"Volume '{vname}' emptyDir sizeLimit"):
                    ed["sizeLimit"] = src
            vol["emptyDir"] = ed
        elif t == "hostpath":
            if not src.startswith("/"):
                c.err(f"Volume '{vname}': hostPath source must be an absolute path.")
            vol["hostPath"] = {"path": src}
        else:
            c.err(f"Volume '{vname}': unknown type '{vtype}' (use pvc, configMap, secret, emptyDir, hostPath).")
            continue
        vols.append(vol)
        if not mpath:
            continue  # defined for other containers only
        m: dict[str, Any] = {"name": vname, "mountPath": mpath}
        if ro:
            m["readOnly"] = True
        mounts.append(m)
    return vols, mounts


def _resources(c: _Ctx) -> dict:
    res: dict[str, dict] = {}
    for kind_, fields in (("requests", ("cpu_request", "memory_request")), ("limits", ("cpu_limit", "memory_limit"))):
        for key in fields:
            val = c.s(key)
            label = key.replace("_", " ")
            if _check_quantity(c, val, label):
                res.setdefault(kind_, {})["cpu" if key.startswith("cpu") else "memory"] = val
    for r in ("cpu", "memory"):
        rq = res.get("requests", {}).get(r)
        lm = res.get("limits", {}).get(r)
        if rq and lm:
            a, b = _q_to_float(rq), _q_to_float(lm)
            if a is not None and b is not None and a > b:
                c.err(f"{r} request ({rq}) must not exceed {r} limit ({lm}).")
    return res


def _check_image(c: _Ctx, image: str):
    if not image:
        c.err("Image is required.")
    elif re.search(r"\s", image):
        c.err(f"Image '{image}' must not contain whitespace.")
    elif image.endswith(":"):
        c.err(f"Image '{image}' looks malformed.")
    elif ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
        c.warn(f"Image '{image}' has no tag; Kubernetes will use ':latest' (and imagePullPolicy defaults to Always).")
    elif image.endswith(":latest"):
        c.warn("Using ':latest' tag is not recommended for reproducible deployments.")


def _container_core(c: _Ctx, cname: str) -> dict:
    """name/image/pull policy/command/args/workingDir/ports/env/envFrom/resources from ctx keys."""
    image = c.s("image")
    _check_image(c, image)
    container: dict[str, Any] = {"name": cname, "image": image}
    ipp = c.s("image_pull_policy")
    if ipp:
        if ipp not in IMAGE_PULL_POLICIES:
            c.err(f"imagePullPolicy must be one of {', '.join(IMAGE_PULL_POLICIES)}.")
        container["imagePullPolicy"] = ipp
    cmd = _split_args(c.raw("command"))
    if cmd:
        container["command"] = cmd
    args = _split_args(c.raw("args"))
    if args:
        container["args"] = args
    if c.s("working_dir"):
        container["workingDir"] = c.s("working_dir")
    ports = _parse_container_ports(c)
    if ports:
        container["ports"] = ports
    env = _parse_env(c)
    if env:
        container["env"] = env
    env_from = _parse_env_from(c)
    if env_from:
        container["envFrom"] = env_from
    res = _resources(c)
    if res:
        container["resources"] = res
    return container


def _parse_mounts(c: _Ctx, text: str, defined: set | None) -> list[dict]:
    mounts, paths = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            c.err(f"volumeMount '{line}' must be volume:/mount/path[:ro].")
            continue
        vname, mpath = parts[0], parts[1]
        if not mpath.startswith("/"):
            c.err(f"volumeMount '{vname}' path '{mpath}' must be absolute.")
        if defined is not None and vname not in defined:
            known = f"defined: {', '.join(sorted(defined))}" if defined else "no volumes are defined"
            c.err(f"mounts volume '{vname}', which is not defined in Volumes ({known}).")
        if mpath in paths:
            c.err(f"mountPath '{mpath}' is used twice.")
        paths.add(mpath)
        m: dict[str, Any] = {"name": vname, "mountPath": mpath}
        if len(parts) > 2 and parts[2].lower() in ("ro", "readonly", "true"):
            m["readOnly"] = True
        mounts.append(m)
    return mounts


def _extra_container(c: _Ctx, row: dict, idx: int, defined: set | None) -> tuple[str, dict]:
    label = row.get("name") or f"#{idx + 1}"
    sc = _SubCtx(c, row, f"Container '{label}': ")
    role = sc.s("role") or ROLE_SIDECAR
    if role not in CONTAINER_ROLES:
        sc.err(f"role must be one of {', '.join(CONTAINER_ROLES)}.")
    name = sc.s("name")
    _check_dns(sc, name, "name", "label")
    ctr = _container_core(sc, name)
    mounts = _parse_mounts(sc, sc.raw("volume_mounts"), defined)
    if mounts:
        ctr["volumeMounts"] = mounts
    if role == ROLE_NATIVE:
        # restartPolicy right after name/image for readability
        ctr = {"name": ctr.pop("name"), "image": ctr.pop("image"), "restartPolicy": "Always", **ctr}
    return role, ctr


def _tolerations(c: _Ctx) -> list[dict]:
    out = []
    for i, r in enumerate(_rows(c, "tolerations")):
        sc = _SubCtx(c, r, f"Toleration #{i + 1}: ")
        key, op, val, eff = sc.s("key"), sc.s("operator") or "Equal", sc.s("value"), sc.s("effect")
        t: dict[str, Any] = {}
        if key:
            _check_label_key(sc, key, "key")
            t["key"] = key
        if op not in TOLERATION_OPERATORS:
            sc.err(f"operator must be Equal or Exists (got '{op}').")
        t["operator"] = op
        if op == "Exists" and val:
            sc.err("value must be empty when operator is Exists.")
        if op == "Equal" and not key:
            sc.err("an empty key requires operator Exists (tolerates every taint).")
        if op == "Equal":
            if val and (len(val) > 63 or not _LABEL_NAME.match(val)):
                sc.err(f"value '{val}' must be a valid label value (max 63, alphanumeric, '-', '_', '.').")
            t["value"] = val
        if eff:
            if eff not in TOLERATION_EFFECTS:
                sc.err(f"effect must be one of NoSchedule, PreferNoSchedule, NoExecute or blank.")
            t["effect"] = eff
        secs = sc.int("toleration_seconds", "tolerationSeconds", lo=0)
        if secs is not None:
            if eff != "NoExecute":
                sc.err("tolerationSeconds is only allowed with effect NoExecute.")
            t["tolerationSeconds"] = secs
        out.append(t)
    return out


def _match_expr(sc: _Ctx, ops: list[str], numeric_ops=()) -> dict:
    key, op, vals = sc.s("key"), sc.s("operator") or "In", _csv(sc.s("values"))
    if not key:
        sc.err("key is required.")
    else:
        _check_label_key(sc, key, "key")
    if op not in ops:
        sc.err(f"operator must be one of {', '.join(ops)} (got '{op}').")
    e: dict[str, Any] = {"key": key, "operator": op}
    if op in ("In", "NotIn"):
        if not vals:
            sc.err(f"operator {op} needs at least one value.")
        for v in vals:
            if len(v) > 63 or not _LABEL_NAME.match(v):
                sc.err(f"value '{v}' is not a valid label value.")
        e["values"] = vals
    elif op in ("Exists", "DoesNotExist"):
        if vals:
            sc.err(f"operator {op} must not have values.")
    elif op in numeric_ops:
        if len(vals) != 1 or not re.match(r"^-?[0-9]+$", vals[0]):
            sc.err(f"operator {op} needs exactly one integer value.")
        e["values"] = vals
    return e


def _weight(sc: _Ctx) -> int:
    w = sc.int("weight", "weight", lo=1, hi=100, required=True)
    return w if w is not None else 1


def _affinity(c: _Ctx) -> dict:
    aff: dict[str, Any] = {}
    # node affinity
    terms: dict[str, list] = {}
    preferred = []
    for i, r in enumerate(_rows(c, "node_affinity")):
        sc = _SubCtx(c, r, f"Node affinity #{i + 1}: ")
        typ = sc.s("type") or "required"
        if typ not in AFFINITY_TYPES:
            sc.err("type must be required or preferred.")
        expr = _match_expr(sc, NODE_OPERATORS, numeric_ops=("Gt", "Lt"))
        if typ == "preferred":
            preferred.append({"weight": _weight(sc), "preference": {"matchExpressions": [expr]}})
        else:
            term = sc.s("term") or "1"
            if not term.isdigit():
                sc.err("term # must be a number.")
            terms.setdefault(term, []).append(expr)
    na: dict[str, Any] = {}
    if terms:
        na["requiredDuringSchedulingIgnoredDuringExecution"] = {
            "nodeSelectorTerms": [{"matchExpressions": terms[t]} for t in sorted(terms, key=lambda x: (len(x), x))]}
    if preferred:
        na["preferredDuringSchedulingIgnoredDuringExecution"] = preferred
    if na:
        aff["nodeAffinity"] = na
    # pod (anti-)affinity
    for key, k8s, label in (("pod_affinity", "podAffinity", "Pod affinity"),
                            ("pod_anti_affinity", "podAntiAffinity", "Pod anti-affinity")):
        req, pref = [], []
        for i, r in enumerate(_rows(c, key)):
            sc = _SubCtx(c, r, f"{label} #{i + 1}: ")
            typ = sc.s("type") or "required"
            if typ not in AFFINITY_TYPES:
                sc.err("type must be required or preferred.")
            term: dict[str, Any] = {"labelSelector": {"matchExpressions": [_match_expr(sc, SELECTOR_OPERATORS)]}}
            nss = _csv(sc.s("namespaces"))
            for n in nss:
                _check_dns(sc, n, "namespace", "label")
            if nss:
                term["namespaces"] = nss
            topo_key = sc.s("topology_key")
            if not topo_key:
                sc.err("topologyKey is required (e.g. kubernetes.io/hostname).")
            else:
                _check_label_key(sc, topo_key, "topologyKey")
            term["topologyKey"] = topo_key
            if typ == "preferred":
                pref.append({"weight": _weight(sc), "podAffinityTerm": term})
            else:
                req.append(term)
        block: dict[str, Any] = {}
        if req:
            block["requiredDuringSchedulingIgnoredDuringExecution"] = req
        if pref:
            block["preferredDuringSchedulingIgnoredDuringExecution"] = pref
        if block:
            aff[k8s] = block
    return aff


def _spread(c: _Ctx, pod_labels: dict) -> list[dict]:
    out = []
    for i, r in enumerate(_rows(c, "topology_spread")):
        sc = _SubCtx(c, r, f"Topology spread #{i + 1}: ")
        skew = sc.int("max_skew", "maxSkew", lo=1, required=True)
        topo_key = sc.s("topology_key")
        if not topo_key:
            sc.err("topologyKey is required.")
        else:
            _check_label_key(sc, topo_key, "topologyKey")
        wu = sc.s("when_unsatisfiable") or "DoNotSchedule"
        if wu not in WHEN_UNSATISFIABLE:
            sc.err("whenUnsatisfiable must be DoNotSchedule or ScheduleAnyway.")
        ml = _parse_kv(sc, sc.raw("match_labels"), "match labels", label_rules=True) or dict(pod_labels)
        if not ml:
            sc.err("match labels are required when the pod has no labels.")
        out.append({"maxSkew": skew or 1, "topologyKey": topo_key, "whenUnsatisfiable": wu,
                    "labelSelector": {"matchLabels": ml}})
    return out


def check_row(table_key: str, row: dict) -> list[str]:
    """Validate one table row in isolation (used by the GUI row dialog)."""
    c = _Ctx({table_key: [row]})
    if table_key == "extra_containers":
        _extra_container(c, {**row_defaults(table_key), **row}, 0, None)
    elif table_key == "tolerations":
        _tolerations(c)
    elif table_key in ("node_affinity", "pod_affinity", "pod_anti_affinity"):
        _affinity(c)
    elif table_key == "topology_spread":
        _spread(c, {"app": "x"})
    elif table_key in ("ingress_rules", "egress_rules"):
        _np_rules(c, table_key, "from" if table_key == "ingress_rules" else "to")
    elif table_key == "allowed_topologies":
        c2 = _Ctx({"name": "x", "allowed_topologies": [row], "provisioner": "x.y"})
        storage_class_doc(c2)
        c.errors.extend(c2.errors)
    return [e.split(": ", 1)[1] if e.startswith(("Container '", "Toleration #", "Node affinity #", "Pod ",
                                                    "Topology spread #", "Ingress rule row #", "Egress rule row #",
                                                    "Allowed topology #")) else e for e in c.errors]


def _pod_template(c: _Ctx, kind: str, name: str, pod_labels: dict) -> dict:
    cname = c.s("container_name") or name[:63]
    if c.s("container_name"):
        _check_dns(c, cname, "Container name", "label")
    container = _container_core(c, cname)
    ports = container.get("ports", [])

    # probes
    probe_port_s = c.s("probe_port")

    def _pport(pkey):
        if probe_port_s:
            return _port_value(c, probe_port_s, "Probe port")
        if ports:
            return ports[0].get("name") or ports[0]["containerPort"]
        c.err(f"{pkey}: set a Probe port or at least one container port.")
        return 80
    timing = {}
    for key, k8s, lo in (("probe_period", "periodSeconds", 1), ("probe_timeout", "timeoutSeconds", 1),
                         ("probe_failure_threshold", "failureThreshold", 1)):
        n = c.int(key, k8s, lo=lo)
        if n is not None:
            timing[k8s] = n
    for prefix, pkey, delay in (("readiness", "readinessProbe", 5), ("liveness", "livenessProbe", 15)):
        ptype = c.s(f"{prefix}_type")
        path = c.s(f"{prefix}_path")
        cmd = c.raw(f"{prefix}_command").strip()
        if ptype not in PROBE_TYPES:
            c.err(f"{pkey} type must be one of httpGet, tcpSocket, exec, grpc.")
            continue
        if not ptype:
            if not path:
                if cmd:
                    c.warn(f"{pkey}: exec command is ignored unless the probe type is 'exec'.")
                continue
            ptype = "httpGet"
        if ptype == "httpGet":
            if not path:
                c.err(f"{pkey}: httpGet needs an HTTP path.")
                path = "/"
            elif not path.startswith("/"):
                c.err(f"{pkey} path '{path}' must start with '/'.")
            handler = {"httpGet": {"path": path, "port": _pport(pkey)}}
        elif ptype == "tcpSocket":
            handler = {"tcpSocket": {"port": _pport(pkey)}}
        elif ptype == "grpc":
            pp = _pport(pkey)
            if not isinstance(pp, int):
                c.err(f"{pkey}: grpc probes need a numeric port (not a port name).")
                pp = 0
            handler = {"grpc": {"port": pp}}
        else:
            argv = _split_args(cmd)
            if not argv:
                c.err(f"{pkey}: exec probe needs a command.")
            handler = {"exec": {"command": argv}}
        d = c.int(f"{prefix}_initial_delay", f"{pkey} initialDelaySeconds", lo=0)
        container[pkey] = {**handler, "initialDelaySeconds": delay if d is None else d,
                           "periodSeconds": 10, **timing}

    vols, mounts = _parse_volumes(c)
    defined = {v["name"] for v in vols}
    if kind == "StatefulSet" and c.s("vct_name"):
        defined.add(c.s("vct_name"))
    if kind == "StatefulSet" and c.s("vct_name"):
        vct = c.s("vct_name")
        if any(m["name"] == vct for m in mounts):
            c.err(f"volumeClaimTemplate name '{vct}' clashes with a volume of the same name.")
        mp = c.s("vct_mount")
        if not mp.startswith("/"):
            c.err("VCT mountPath must be an absolute path.")
        mounts.append({"name": vct, "mountPath": mp})
    if mounts:
        container["volumeMounts"] = mounts

    # additional containers
    containers, init_containers = [container], []
    roles = [ROLE_MAIN]
    for i, row in enumerate(_rows(c, "extra_containers")):
        role, ctr = _extra_container(c, row, i, defined)
        roles.append(role)
        (init_containers if role in (ROLE_INIT, ROLE_NATIVE) else containers).append(ctr)
    if roles.count(ROLE_MAIN) < 1:
        c.err("The pod needs at least one main container.")
    seen: dict[str, int] = {}
    for ctr in containers + init_containers:
        seen[ctr["name"]] = seen.get(ctr["name"], 0) + 1
    for n, cnt in seen.items():
        if cnt > 1 and n:
            c.err(f"Container name '{n}' is used by {cnt} containers; names must be unique in the pod "
                  f"(the main container defaults to the resource name).")
    port_owner: dict[tuple, str] = {}
    for ctr in containers + [x for x in init_containers if x.get("restartPolicy") == "Always"]:
        for p in ctr.get("ports", []):
            k = (p["containerPort"], p["protocol"])
            if k in port_owner and port_owner[k] != ctr["name"]:
                c.warn(f"Port {k[0]}/{k[1]} is declared by both '{port_owner[k]}' and '{ctr['name']}'.")
            port_owner.setdefault(k, ctr["name"])
    if any(x.get("restartPolicy") == "Always" for x in init_containers):
        c.warn("Native sidecars (initContainers with restartPolicy: Always) need Kubernetes 1.29+.")

    pod: dict[str, Any] = {}
    sa = c.s("service_account")
    if sa:
        _check_dns(c, sa, "serviceAccountName")
        pod["serviceAccountName"] = sa
    if not c.b("enable_service_links"):
        pod["enableServiceLinks"] = False
    rp = c.s("restart_policy") or restart_policies_for(kind)[0]
    allowed = restart_policies_for(kind)
    if rp not in allowed:
        c.err(f"restartPolicy '{rp}' is not allowed for {kind} (allowed: {', '.join(allowed)}).")
    pod["restartPolicy"] = rp
    psc = {}
    for key, k8s in (("run_as_user", "runAsUser"), ("run_as_group", "runAsGroup"), ("fs_group", "fsGroup")):
        n = c.int(key, f"securityContext.{k8s}", lo=0, hi=2147483647)
        if n is not None:
            psc[k8s] = n
    if c.b("run_as_non_root"):
        psc["runAsNonRoot"] = True
        if psc.get("runAsUser") == 0:
            c.err("runAsNonRoot is set but runAsUser is 0 (root).")
    if psc:
        pod["securityContext"] = psc
    ns = _parse_kv(c, c.raw("node_selector"), "nodeSelector", label_rules=False)
    if ns:
        pod["nodeSelector"] = ns
    ips = [s.strip() for s in c.s("image_pull_secrets").split(",") if s.strip()]
    for s in ips:
        _check_dns(c, s, "imagePullSecret name")
    if ips:
        pod["imagePullSecrets"] = [{"name": s} for s in ips]
    tg = c.int("termination_grace", "terminationGracePeriodSeconds", lo=0)
    if tg is not None:
        pod["terminationGracePeriodSeconds"] = tg
    if kind == "DaemonSet":
        if c.b("host_network"):
            pod["hostNetwork"] = True
            pod["dnsPolicy"] = "ClusterFirstWithHostNet"
    tols = _tolerations(c)
    if kind == "DaemonSet" and c.b("tolerate_all"):
        if not any(t == {"operator": "Exists"} for t in tols):
            tols.insert(0, {"operator": "Exists"})
    if tols:
        pod["tolerations"] = tols
    aff = _affinity(c)
    if aff:
        pod["affinity"] = aff
    spread = _spread(c, pod_labels)
    if spread:
        pod["topologySpreadConstraints"] = spread
    if init_containers:
        pod["initContainers"] = init_containers
    pod["containers"] = containers
    if vols:
        pod["volumes"] = vols
    first = ("serviceAccountName", "restartPolicy", "initContainers", "containers", "volumes")
    pod = {**{k: pod[k] for k in first if k in pod}, **{k: v for k, v in pod.items() if k not in first}}
    tmpl: dict[str, Any] = {}
    if pod_labels:
        tmpl["metadata"] = {"labels": dict(pod_labels)}
    tmpl["spec"] = pod
    return tmpl


def _selector_and_labels(c: _Ctx, md: dict) -> tuple[dict, dict]:
    labels = md.get("labels") or {}
    sel = _parse_kv(c, c.raw("selector"), "Selector", label_rules=True) or dict(labels) or {"app": md["name"][:63]}
    if not labels:
        md["labels"] = dict(sel)
    pod_labels = {**md["labels"], **sel}
    return sel, pod_labels


def _job_spec(c: _Ctx, kind: str, name: str, pod_labels: dict) -> dict:
    spec: dict[str, Any] = {}
    for key, label, k8s in (("backoff_limit", "backoffLimit", "backoffLimit"),
                            ("completions", "completions", "completions"),
                            ("parallelism", "parallelism", "parallelism"),
                            ("active_deadline", "activeDeadlineSeconds", "activeDeadlineSeconds"),
                            ("ttl_after_finished", "ttlSecondsAfterFinished", "ttlSecondsAfterFinished")):
        lo = 1 if k8s == "activeDeadlineSeconds" else 0
        n = c.int(key, label, lo=lo)
        if n is not None:
            spec[k8s] = n
    if spec.get("completions") is not None and spec.get("parallelism") is not None \
            and spec["parallelism"] > spec["completions"] > 0:
        c.warn("parallelism is greater than completions; extra pods will not start.")
    # Job pod templates must not carry selector-managed labels we invent; user labels are fine.
    spec["template"] = _pod_template(c, kind, name, pod_labels)
    return spec


def _build_workload(c: _Ctx, kind: str) -> dict:
    md = _metadata(c, kind)
    name = md["name"]
    m: dict[str, Any] = {"apiVersion": API_VERSIONS[kind], "kind": kind, "metadata": md}
    if kind in ("Deployment", "StatefulSet", "DaemonSet"):
        sel, pod_labels = _selector_and_labels(c, md)
        spec: dict[str, Any] = {}
        if kind != "DaemonSet":
            r = c.int("replicas", "Replicas", lo=0, required=True)
            if r is not None:
                spec["replicas"] = r
        if kind == "StatefulSet":
            sn = c.s("service_name")
            if _check_dns(c, sn, "StatefulSet serviceName", "1035"):
                spec["serviceName"] = sn
            pmp = c.s("pod_management")
            if pmp and pmp != "OrderedReady":
                spec["podManagementPolicy"] = pmp
        spec["selector"] = {"matchLabels": sel}
        strat = c.s("strategy")
        if kind == "Deployment":
            if strat not in DEPLOY_STRATEGIES:
                c.err(f"Deployment strategy must be one of {', '.join(DEPLOY_STRATEGIES)}.")
            st: dict[str, Any] = {"type": strat}
            if strat == "RollingUpdate":
                ru = {}
                for key, k8s in (("max_surge", "maxSurge"), ("max_unavailable", "maxUnavailable")):
                    val = c.s(key)
                    if val:
                        if not _PERCENT_OR_INT.match(val):
                            c.err(f"{k8s} '{val}' must be an integer or a percentage like 25%.")
                        ru[k8s] = int(val) if val.isdigit() else val
                if c.s("max_surge") in ("0", "0%") and c.s("max_unavailable") in ("0", "0%"):
                    c.err("maxSurge and maxUnavailable cannot both be 0.")
                if ru:
                    st["rollingUpdate"] = ru
            spec["strategy"] = st
            rh = c.int("revision_history", "revisionHistoryLimit", lo=0)
            if rh is not None:
                spec["revisionHistoryLimit"] = rh
        else:
            if strat not in STS_DS_STRATEGIES:
                c.err(f"{kind} updateStrategy must be one of {', '.join(STS_DS_STRATEGIES)}.")
            st = {"type": strat}
            if strat == "RollingUpdate":
                if kind == "StatefulSet":
                    p = c.int("partition", "partition", lo=0)
                    if p is not None:
                        st["rollingUpdate"] = {"partition": p}
                else:
                    mu = c.s("max_unavailable")
                    if mu:
                        if not _PERCENT_OR_INT.match(mu):
                            c.err(f"maxUnavailable '{mu}' must be an integer or a percentage.")
                        st["rollingUpdate"] = {"maxUnavailable": int(mu) if mu.isdigit() else mu}
            spec["updateStrategy"] = st
        spec["template"] = _pod_template(c, kind, name, pod_labels)
        if kind == "StatefulSet" and c.s("vct_name"):
            vct = c.s("vct_name")
            _check_dns(c, vct, "volumeClaimTemplate name", "label")
            size = c.s("vct_size")
            if not size:
                c.err("VCT size is required when a volumeClaimTemplate name is set.")
            _check_quantity(c, size, "VCT size")
            pvc_spec: dict[str, Any] = {"accessModes": [c.s("vct_access_mode") or "ReadWriteOnce"],
                                        "resources": {"requests": {"storage": size}}}
            if c.s("vct_storage_class"):
                pvc_spec["storageClassName"] = c.s("vct_storage_class")
            spec["volumeClaimTemplates"] = [{"metadata": {"name": vct}, "spec": pvc_spec}]
        m["spec"] = spec
    elif kind == "Job":
        pod_labels = dict(md.get("labels") or {})
        m["spec"] = _job_spec(c, kind, name, pod_labels)
    elif kind == "CronJob":
        sched = c.s("schedule")
        if not sched:
            c.err("CronJob schedule is required.")
        elif sched.startswith("@"):
            if sched not in _CRON_MACROS and not sched.startswith("@every "):
                c.err(f"Schedule '{sched}' is not a known macro ({', '.join(sorted(_CRON_MACROS))}).")
        else:
            parts = sched.split()
            if len(parts) != 5 or not all(_CRON_FIELD.match(p) for p in parts):
                c.err(f"Schedule '{sched}' must have 5 fields: minute hour day-of-month month day-of-week (e.g. '*/5 * * * *').")
        spec = {"schedule": sched}
        tz = c.s("time_zone")
        if tz:
            if " " in tz or "TZ=" in sched:
                c.err("timeZone must be an IANA name like America/Los_Angeles.")
            spec["timeZone"] = tz
        cp = c.s("concurrency_policy") or "Allow"
        if cp not in CONCURRENCY_POLICIES:
            c.err(f"concurrencyPolicy must be one of {', '.join(CONCURRENCY_POLICIES)}.")
        spec["concurrencyPolicy"] = cp
        for key, label in (("starting_deadline", "startingDeadlineSeconds"),
                           ("successful_history", "successfulJobsHistoryLimit"),
                           ("failed_history", "failedJobsHistoryLimit")):
            n = c.int(key, label, lo=0)
            if n is not None:
                spec[label] = n
        if c.b("suspend"):
            spec["suspend"] = True
        pod_labels = dict(md.get("labels") or {})
        jt: dict[str, Any] = {}
        if pod_labels:
            jt["metadata"] = {"labels": dict(pod_labels)}
        jt["spec"] = _job_spec(c, kind, name, pod_labels)
        spec["jobTemplate"] = jt
        m["spec"] = spec
    return m


def _build_service(c: _Ctx) -> dict:
    md = _metadata(c, "Service")
    stype = c.s("service_type") or "ClusterIP"
    if stype not in SERVICE_TYPES:
        c.err(f"Service type must be one of {', '.join(SERVICE_TYPES)}.")
    spec: dict[str, Any] = {"type": stype}
    if stype == "ExternalName":
        en = c.s("external_name")
        if not en:
            c.err("externalName is required for type ExternalName.")
        elif not _DNS1123_SUB.match(en.lower().rstrip(".")):
            c.err(f"externalName '{en}' must be a DNS name like db.example.com.")
        spec["externalName"] = en
    sel = _parse_kv(c, c.raw("selector"), "Selector", label_rules=True)
    if sel and stype != "ExternalName":
        spec["selector"] = sel
    elif not sel and stype != "ExternalName":
        c.warn("Service has no selector; you will need to manage EndpointSlices yourself.")
    cip = c.s("cluster_ip")
    if cip:
        if stype != "ClusterIP" and cip == "None":
            c.err("clusterIP: None (headless) is only valid for type ClusterIP.")
        spec["clusterIP"] = cip

    ports = []
    names = set()

    def add_port(pname, port_s, target_s, node_s, proto, where):
        p: dict[str, Any] = {}
        if pname:
            if not _is_port_name(pname):
                c.err(f"{where} name '{pname}' must be <=15 chars, lowercase alphanumeric/'-', contain a letter.")
            if pname in names:
                c.err(f"{where} name '{pname}' is duplicated.")
            names.add(pname)
            p["name"] = pname
        proto = (proto or "TCP").upper()
        if proto not in PROTOCOLS:
            c.err(f"{where} protocol '{proto}' must be one of {', '.join(PROTOCOLS)}.")
        p["protocol"] = proto
        if not port_s.isdigit():
            c.err(f"{where} port '{port_s}' must be a number 1-65535.")
            return
        port = int(port_s)
        if not 1 <= port <= 65535:
            c.err(f"{where} port {port} must be between 1 and 65535.")
        p["port"] = port
        if target_s:
            p["targetPort"] = _port_value(c, target_s, f"{where} targetPort")
        if node_s:
            if stype not in ("NodePort", "LoadBalancer"):
                c.err(f"{where} nodePort is only valid for NodePort/LoadBalancer services (type is {stype}).")
            elif not node_s.isdigit():
                c.err(f"{where} nodePort '{node_s}' must be a number.")
            else:
                n = int(node_s)
                if not 1 <= n <= 65535:
                    c.err(f"{where} nodePort {n} must be between 1 and 65535.")
                elif not 30000 <= n <= 32767:
                    c.warn(f"{where} nodePort {n} is outside the default range 30000-32767.")
                p["nodePort"] = n
        ports.append(p)

    if c.s("port") or stype != "ExternalName":
        add_port(c.s("port_name"), c.s("port"), c.s("target_port"), c.s("node_port"), c.s("protocol"), "Service port")
    for line in c.raw("extra_ports").splitlines():
        line = line.strip()
        if not line:
            continue
        proto = c.s("protocol") or "TCP"
        if "/" in line:
            line, proto = line.rsplit("/", 1)
        parts = [x.strip() for x in line.split(":")]
        if len(parts) < 3:
            c.err(f"Additional port '{line}' must be name:port:targetPort[:nodePort][/PROTO].")
            continue
        add_port(parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else "", proto, f"Port '{parts[0]}'")
    if len(ports) > 1 and any("name" not in p for p in ports):
        c.err("When a Service has more than one port, every port must have a name.")
    if ports:
        spec["ports"] = ports
    etp = c.s("external_traffic_policy")
    if etp:
        if stype not in ("NodePort", "LoadBalancer"):
            c.err("externalTrafficPolicy is only valid for NodePort/LoadBalancer.")
        spec["externalTrafficPolicy"] = etp
    sa = c.s("session_affinity")
    if sa and sa != "None":
        spec["sessionAffinity"] = sa
    if c.b("publish_not_ready"):
        if stype == "ExternalName":
            c.err("publishNotReadyAddresses has no effect on an ExternalName Service.")
        spec["publishNotReadyAddresses"] = True
    return {"apiVersion": "v1", "kind": "Service", "metadata": md, "spec": spec}


def _build_ingress(c: _Ctx) -> dict:
    md = _metadata(c, "Ingress")
    spec: dict[str, Any] = {}
    ic = c.s("ingress_class")
    if ic:
        _check_dns(c, ic, "ingressClassName")
        spec["ingressClassName"] = ic
    host = c.s("host").lower()
    if host and not _HOST.match(host):
        c.err(f"Host '{host}' must be a DNS name (wildcard '*.' allowed as first label), no scheme or port.")

    def backend(svc, port_s, where):
        _check_dns(c, svc, f"{where} backend service", "1035")
        if not port_s:
            c.err(f"{where} backend port is required.")
            return {"service": {"name": svc, "port": {"number": 80}}}
        pv = _port_value(c, port_s, f"{where} backend port")
        return {"service": {"name": svc, "port": {"number": pv} if isinstance(pv, int) else {"name": pv}}}

    def path_obj(path, ptype, svc, port_s, where):
        if not path.startswith("/"):
            c.err(f"{where} path '{path}' must start with '/'.")
        if ptype not in PATH_TYPES:
            c.err(f"{where} pathType '{ptype}' must be one of {', '.join(PATH_TYPES)}.")
        return {"path": path, "pathType": ptype, "backend": backend(svc, port_s, where)}

    rules: dict[str, list] = {}
    order: list[str] = []

    def add(h, p):
        if h not in rules:
            rules[h] = []
            order.append(h)
        rules[h].append(p)

    add(host, path_obj(c.s("path") or "/", c.s("path_type") or "Prefix", c.s("backend_service"),
                       c.s("backend_port"), "Ingress"))
    for line in c.raw("extra_rules").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 5:
            c.err(f"Additional path '{line}' must be: host path pathType service port.")
            continue
        h = host if parts[0] == "-" else parts[0].lower()
        if h and not _HOST.match(h):
            c.err(f"Additional path host '{h}' is not a valid DNS name.")
        add(h, path_obj(parts[1], parts[2], parts[3], parts[4], f"Path '{parts[1]}'"))
    out_rules = []
    for h in order:
        r: dict[str, Any] = {}
        if h:
            r["host"] = h
        r["http"] = {"paths": rules[h]}
        out_rules.append(r)
    tls_secret = c.s("tls_secret")
    if tls_secret:
        _check_dns(c, tls_secret, "TLS secret name")
        hosts = [x.strip().lower() for x in c.s("tls_hosts").split(",") if x.strip()] or ([host] if host else [])
        if not hosts:
            c.warn("TLS is configured without any hosts.")
        t: dict[str, Any] = {}
        if hosts:
            t["hosts"] = hosts
        t["secretName"] = tls_secret
        spec["tls"] = [t]
    spec["rules"] = out_rules
    return {"apiVersion": API_VERSIONS["Ingress"], "kind": "Ingress", "metadata": md, "spec": spec}


def _parse_data(c: _Ctx, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in c.raw("data").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            c.err(f"{what} line '{line.strip()}' must be KEY=VALUE.")
            continue
        k, val = line.split("=", 1)
        k = k.strip()
        val = val.strip().replace("\\n", "\n")
        out[k] = val
    for kk, ck in (("file_key", "file_content"), ("file_key2", "file_content2")):
        fk = c.s(kk)
        if fk:
            content = c.raw(ck)
            if not content:
                c.warn(f"{what} file key '{fk}' has empty content.")
            out[fk] = content
        elif c.raw(ck).strip():
            c.err(f"{what}: file content provided but its File key is empty.")
    for k in out:
        if len(k) > 253 or not _DATA_KEY.match(k):
            c.err(f"{what} key '{k}' is invalid (letters, digits, '-', '_', '.' only).")
    return out


def _build_configmap(c: _Ctx) -> dict:
    md = _metadata(c, "ConfigMap")
    m: dict[str, Any] = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": md}
    data = _parse_data(c, "ConfigMap data")
    if not data:
        c.warn("ConfigMap has no data.")
    m["data"] = data
    if c.b("immutable"):
        m["immutable"] = True
    if sum(len(k) + len(v) for k, v in data.items()) > 1024 * 1024:
        c.err("ConfigMap data exceeds the 1 MiB limit.")
    return m


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _build_secret(c: _Ctx) -> dict:
    md = _metadata(c, "Secret")
    stype = c.s("secret_type") or "Opaque"
    if stype not in SECRET_TYPES:
        c.warn(f"Secret type '{stype}' is not a built-in type.")
    plain = _parse_data(c, "Secret data")
    if c.s("registry_server"):
        if stype != "kubernetes.io/dockerconfigjson":
            c.warn("Registry fields are set but Secret type is not kubernetes.io/dockerconfigjson.")
        user, pw = c.s("registry_user"), c.s("registry_password")
        if not user or not pw:
            c.err("Registry username and password are required for the dockerconfigjson helper.")
        auth = {"username": user, "password": pw, "auth": _b64(f"{user}:{pw}")}
        if c.s("registry_email"):
            auth["email"] = c.s("registry_email")
        plain[".dockerconfigjson"] = json.dumps({"auths": {c.s("registry_server"): auth}}, separators=(",", ":"))
        if c.b("already_base64"):
            c.err("'Values are already base64' cannot be combined with the dockerconfigjson helper.")
    data: dict[str, str] = {}
    if c.b("already_base64"):
        for k, val in plain.items():
            try:
                base64.b64decode(val.strip(), validate=True)
                data[k] = val.strip()
            except (binascii.Error, ValueError):
                c.err(f"Secret key '{k}' is not valid base64 (uncheck 'Values are already base64' to auto-encode).")
    else:
        data = {k: _b64(v) for k, v in plain.items()}
    keys = set(plain)
    required = {
        "kubernetes.io/dockerconfigjson": [".dockerconfigjson"],
        "kubernetes.io/tls": ["tls.crt", "tls.key"],
        "kubernetes.io/ssh-auth": ["ssh-privatekey"],
    }.get(stype, [])
    missing = [k for k in required if k not in keys]
    if missing:
        c.err(f"Secret type {stype} requires key(s): {', '.join(missing)}.")
    if stype == "kubernetes.io/basic-auth" and not ({"username", "password"} & keys):
        c.err("Secret type kubernetes.io/basic-auth requires a 'username' and/or 'password' key.")
    if stype == "kubernetes.io/tls" and "tls.crt" in plain and not c.b("already_base64") \
            and "BEGIN CERTIFICATE" not in plain["tls.crt"]:
        c.warn("tls.crt does not look like a PEM certificate.")
    if stype == "kubernetes.io/service-account-token":
        sa = c.s("sa_token_account")
        ann = md.setdefault("annotations", {})
        if sa:
            _check_dns(c, sa, "ServiceAccount name")
            ann["kubernetes.io/service-account.name"] = sa
        elif "kubernetes.io/service-account.name" not in ann:
            c.err("service-account-token Secrets need the ServiceAccount name (annotation kubernetes.io/service-account.name).")
    if md.get("annotations") == {}:
        del md["annotations"]
    m: dict[str, Any] = {"apiVersion": "v1", "kind": "Secret", "metadata": md, "type": stype}
    if data or stype != "kubernetes.io/service-account-token":
        m["data"] = data
    if c.b("immutable"):
        m["immutable"] = True
    return m


def _build_pvc(c: _Ctx) -> dict:
    md = _metadata(c, "PersistentVolumeClaim")
    size = c.s("size")
    if not size:
        c.err("PVC size is required (e.g. 10Gi).")
    elif _check_quantity(c, size, "PVC size"):
        if size.endswith("m"):
            c.err("PVC size uses 'm' (milli); did you mean 'Mi'?")
        elif size[-1].isdigit() or size[-1] in "kMGTPE":
            c.warn(f"PVC size '{size}' uses decimal units; storage is usually given in Mi/Gi.")
    am = c.s("access_mode") or "ReadWriteOnce"
    if am not in ACCESS_MODES:
        c.err(f"Access mode must be one of {', '.join(ACCESS_MODES)}.")
    spec: dict[str, Any] = {"accessModes": [am]}
    sc = c.s("storage_class")
    if sc:
        _check_dns(c, sc, "storageClassName")
        spec["storageClassName"] = sc
    vm = c.s("volume_mode")
    if vm:
        if vm not in VOLUME_MODES:
            c.err(f"volumeMode must be one of {', '.join(VOLUME_MODES)}.")
        spec["volumeMode"] = vm
    if c.s("volume_name"):
        _check_dns(c, c.s("volume_name"), "volumeName")
        spec["volumeName"] = c.s("volume_name")
    spec["resources"] = {"requests": {"storage": size}}
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": md, "spec": spec}


def _build_namespace(c: _Ctx) -> dict:
    md = _metadata(c, "Namespace")
    if md["name"].startswith("kube-"):
        c.warn("Namespaces starting with 'kube-' are reserved for Kubernetes system use.")
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": md}


# --------------------------------------------------------------------------- #
# NetworkPolicy / StorageClass / VolumeSnapshotClass
# --------------------------------------------------------------------------- #
_PORT_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DNS_EGRESS_RULE = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                           "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                   "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}


def _np_ports(sc: _Ctx, text: str) -> list[dict]:
    out = []
    for item in _csv_items(text):
        proto = "TCP"
        if "/" in item:
            item, proto = item.rsplit("/", 1)
            proto = proto.strip().upper()
            if proto not in PROTOCOLS:
                sc.err(f"port protocol '{proto}' must be one of {', '.join(PROTOCOLS)}.")
        item = item.strip()
        p: dict[str, Any] = {"protocol": proto}
        m = re.match(r"^(\d+)-(\d+)$", item)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if not (1 <= a <= 65535 and 1 <= b <= 65535) or b < a:
                sc.err(f"port range '{item}' must be start-end with 1 <= start <= end <= 65535.")
            p.update({"port": a, "endPort": b})
        elif item.isdigit():
            if not 1 <= int(item) <= 65535:
                sc.err(f"port {item} must be 1-65535.")
            p["port"] = int(item)
        elif _PORT_NAME.match(item) and len(item) <= 15 and re.search("[a-z]", item):
            p["port"] = item                       # named container port, e.g. http
        else:
            sc.err(f"port '{item}' must be a number, a range like 30000-32767 or a port name like http.")
            continue
        out.append(p)
    return out


def _csv_items(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", text or "") if x.strip()]


def _selector(sc: _Ctx, text: str, what: str) -> dict:
    labels = _parse_kv(sc, text, what, label_rules=True)
    return {"matchLabels": labels} if labels else {}


def _np_peer(sc: _Ctx, row: dict, direction: str):
    """One peer dict, or None for 'any' (omit from/to)."""
    peer = row.get("peer") or NP_PEERS[0]
    if peer not in NP_PEERS:
        sc.err(f"peer must be one of {', '.join(NP_PEERS)}.")
        return None
    if peer.startswith("any"):
        return None
    if peer.startswith("ipBlock"):
        cidr = (row.get("cidr") or "").strip()
        if not cidr:
            sc.err("ipBlock needs a CIDR, e.g. 10.0.0.0/8.")
            return {"ipBlock": {"cidr": "0.0.0.0/0"}}
        try:
            net = ipaddress.ip_network(cidr, strict=True)
        except ValueError as exc:
            sc.err(f"CIDR '{cidr}' is invalid ({exc}).")
            return {"ipBlock": {"cidr": cidr}}
        blk: dict[str, Any] = {"cidr": cidr}
        exc_list = []
        for e in _csv_items(row.get("except_cidrs") or ""):
            try:
                en = ipaddress.ip_network(e, strict=True)
                if en.version != net.version or not en.subnet_of(net):
                    sc.err(f"except CIDR {e} must lie inside {cidr}.")
            except ValueError as ex:
                sc.err(f"except CIDR '{e}' is invalid ({ex}).")
            exc_list.append(e)
        if exc_list:
            blk["except"] = exc_list
        if row.get("pod_labels") or row.get("ns_labels"):
            sc.warn("ipBlock peers ignore pod / namespace labels (they select by IP only).")
        return {"ipBlock": blk}
    out: dict[str, Any] = {}
    if peer.startswith("pods"):
        out["podSelector"] = _selector(sc, row.get("pod_labels") or "", "pod labels")
    if peer.startswith("namespaces") or peer == "pods in namespaces":
        out["namespaceSelector"] = _selector(sc, row.get("ns_labels") or "", "namespace labels")
    if row.get("cidr"):
        sc.warn("CIDR is only used by ipBlock peers.")
    return out


def _np_rules(c: _Ctx, key: str, direction: str) -> list[dict]:
    rows = _rows(c, key)
    rules: list[dict] = []
    by_num: dict[str, dict] = {}
    for i, raw in enumerate(rows, 1):
        row = {**row_defaults(key), **raw}
        sc = _SubCtx(c, row, f"{'Ingress' if key == 'ingress_rules' else 'Egress'} rule row #{i}: ")
        peer = _np_peer(sc, row, direction)
        ports = _np_ports(sc, row.get("ports") or "")
        num = (row.get("rule") or "").strip()
        if num and not num.isdigit():
            sc.err("Rule # must be a number (or blank).")
        rule = by_num.get(num) if num else None
        if rule is None:
            rule = {"_any": False, "peers": [], "ports": []}
            rules.append(rule)
            if num:
                by_num[num] = rule
        if peer is None:
            rule["_any"] = True
        else:
            rule["peers"].append(peer)
        for p in ports:
            if p not in rule["ports"]:
                rule["ports"].append(p)
    out = []
    for r in rules:
        d: dict[str, Any] = {}
        if r["peers"] and not r["_any"]:
            d[direction] = r["peers"]
        if r["ports"]:
            d["ports"] = r["ports"]
        out.append(d)
    return out


def _build_network_policy(c: _Ctx) -> dict:
    md = _metadata(c, "NetworkPolicy")
    spec: dict[str, Any] = {"podSelector": _selector(c, c.raw("pod_selector"), "Pod selector")}
    deny = c.s("np_default_deny")
    if deny not in NP_DEFAULT_DENY:
        c.err(f"Default deny must be one of: {', '.join(x or '(blank)' for x in NP_DEFAULT_DENY)}.")
    ingress = _np_rules(c, "ingress_rules", "from")
    egress = _np_rules(c, "egress_rules", "to")
    if c.b("np_allow_same_ns"):
        ingress.insert(0, {"from": [{"podSelector": {}}]})
    nss = _csv_items(c.raw("np_allow_namespaces"))
    for ns in nss:
        _check_dns(c, ns, "Allowed namespace", "label")
    if nss:
        ingress.append({"from": [{"namespaceSelector": {"matchExpressions": [
            {"key": "kubernetes.io/metadata.name", "operator": "In", "values": nss}]}}]})
    if c.b("np_allow_dns"):
        egress.insert(0, copy.deepcopy(DNS_EGRESS_RULE))
    want_in = c.b("policy_ingress") or deny in ("ingress", "all") or bool(ingress)
    want_eg = c.b("policy_egress") or deny in ("egress", "all") or bool(egress)
    if c.b("policy_ingress") is False and ingress and not deny:
        c.warn("Ingress rules are set, so policyTypes includes Ingress even though the checkbox is off.")
    if c.b("policy_egress") is False and egress and not deny:
        c.warn("Egress rules are set, so policyTypes includes Egress even though the checkbox is off.")
    types = (["Ingress"] if want_in else []) + (["Egress"] if want_eg else [])
    if not types:
        c.err("Select at least one policy type (Ingress / Egress) or a Default deny preset.")
    spec["policyTypes"] = types
    if want_in and ingress:
        spec["ingress"] = ingress
    if want_eg and egress:
        spec["egress"] = egress
    if want_in and not ingress:
        c.warn("No ingress rules: ALL incoming traffic to the selected pods is denied.")
    if want_eg and not egress:
        c.warn("No egress rules: ALL outgoing traffic from the selected pods is denied (including DNS).")
    elif want_eg and not c.b("np_allow_dns") and not any(
            any(p.get("port") == 53 for p in r.get("ports", [])) or not r.get("ports") for r in egress):
        c.warn("Egress is restricted but DNS (53/UDP) isn't allowed - pods won't resolve names. "
               "Tick 'Allow DNS egress'.")
    if any(not r for r in ingress + egress):
        c.warn("A rule with peer 'any' and no ports allows ALL traffic in that direction.")
    if not spec["podSelector"]:
        c.warn("Empty pod selector: the policy applies to every pod in the namespace.")
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": md, "spec": spec}


_PARAM_KEY = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_./]*$")
_DRIVER_NAME = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?(/[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)?$")


def parse_params(c: _Ctx, text: str, what: str = "parameters") -> dict:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            c.err(f"{what}: '{line}' is not key=value.")
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if not _PARAM_KEY.match(k):
            c.err(f"{what}: key '{k}' has invalid characters.")
            continue
        out[k] = v
    return out


def _check_driver(c: _Ctx, name: str, label: str):
    if not name:
        c.err(f"{label} is required.")
    elif len(name) > 63 or not _DRIVER_NAME.match(name):
        c.err(f"{label} '{name}' must be a CSI driver name like ebs.csi.aws.com (max 63 chars).")


def storage_class_doc(c: _Ctx, provisioner: str | None = None, params: dict | None = None) -> dict:
    """StorageClass from the common keys (name, labels, annotations, default_class, reclaim_policy,
    binding_mode, allow_expansion, mount_options, allowed_topologies) + provisioner/params. Shared by
    the StorageClass kind and the CSI add-on types."""
    md = _metadata(c, "StorageClass")
    if c.b("default_class"):
        md.setdefault("annotations", {})["storageclass.kubernetes.io/is-default-class"] = "true"
    prov = provisioner if provisioner is not None else c.s("provisioner")
    _check_driver(c, prov, "provisioner")
    m: dict[str, Any] = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": md,
                         "provisioner": prov}
    p = dict(params or {})
    extra = parse_params(c, c.raw("parameters") if provisioner is None else c.raw("extra_params"))
    for k, v in extra.items():
        if k in p and p[k] != v:
            c.warn(f"parameter {k} from the extra parameters overrides the form value ({p[k]} -> {v}).")
        p[k] = v
    if p:
        m["parameters"] = {k: str(v) for k, v in p.items()}
    rp = c.s("reclaim_policy") or "Delete"
    if rp not in RECLAIM_POLICIES:
        c.err("reclaimPolicy must be Delete or Retain.")
    m["reclaimPolicy"] = rp
    if c.b("allow_expansion"):
        m["allowVolumeExpansion"] = True
    mo = _csv_items(c.raw("mount_options"))
    if mo:
        m["mountOptions"] = mo
    bm = c.s("binding_mode") or "WaitForFirstConsumer"
    if bm not in BINDING_MODES:
        c.err("volumeBindingMode must be WaitForFirstConsumer or Immediate.")
    m["volumeBindingMode"] = bm
    topo = []
    for i, row in enumerate(_rows(c, "allowed_topologies"), 1):
        row = {**row_defaults("allowed_topologies"), **row}
        sc = _SubCtx(c, row, f"Allowed topology #{i}: ")
        key = sc.s("key")
        _check_label_key(sc, key, "label key") if key else sc.err("label key is required.")
        vals = _csv_items(sc.s("values"))
        if not vals:
            sc.err("at least one value is required.")
        topo.append({"key": key, "values": vals})
    if topo:
        m["allowedTopologies"] = [{"matchLabelExpressions": topo}]
        if bm == "Immediate":
            c.warn("allowedTopologies with Immediate binding pins volumes before the pod is scheduled; "
                   "WaitForFirstConsumer is usually what you want.")
    return m


def snapshot_class_doc(c: _Ctx, name: str, driver: str, deletion: str, default: bool, params: dict,
                       labels: dict | None = None) -> dict:
    _check_dns(c, name, "VolumeSnapshotClass name")
    _check_driver(c, driver, "VolumeSnapshotClass driver")
    if deletion not in DELETION_POLICIES:
        c.err("deletionPolicy must be Delete or Retain.")
    md: dict[str, Any] = {"name": name}
    if labels:
        md["labels"] = labels
    if default:
        md["annotations"] = {"snapshot.storage.kubernetes.io/is-default-class": "true"}
    m: dict[str, Any] = {"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshotClass",
                         "metadata": md, "driver": driver, "deletionPolicy": deletion}
    if params:
        m["parameters"] = {k: str(v) for k, v in params.items()}
    return m


def _build_snapshot_class(c: _Ctx) -> dict:
    md = _metadata(c, "VolumeSnapshotClass")
    m = snapshot_class_doc(c, md["name"], c.s("driver"), c.s("deletion_policy") or "Delete",
                           c.b("default_snapshot_class"), parse_params(c, c.raw("parameters")), md.get("labels"))
    if md.get("annotations"):
        m["metadata"].setdefault("annotations", {}).update(md["annotations"])
    return m


def generate(kind: str, values: dict | None = None) -> Result:
    """Build a manifest for *kind* from GUI-style string values (missing keys use defaults)."""
    if kind not in SCHEMA:
        return Result(None, [f"Unknown kind '{kind}'. Supported: {', '.join(KINDS)}"], [])
    merged = defaults(kind)
    merged.update(values or {})
    c = _Ctx(merged)
    try:
        if kind in WORKLOAD_KINDS:
            m = _build_workload(c, kind)
        elif kind == "Service":
            m = _build_service(c)
        elif kind == "Ingress":
            m = _build_ingress(c)
        elif kind == "ConfigMap":
            m = _build_configmap(c)
        elif kind == "Secret":
            m = _build_secret(c)
        elif kind == "PersistentVolumeClaim":
            m = _build_pvc(c)
        elif kind == "NetworkPolicy":
            m = _build_network_policy(c)
        elif kind == "StorageClass":
            m = storage_class_doc(c)
        elif kind == "VolumeSnapshotClass":
            m = _build_snapshot_class(c)
        else:
            m = _build_namespace(c)
    except Exception as exc:  # defensive: never crash the GUI on odd input
        c.err(f"Internal error while building {kind}: {exc!r}")
        m = None
    return Result(m, c.errors, c.warnings)


# --------------------------------------------------------------------------- #
# YAML output
# --------------------------------------------------------------------------- #
def _dumper():
    import yaml  # imported lazily so build/validate work without PyYAML

    class _Dumper(yaml.SafeDumper):
        pass

    def _str(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _Dumper.add_representer(str, _str)

    def _ignore_aliases(self, data):
        return True

    _Dumper.ignore_aliases = _ignore_aliases
    return yaml, _Dumper


def to_yaml(manifest: dict) -> str:
    yaml, D = _dumper()
    return yaml.dump(manifest, Dumper=D, sort_keys=False, default_flow_style=False,
                     allow_unicode=True, width=120)


def to_multi_yaml(manifests: list[dict]) -> str:
    return "".join("---\n" + to_yaml(m) for m in manifests)


def filename_for(manifest: dict) -> str:
    return f"{manifest['kind'].lower()}-{manifest['metadata']['name']}.yaml"


# Hover help for every field (see field_docs.py)
import field_docs as _field_docs  # noqa: E402
_field_docs.apply_manifest(SCHEMA)
