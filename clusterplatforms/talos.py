"""
Talos Linux: `talosctl gen config` inputs (talos-gen-config.sh) + machine-config patches in the
Talos 1.14 multi-document format (KubeNetworkConfig, KubeFlannelCNIConfig, KubeProxyConfig,
KubeNodeConfig, KubeletConfig, ResolverConfig, TimeSyncConfig). Validated by running
`talosctl gen config ... --config-patch @talos-patch.yaml` and
`talosctl validate --mode metal --strict` on the generated controlplane.yaml / worker.yaml
(talosctl v1.14.4). Talos < 1.14 uses the legacy v1alpha1 `machine:` / `cluster:` patch keys instead.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .common import (Field, ConfigResult, OutFile, _cidrs, _csv, _ip, _lines, _no_overlap, _parse_kv, _san,
                     required, shell_script, to_yaml, _DNS1123_SUB, TAINT_EFFECTS)

TYPE = "Talos"
GROUP = "Self-managed"

K8S = ["1.37.1", "1.36.4", "1.35.8"]
CNI = ["flannel (default)", "none (bring your own: Cilium, Calico...)"]
FLANNEL_BACKENDS = ["vxlan", "host-gw", "wireguard"]
PROXY_MODES = ["nftables", "iptables", "ipvs"]
MODES = ["metal", "cloud", "container"]
DISKS = ["/dev/sda", "/dev/vda", "/dev/nvme0n1", "/dev/xvda"]

TABLES: dict = {}
SCHEMA = [
    ("Cluster", [
        Field("tl_cluster_name", "cluster name", default="talos-lab", required=True),
        Field("tl_endpoint", "cluster endpoint", default="https://192.168.1.100:6443", required=True,
              help="https://<VIP / LB / first control-plane IP>:6443"),
        Field("tl_k8s_version", "--kubernetes-version", "ecombo", default=K8S[0], options=K8S),
        Field("tl_talos_version", "--talos-version", "ecombo", default="", options=["", "v1.14", "v1.13"],
              help="blank = this talosctl's version; older targets use the legacy v1alpha1 patch format"),
        Field("tl_sans", "--additional-sans", default="", help="comma-separated extra API cert SANs"),
        Field("tl_dns_domain", "--dns-domain", default="cluster.local"),
        Field("tl_install_disk", "--install-disk", "ecombo", default="/dev/sda", options=DISKS),
        Field("tl_install_image", "--install-image", default="",
              help="blank = default factory.talos.dev installer; use an Image Factory URL for extensions"),
        Field("tl_kubespan", "--with-kubespan", "check", default=False),
        Field("tl_discovery", "--with-cluster-discovery", "check", default=True),
        Field("tl_registry_mirrors", "--registry-mirror", "text", default="", height=2,
              help="registry=https://mirror per line, e.g. docker.io=https://mirror.gcr.io"),
        Field("tl_validate_mode", "validate --mode", "combo", default="metal", options=MODES),
        Field("tl_out_dir", "output dir", default="_out"),
    ]),
    ("Network (patch)", [
        Field("tl_pod_subnets", "podSubnets", default="10.244.0.0/16", help="IPv4 and/or IPv6, comma-separated"),
        Field("tl_service_subnets", "serviceSubnets", default="10.96.0.0/12"),
        Field("tl_cni", "CNI", "combo", default=CNI[0], options=CNI),
        Field("tl_flannel_backend", "flannel backendType", "combo", default="vxlan", options=FLANNEL_BACKENDS),
        Field("tl_proxy_mode", "kube-proxy mode", "combo", default="nftables", options=PROXY_MODES),
        Field("tl_proxy_disabled", "disable kube-proxy (KubeProxyConfig enabled: false)", "check", default=False),
        Field("tl_nameservers", "nameservers", default="", help="comma-separated IPs (ResolverConfig)"),
        Field("tl_ntp", "NTP servers", default="", help="comma-separated (TimeSyncConfig)"),
    ]),
    ("Nodes (patch)", [
        Field("tl_schedule_cp", "allow workloads on control planes", "check", default=False,
              help="removes the node-role.kubernetes.io/control-plane taint (control-plane patch)"),
        Field("tl_node_labels", "node labels (all nodes)", "text", default="", height=2, help="key=value per line"),
        Field("tl_node_taints", "node taints (workers)", "text", default="", height=2,
              help="key=value:Effect per line"),
        Field("tl_max_pods", "kubelet maxPods", default=""),
        Field("tl_kubelet_args", "kubelet extraArgs", "text", default="", height=2, help="name=value per line"),
    ]),
]
COLLAPSED = {"Nodes (patch)"}


def _doc(kind, **body):
    return {"apiVersion": "v1alpha1", "kind": kind, **body}


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    name = c.s("tl_cluster_name")
    if name and not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name):
        c.err(f"cluster name '{name}' must be lowercase letters, digits and '-'.")
    ep = c.s("tl_endpoint")
    if ep:
        u = urlparse(ep)
        if u.scheme != "https" or not u.hostname:
            c.err("cluster endpoint must be https://<host>:<port>, e.g. https://192.168.1.100:6443.")
        elif not u.port:
            c.warn("cluster endpoint has no port; Talos assumes 443 - the API server listens on 6443.")
        if u.hostname:
            _san(c, u.hostname, "cluster endpoint host")
    kv = c.s("tl_k8s_version")
    if kv and not re.match(r"^v?\d+\.\d+\.\d+$", kv):
        c.err(f"--kubernetes-version '{kv}' must look like 1.37.1.")
    tv = c.s("tl_talos_version")
    if tv and not re.match(r"^v\d+\.\d+$", tv):
        c.err("--talos-version must look like v1.14.")
    legacy = bool(tv) and tuple(int(x) for x in tv[1:].split(".")) < (1, 14)
    if legacy:
        c.warn(f"--talos-version {tv}: this patch uses the Talos 1.14 multi-document format; older Talos "
               "versions need the v1alpha1 machine:/cluster: patch layout.")
    sans = _csv(c.s("tl_sans"))
    for s in sans:
        _san(c, s, "--additional-sans")
    dd = c.s("tl_dns_domain")
    if dd and not _DNS1123_SUB.match(dd):
        c.err(f"--dns-domain '{dd}' is not a valid DNS domain.")
    disk = c.s("tl_install_disk")
    if disk and not disk.startswith("/dev/"):
        c.err("--install-disk must be a device path like /dev/sda.")
    img = c.s("tl_install_image")
    if img and not re.match(r"^[a-z0-9.\-]+(:\d+)?/[A-Za-z0-9._/\-]+(:[A-Za-z0-9._\-]+)?(@sha256:[0-9a-f]{64})?$", img):
        c.err(f"--install-image '{img}' is not an image reference.")
    mirrors = []
    for line in _lines(c.s("tl_registry_mirrors")):
        reg, eq, url = line.partition("=")
        if not eq or not re.match(r"^https?://", url.strip()):
            c.err(f"--registry-mirror '{line}' must be registry=https://mirror.")
        else:
            mirrors.append(f"{reg.strip()}={url.strip()}")
    out_dir = c.s("tl_out_dir") or "_out"

    # ---------------------------------------------------------------- patches
    common: list = []
    cp: list = []
    worker: list = []
    pods = _cidrs(c, c.s("tl_pod_subnets"), "podSubnets")
    svcs = _cidrs(c, c.s("tl_service_subnets"), "serviceSubnets")
    _no_overlap(c, pods, svcs, "podSubnets", "serviceSubnets")
    netdoc = {}
    if pods and [str(p) for p in pods] != ["10.244.0.0/16"]:
        netdoc["podSubnets"] = [str(p) for p in pods]
    if svcs and [str(s) for s in svcs] != ["10.96.0.0/12"]:
        netdoc["serviceSubnets"] = [str(s) for s in svcs]
    if netdoc:
        common.append(_doc("KubeNetworkConfig", **netdoc))
    no_cni = c.s("tl_cni").startswith("none")
    if no_cni:
        common.append(_doc("KubeFlannelCNIConfig", **{"$patch": "delete"}))
    elif (c.s("tl_flannel_backend") or "vxlan") != "vxlan":
        common.append(_doc("KubeFlannelCNIConfig", backendType=c.s("tl_flannel_backend")))
    if c.b("tl_proxy_disabled"):
        cp.append(_doc("KubeProxyConfig", enabled=False))
        if not no_cni:
            c.warn("kube-proxy disabled with Flannel: Services stop working unless you add a kube-proxy "
                   "replacement (e.g. Cilium with CNI none).")
    elif (c.s("tl_proxy_mode") or "nftables") != "nftables":
        cp.append(_doc("KubeProxyConfig", mode=c.s("tl_proxy_mode")))
        c.warn("Talos recommends kube-proxy mode nftables (its default)" +
               ("; ipvs is deprecated since Kubernetes v1.35." if c.s("tl_proxy_mode") == "ipvs" else "."))
    ns = _csv(c.s("tl_nameservers"))
    for n in ns:
        _ip(c, n, "nameserver")
    if ns:
        common.append(_doc("ResolverConfig", nameservers=[{"address": n} for n in ns]))
    ntp = _csv(c.s("tl_ntp"))
    for n in ntp:
        _san(c, n, "NTP server")
    if ntp:
        common.append(_doc("TimeSyncConfig", ntp={"servers": ntp}))
    labels = _parse_kv(c, c.s("tl_node_labels"), "node labels", label_rules=True)
    if labels:
        common.append(_doc("KubeNodeConfig", labels=labels))
    kubelet: dict = {}
    mp = c.int("tl_max_pods", "kubelet maxPods", lo=10, hi=2500)
    if mp:
        kubelet["config"] = {"maxPods": mp}
    ka = _parse_kv(c, c.s("tl_kubelet_args"), "kubelet extraArgs", split_commas=False)
    for k in ka:
        if k.startswith("-"):
            c.err(f"kubelet extraArgs '{k}': give the flag name without leading dashes.")
    if ka:
        kubelet["extraArgs"] = ka
    if kubelet:
        common.append(_doc("KubeletConfig", **kubelet))
    if c.b("tl_schedule_cp"):
        cp.append(_doc("KubeNodeConfig", taints={"node-role.kubernetes.io/control-plane": {"$patch": "delete"}}))
    taints = {}
    for line in _lines(c.s("tl_node_taints")):
        m = re.match(r"^([^=:\s]+)(?:=([^:\s]*))?:(\S+)$", line)
        if not m or m.group(3) not in TAINT_EFFECTS:
            c.err(f"node taint '{line}' must be key[=value]:Effect ({'/'.join(TAINT_EFFECTS)}).")
            continue
        taints[m.group(1)] = (f"{m.group(2)}:" if m.group(2) else "") + m.group(3)
    if taints:
        worker.append(_doc("KubeNodeConfig", taints=taints))

    def ydocs(docs):
        return "---\n".join(to_yaml(d) for d in docs)

    files = []
    flags: list = []
    if common:
        files.append(OutFile("talos-patch.yaml", "# Talos 1.14 machine-config patch (all nodes)\n" + ydocs(common)))
        flags.append(("--config-patch", "@talos-patch.yaml"))
    if cp:
        files.append(OutFile("talos-patch-controlplane.yaml", "# control-plane nodes only\n" + ydocs(cp)))
        flags.append(("--config-patch-control-plane", "@talos-patch-controlplane.yaml"))
    if worker:
        files.append(OutFile("talos-patch-worker.yaml", "# worker nodes only\n" + ydocs(worker)))
        flags.append(("--config-patch-worker", "@talos-patch-worker.yaml"))
    if kv:
        flags.append(("--kubernetes-version", kv.lstrip("v")))
    if tv:
        flags.append(("--talos-version", tv))
    if sans:
        flags.append(("--additional-sans", ",".join(sans)))
    if dd and dd != "cluster.local":
        flags.append(("--dns-domain", dd))
    if disk:
        flags.append(("--install-disk", disk))
    if img:
        flags.append(("--install-image", img))
    if c.b("tl_kubespan"):
        flags.append(("--with-kubespan", None))
    if not c.b("tl_discovery"):
        flags.append(("--with-cluster-discovery=false", None))
    for m in mirrors:
        flags.append(("--registry-mirror", m))
    flags += [("--with-docs=false", None), ("--with-examples=false", None), ("--output", out_dir)]
    mode = c.s("tl_validate_mode") or "metal"
    tc = '"${TALOSCTL:-talosctl}"'
    script = shell_script(
        f"talosctl gen config for cluster '{name}' - generated by k8s-manifest-gui",
        ["Creates controlplane.yaml, worker.yaml and talosconfig (keep them secret - they hold cluster CAs).",
         "Then: talosctl apply-config --insecure -n <node-ip> --file <out>/controlplane.yaml; "
         "talosctl bootstrap -n <cp-ip>"],
        [tc, "gen", "config", name or "talos-lab", ep or "https://127.0.0.1:6443"], flags,
        pre=['cd "$(dirname "$0")"'],
        post=["", f'{tc} validate --config {out_dir}/controlplane.yaml --mode {mode} --strict',
              f'{tc} validate --config {out_dir}/worker.yaml --mode {mode} --strict',
              f'echo "Machine configs written to {out_dir}/ (chmod 600 them)"'],
        passthrough=True)
    files.insert(0, OutFile("talos-gen-config.sh", script, 0o755))
    return ConfigResult(None, c.errors, c.warnings, "talos-gen-config.sh",
                        info="talosctl gen config + Talos 1.14 patches", files=files)


def check_row(key, sc):
    pass
