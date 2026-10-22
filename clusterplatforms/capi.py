"""
Cluster API (v1.14, v1beta2 contract) workload cluster on the Docker infrastructure provider (CAPD):
Cluster + DockerCluster + KubeadmControlPlane + DockerMachineTemplate(s) + MachineDeployment +
KubeadmConfigTemplate. Apply to a management cluster after `clusterctl init --infrastructure docker`.
Validated by installing the CAPI/CAPD CRDs into a kind cluster and `kubectl apply --dry-run=server`.
"""
from __future__ import annotations

import ipaddress
import re

from .common import (Field, ConfigResult, _cidrs, _csv, _no_overlap, _parse_kv, _san, _DNS1123_SUB, _DNS1123_LABEL,
                     required)

TYPE = "Cluster API"
GROUP = "Self-managed"

VERSIONS = ["v1.34.0", "v1.35.0", "v1.33.1", "v1.32.2"]
CORE, CP, BOOT, INFRA = ("cluster.x-k8s.io", "controlplane.cluster.x-k8s.io", "bootstrap.cluster.x-k8s.io",
                         "infrastructure.cluster.x-k8s.io")
V = "v1beta2"
CAPD_EVICTION = "nodefs.available<0%,nodefs.inodesFree<0%,imagefs.available<0%"

SCHEMA = [
    ("Cluster", [
        Field("ca_name", "cluster name", default="capd-demo", required=True),
        Field("ca_namespace", "namespace", default="default", required=True),
        Field("ca_provider", "infrastructure provider", "combo", default="docker (CAPD)", options=["docker (CAPD)"],
              help="CAPD runs nodes as containers - for dev/test; cloud providers need their own templates"),
        Field("ca_version", "Kubernetes version", "ecombo", default=VERSIONS[0], options=VERSIONS,
              help="needs a matching kindest/node image (customImage)"),
        Field("ca_cp_replicas", "control-plane replicas", "combo", default="1", options=["1", "3", "5"]),
        Field("ca_workers", "worker replicas (MachineDeployment)", default="1"),
        Field("ca_md_name", "MachineDeployment name suffix", default="md-0"),
        Field("ca_labels", "cluster labels", default="", help="e.g. cni=calico (for ClusterResourceSet matching)"),
    ]),
    ("Networking", [
        Field("ca_pods", "clusterNetwork.pods", default="192.168.0.0/16"),
        Field("ca_services", "clusterNetwork.services", default="10.128.0.0/12"),
        Field("ca_domain", "clusterNetwork.serviceDomain", default="cluster.local"),
        Field("ca_sans", "apiServer certSANs", default="", help="extra SANs, comma-separated"),
    ]),
    ("Machines", [
        Field("ca_image", "customImage", default="", help="blank = kindest/node:<version>"),
        Field("ca_relax_eviction", "relax disk eviction (CAPD quickstart default)", "check", default=True),
        Field("ca_worker_labels", "worker node-labels", default=""),
    ]),
]
COLLAPSED = {"Networking", "Machines"}


def _ref(group, kind, name):
    return {"apiGroup": group, "kind": kind, "name": name}


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    name, ns = c.s("ca_name"), c.s("ca_namespace")
    if name and (not _DNS1123_LABEL.match(name) or len(name) > 63):
        c.err("cluster name must be a DNS label (lowercase letters, digits, '-').")
    if ns and not _DNS1123_LABEL.match(ns):
        c.err("namespace must be a DNS label.")
    ver = c.s("ca_version")
    if ver and not re.match(r"^v\d+\.\d+\.\d+$", ver):
        c.err("Kubernetes version must be a full semver with 'v', e.g. v1.34.0.")
    cp = c.int("ca_cp_replicas", "control-plane replicas", lo=1, hi=7, required=True)
    if cp is not None and cp % 2 == 0:
        c.err("control-plane replicas must be odd (stacked etcd quorum).")
    workers = c.int("ca_workers", "worker replicas", lo=0, hi=100, required=True)
    md = c.s("ca_md_name") or "md-0"
    if not _DNS1123_LABEL.match(md):
        c.err("MachineDeployment suffix must be a DNS label.")
    pods = _cidrs(c, c.s("ca_pods"), "clusterNetwork.pods")
    svcs = _cidrs(c, c.s("ca_services"), "clusterNetwork.services")
    if not pods or not svcs:
        c.err("pod and service CIDRs are required.")
    _no_overlap(c, pods, svcs, "pods", "services")
    for n in pods + svcs:
        if n.version == 4 and n.overlaps(ipaddress.ip_network("172.18.0.0/16")):
            c.warn(f"{n} overlaps the kind docker network (172.18.0.0/16) CAPD nodes use.")
    dom = c.s("ca_domain") or "cluster.local"
    if not _DNS1123_SUB.match(dom):
        c.err("serviceDomain must be a DNS domain.")
    sans = _csv(c.s("ca_sans"))
    for s in sans:
        _san(c, s, "certSANs")
    labels = _parse_kv(c, c.s("ca_labels"), "cluster labels", label_rules=True)
    wl = _parse_kv(c, c.s("ca_worker_labels"), "worker node-labels", label_rules=True)
    image = c.s("ca_image")
    if image and ver and not image.endswith(":" + ver) and "@sha256:" not in image:
        c.warn(f"customImage {image} doesn't match version {ver}; kubelet/kubeadm come from the image.")

    meta = lambda n: {"name": n, "namespace": ns}  # noqa: E731
    kargs = [{"name": "eviction-hard", "value": CAPD_EVICTION}] if c.b("ca_relax_eviction") else []
    cp_name, cp_tmpl, wk_tmpl, md_name = f"{name}-control-plane", f"{name}-control-plane", f"{name}-{md}", f"{name}-{md}"
    cluster = {"apiVersion": f"{CORE}/{V}", "kind": "Cluster", "metadata": meta(name),
               "spec": {"clusterNetwork": {"pods": {"cidrBlocks": [str(n) for n in pods]},
                                           "services": {"cidrBlocks": [str(n) for n in svcs]},
                                           "serviceDomain": dom},
                        "controlPlaneRef": _ref(CP, "KubeadmControlPlane", cp_name),
                        "infrastructureRef": _ref(INFRA, "DockerCluster", name)}}
    if labels:
        cluster["metadata"]["labels"] = labels
    dcl = {"apiVersion": f"{INFRA}/{V}", "kind": "DockerCluster", "metadata": meta(name), "spec": {}}

    def dmt(n):
        spec = {"extraMounts": [{"containerPath": "/var/run/docker.sock", "hostPath": "/var/run/docker.sock"}]}
        if image:
            spec["customImage"] = image
        return {"apiVersion": f"{INFRA}/{V}", "kind": "DockerMachineTemplate", "metadata": meta(n),
                "spec": {"template": {"spec": spec}}}

    def nodereg(extra=None):
        a = list(kargs) + (extra or [])
        return {"nodeRegistration": {"kubeletExtraArgs": a}} if a else {}
    ccfg: dict = {}
    if sans:
        ccfg["apiServer"] = {"certSANs": sans}
    kcs: dict = {"initConfiguration": nodereg(), "joinConfiguration": nodereg()}
    if ccfg:
        kcs = {"clusterConfiguration": ccfg, **kcs}
    kcs = {k: v for k, v in kcs.items() if v}
    kcp = {"apiVersion": f"{CP}/{V}", "kind": "KubeadmControlPlane", "metadata": meta(cp_name),
           "spec": {"replicas": cp or 1, "version": ver,
                    "machineTemplate": {"spec": {"infrastructureRef": _ref(INFRA, "DockerMachineTemplate", cp_tmpl)}},
                    "kubeadmConfigSpec": kcs}}
    wl_arg = [{"name": "node-labels", "value": ",".join(f"{k}={v}" for k, v in wl.items())}] if wl else []
    for k in wl:
        if k.startswith("node-role.kubernetes.io/"):
            c.err(f"kubelet can't self-assign '{k}' via --node-labels (NodeRestriction); set it from a controller.")
        elif re.match(r"^([^/]*\.)?(kubernetes|k8s)\.io/", k) and not k.startswith("node.kubernetes.io/"):
            c.warn(f"label '{k}' uses a restricted kubernetes.io/k8s.io prefix; kubelet may refuse it.")
    kct = {"apiVersion": f"{BOOT}/{V}", "kind": "KubeadmConfigTemplate", "metadata": meta(wk_tmpl),
           "spec": {"template": {"spec": {"joinConfiguration": nodereg(wl_arg) or {"nodeRegistration": {}}}}}}
    mdep = {"apiVersion": f"{CORE}/{V}", "kind": "MachineDeployment", "metadata": meta(md_name),
            "spec": {"clusterName": name, "replicas": workers if workers is not None else 1,
                     "selector": {"matchLabels": {}},
                     "template": {"spec": {"clusterName": name, "version": ver,
                                           "bootstrap": {"configRef": _ref(BOOT, "KubeadmConfigTemplate", wk_tmpl)},
                                           "infrastructureRef": _ref(INFRA, "DockerMachineTemplate", wk_tmpl)}}}}
    docs = [cluster, dcl, kcp, dmt(cp_tmpl), mdep, dmt(wk_tmpl), kct]
    if workers == 0:
        c.warn("0 workers: workloads only run if you untaint the control plane.")
    c.warn("CAPI installs no CNI: apply one (e.g. Calico) to the workload cluster, or use a ClusterResourceSet.")
    header = ("# Management cluster: kind create cluster --config <kind with /var/run/docker.sock mount>\n"
              "#   clusterctl init --infrastructure docker\n"
              f"#   kubectl apply -f capi-{name}.yaml && clusterctl describe cluster {name} -n {ns}\n"
              f"#   clusterctl get kubeconfig {name} -n {ns} > {name}.kubeconfig\n")
    return ConfigResult(docs, c.errors, c.warnings, f"capi-{name or 'cluster'}.yaml", header=header,
                        info=f"Cluster API {V} / CAPD - {len(docs)} objects")
