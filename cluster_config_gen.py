"""
cluster_config_gen.py - headless generator for initial cluster configuration files.

No GUI dependency. Shares the Field schema / validation helpers with manifest_gen.

Types (CONFIG_TYPES):
  "kubeadm"     kubeadm.k8s.io/v1beta4 InitConfiguration + ClusterConfiguration
                (+ optional KubeletConfiguration / KubeProxyConfiguration), or JoinConfiguration
  "kind"        kind.x-k8s.io/v1alpha4 Cluster
  "kubeconfig"  v1 Config (clusters / users / contexts)
  "k3s / RKE2"  /etc/rancher/{k3s,rke2}/config.yaml
  plus the platform modules in clusterplatforms/ (one file each), grouped in GROUPS:
  Local: kind, minikube (config.json + start-minikube.sh), k3s / RKE2, k0s (k0sctl.yaml / k0s.yaml),
         MicroK8s (launch configuration)
  Self-managed: kubeadm, Talos (gen-config script + patches), Kubespray (inventory + group_vars),
         Cluster API (v1beta2 CAPD objects), OpenShift (install-config.yaml)
  Managed cloud: EKS (eksctl ClusterConfig), GKE (gcloud script), AKS (az aks create script)
  Access: kubeconfig

Public API:
  SCHEMA[type], defaults(type), TABLE_FIELDS, row_defaults(), check_row()
  generate(type, values) -> ConfigResult(docs, errors, warnings, filename, header, info)
  render(result) -> str      (preview text; multi-file results under '# ===== name =====' banners)
  outputs(result) -> [OutFile(name, text, mode)]   (every file to write)
"""
from __future__ import annotations

import base64
import binascii
import copy
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from manifest_gen import (Field, _Ctx, _SubCtx, _check_dns, _check_label_key, _parse_kv, _LABEL_NAME,  # noqa: F401
                          _DNS1123_SUB, _DNS1123_LABEL, to_yaml)
from clusterplatforms.common import (TABLE_FIELDS, ConfigResult, OutFile, outputs, render as _render,  # noqa: F401
                                     row_defaults, _TOKEN, _DURATION, _VERSION, _HASH, _HEX64, _GATE, _rows, _csv,
                                     _lines, _ip, _cidrs, _no_overlap, _port, _hostport, _san, _taints, TAINT_EFFECTS)

CONFIG_TYPES = ["kubeadm", "kind", "kubeconfig", "k3s / RKE2"]

KUBEADM_API = "kubeadm.k8s.io/v1beta4"      # kubeadm >= 1.31 (verified with kubeadm v1.37.1)
KUBELET_API = "kubelet.config.k8s.io/v1beta1"
KUBEPROXY_API = "kubeproxy.config.k8s.io/v1alpha1"
KIND_API = "kind.x-k8s.io/v1alpha4"

KUBEADM_OUTPUTS = ["init (InitConfiguration + ClusterConfiguration)", "join (JoinConfiguration)"]
CRI_SOCKETS = ["unix:///var/run/containerd/containerd.sock", "unix:///var/run/crio/crio.sock",
               "unix:///var/run/cri-dockerd.sock"]
CGROUP_DRIVERS = ["systemd", "cgroupfs"]
PROXY_MODES = ["iptables", "ipvs", "nftables"]
IMAGE_REPOS = ["registry.k8s.io", "k8s.gcr.io"]
KIND_ROLES = ["control-plane", "worker"]
KIND_IP_FAMILIES = ["ipv4", "ipv6", "dual"]
KIND_PROXY_MODES = ["", "iptables", "ipvs", "nftables", "none"]
KIND_IMAGES = ["", "kindest/node:v1.37.0", "kindest/node:v1.36.1", "kindest/node:v1.35.3"]
PROTOCOLS = ["TCP", "UDP", "SCTP"]
CA_MODES = ["certificate (path, PEM or base64)", "insecure-skip-tls-verify", "none (system CAs)"]
AUTH_METHODS = ["token", "client certificate", "username/password", "exec plugin", "none"]
EXEC_APIS = ["client.authentication.k8s.io/v1", "client.authentication.k8s.io/v1beta1"]
EXEC_COMMANDS = ["", "aws", "gke-gcloud-auth-plugin", "kubelogin", "kubectl-oidc_login", "oci", "doctl"]
EXEC_INTERACTIVE = ["IfAvailable", "Never", "Always"]
DISTROS = ["k3s", "rke2"]
NODE_ROLES = ["server", "agent"]
K3S_FLANNEL = ["", "vxlan", "host-gw", "wireguard-native", "none"]
RKE2_CNI = ["", "canal", "calico", "cilium", "flannel", "none"]
RKE2_INGRESS = ["", "traefik", "none", "ingress-nginx"]
K3S_DISABLE = ["traefik", "servicelb", "local-storage", "metrics-server", "coredns"]
RKE2_DISABLE = ["rke2-coredns", "rke2-metrics-server", "rke2-snapshot-controller", "rke2-snapshot-controller-crd",
                "rke2-snapshot-validation-webhook", "rke2-security-responder", "rke2-gateway-api-crd"]
CONFIG_PATHS = {"k3s": "/etc/rancher/k3s/config.yaml", "rke2": "/etc/rancher/rke2/config.yaml"}

# --------------------------------------------------------------------------- schema
TABLE_FIELDS.update({
    "nodes": [
        Field("role", "Role", "combo", default="worker", options=KIND_ROLES),
        Field("count", "Count", default="1", required=True),
        Field("image", "Node image", "ecombo", default="", options=KIND_IMAGES,
              help="blank = kind's default image; pin a version, ideally with @sha256 digest"),
        Field("labels", "Labels", default="", help="key=value, comma-separated"),
    ],
    "port_mappings": [
        Field("container_port", "containerPort", default="80", required=True),
        Field("host_port", "hostPort", default="80", required=True),
        Field("listen_address", "listenAddress", "ecombo", default="", options=["", "127.0.0.1", "0.0.0.0"],
              help="blank = 0.0.0.0"),
        Field("protocol", "Protocol", "combo", default="TCP", options=PROTOCOLS),
    ],
})


def _table(key, label, summary, help_="", default=None):
    return Field(key, label, "table", default=list(default or []), columns=TABLE_FIELDS[key],
                 summary=summary, help=help_)


SCHEMA: dict[str, list[tuple[str, list[Field]]]] = {
    "kubeadm": [
        ("Output", [
            Field("output", "Generate", "combo", default=KUBEADM_OUTPUTS[0], options=KUBEADM_OUTPUTS,
                  help="init -> kubeadm-config.yaml for `kubeadm init --config`; "
                       "join -> kubeadm-join-config.yaml for `kubeadm join --config`"),
        ]),
        ("InitConfiguration", [
            Field("advertise_address", "advertiseAddress", default="192.168.1.10", required=True,
                  help="IP of this control-plane node's API server"),
            Field("bind_port", "bindPort", default="6443"),
            Field("node_name", "nodeRegistration.name", default="", help="blank = hostname"),
            Field("cri_socket", "criSocket", "ecombo", default=CRI_SOCKETS[0], options=CRI_SOCKETS),
            Field("taints", "Taints", "text", default="", height=2,
                  help="key[=value]:Effect per line; blank = kubeadm default (control-plane:NoSchedule); "
                       "'none' = schedulable control plane"),
            Field("kubelet_extra_args", "kubeletExtraArgs", "text", default="", height=2,
                  help="name=value per line, e.g. node-ip=192.168.1.10"),
            Field("bootstrap_token", "Bootstrap token", default="",
                  help="[a-z0-9]{6}.[a-z0-9]{16}; blank = kubeadm generates one (`kubeadm token generate`)"),
            Field("token_ttl", "Token TTL", default="24h0m0s"),
        ]),
        ("ClusterConfiguration", [
            Field("kubernetes_version", "kubernetesVersion", "ecombo", default="v1.37.1",
                  options=["v1.37.1", "stable-1.37", "stable", "v1.36.4"]),
            Field("cluster_name", "clusterName", default="kubernetes"),
            Field("control_plane_endpoint", "controlPlaneEndpoint", default="",
                  help="host[:port] of a load balancer / VIP; set it now if you may add control-plane nodes later"),
            Field("pod_subnet", "podSubnet", default="10.244.0.0/16", help="must match your CNI (Flannel default 10.244.0.0/16)"),
            Field("service_subnet", "serviceSubnet", default="10.96.0.0/12"),
            Field("dns_domain", "dnsDomain", default="cluster.local"),
            Field("image_repository", "imageRepository", "ecombo", default="registry.k8s.io", options=IMAGE_REPOS),
            Field("cert_sans", "apiServer certSANs", default="", help="comma-separated IPs / DNS names"),
            Field("etcd_data_dir", "etcd local dataDir", default="/var/lib/etcd"),
        ]),
        ("KubeletConfiguration", [
            Field("include_kubelet", "Include KubeletConfiguration", "check", default=True),
            Field("cgroup_driver", "cgroupDriver", "combo", default="systemd", options=CGROUP_DRIVERS,
                  help="must match the container runtime (systemd for containerd/cri-o on systemd distros)"),
        ]),
        ("KubeProxyConfiguration", [
            Field("include_kube_proxy", "Include KubeProxyConfiguration", "check", default=False),
            Field("proxy_mode", "mode", "combo", default="iptables", options=PROXY_MODES,
                  help="nftables needs Kubernetes 1.31+ and a 5.13+ kernel"),
        ]),
        ("JoinConfiguration", [
            Field("join_endpoint", "apiServerEndpoint", default="",
                  help="host:port; blank = controlPlaneEndpoint or advertiseAddress:bindPort"),
            Field("join_token", "Token", default="", help="from `kubeadm token create --print-join-command`"),
            Field("ca_cert_hashes", "caCertHashes", "text", default="", height=2, help="sha256:<64 hex> per line"),
            Field("unsafe_skip_ca", "unsafeSkipCAVerification", "check", default=False),
            Field("join_control_plane", "Join as control-plane", "check", default=False),
            Field("certificate_key", "certificateKey", default="",
                  help="64 hex chars from `kubeadm init phase upload-certs --upload-certs`"),
        ]),
    ],
    "kind": [
        ("Cluster", [
            Field("name", "name", default="dev", required=True, help="cluster name (kind-<name> context)"),
            Field("feature_gates", "featureGates", "text", default="", height=2, help="Name=true|false per line"),
        ]),
        ("Nodes", [
            _table("nodes", "Nodes", ["role", "count", "image", "labels"],
                   "rows expand to `count` identical nodes; at least one control-plane",
                   default=[{"role": "control-plane", "count": "1", "image": "", "labels": ""},
                            {"role": "worker", "count": "2", "image": "", "labels": ""}]),
        ]),
        ("extraPortMappings (first control-plane)", [
            _table("port_mappings", "Port mappings", ["container_port", "host_port", "listen_address", "protocol"],
                   "host -> node port forwards (e.g. 80/443 for an ingress controller)"),
        ]),
        ("Networking", [
            Field("ip_family", "ipFamily", "combo", default="ipv4", options=KIND_IP_FAMILIES),
            Field("api_server_address", "apiServerAddress", default="", help="blank = 127.0.0.1 (recommended)"),
            Field("api_server_port", "apiServerPort", default="", help="blank = random free port"),
            Field("pod_subnet", "podSubnet", default="", help="blank = 10.244.0.0/16"),
            Field("service_subnet", "serviceSubnet", default="", help="blank = 10.96.0.0/16"),
            Field("disable_default_cni", "disableDefaultCNI", "check", default=False,
                  help="install your own CNI (Calico, Cilium...) afterwards"),
            Field("kube_proxy_mode", "kubeProxyMode", "combo", default="", options=KIND_PROXY_MODES,
                  help="blank = iptables"),
        ]),
    ],
    "kubeconfig": [
        ("Cluster", [
            Field("cluster_name", "Cluster name", default="my-cluster", required=True),
            Field("server", "Server URL", default="https://192.168.1.10:6443", required=True),
            Field("ca_mode", "CA", "combo", default=CA_MODES[0], options=CA_MODES),
            Field("ca", "CA certificate", "text", default="", height=3,
                  help="file path, pasted PEM (base64-encoded automatically) or base64 data"),
            Field("tls_server_name", "tls-server-name", default=""),
            Field("proxy_url", "proxy-url", default=""),
        ]),
        ("User / auth", [
            Field("user_name", "User name", default="admin", required=True),
            Field("auth_method", "Auth method", "combo", default="token", options=AUTH_METHODS),
            Field("token", "Token", default=""),
            Field("client_cert", "Client certificate", "text", default="", height=3, help="path, PEM or base64"),
            Field("client_key", "Client key", "text", default="", height=3, help="path, PEM or base64"),
            Field("username", "Username", default=""),
            Field("password", "Password", default=""),
        ]),
        ("Exec plugin", [
            Field("exec_api_version", "apiVersion", "combo", default=EXEC_APIS[0], options=EXEC_APIS),
            Field("exec_command", "command", "ecombo", default="", options=EXEC_COMMANDS),
            Field("exec_args", "args", "text", default="", height=3, help="one argument per line"),
            Field("exec_env", "env", "text", default="", height=2, help="NAME=value per line"),
            Field("exec_interactive", "interactiveMode", "combo", default="IfAvailable", options=EXEC_INTERACTIVE),
            Field("exec_provide_cluster_info", "provideClusterInfo", "check", default=False),
        ]),
        ("Context", [
            Field("context_name", "Context name", default="", help="blank = <user>@<cluster>"),
            Field("namespace", "Namespace", default=""),
            Field("set_current", "Set current-context", "check", default=True),
        ]),
    ],
    "k3s / RKE2": [
        ("Distribution", [
            Field("distro", "Distribution", "combo", default="k3s", options=DISTROS,
                  help="save as /etc/rancher/k3s/config.yaml or /etc/rancher/rke2/config.yaml"),
            Field("role", "Node role", "combo", default="server", options=NODE_ROLES,
                  help="agent = worker joining an existing server"),
            Field("cluster_init", "cluster-init (k3s, first server, embedded etcd)", "check", default=False),
        ]),
        ("Join / identity", [
            Field("token", "token", default="", help="shared secret; required to join"),
            Field("server_url", "server", default="",
                  help="join URL: k3s https://<server>:6443, RKE2 https://<server>:9345; blank on the first server"),
            Field("node_name", "node-name", default=""),
            Field("node_ip", "node-ip", default="", help="comma-separated (dual-stack)"),
            Field("node_external_ip", "node-external-ip", default=""),
        ]),
        ("Server networking", [
            Field("tls_san", "tls-san", default="", help="comma-separated hostnames / IPs (server only)"),
            Field("cluster_cidr", "cluster-cidr", default="", help="blank = 10.42.0.0/16"),
            Field("service_cidr", "service-cidr", default="", help="blank = 10.43.0.0/16"),
            Field("cluster_dns", "cluster-dns", default="", help="blank = 10.43.0.10; must be inside service-cidr"),
            Field("cluster_domain", "cluster-domain", default=""),
            Field("flannel_backend", "flannel-backend (k3s)", "combo", default="", options=K3S_FLANNEL,
                  help="blank = vxlan; none = bring your own CNI"),
            Field("cni", "cni (RKE2)", "combo", default="", options=RKE2_CNI, help="blank = canal"),
            Field("multus", "multus (RKE2, prepended to cni)", "check", default=False),
            Field("ingress_controller", "ingress-controller (RKE2)", "combo", default="", options=RKE2_INGRESS),
            Field("write_kubeconfig_mode", "write-kubeconfig-mode", default="",
                  help="server only; blank = 0600; 0644 lets non-root users on the node read it"),
        ]),
        ("Disable components (k3s)", [
            Field(f"k3s_disable_{c.replace('-', '_')}", c, "check", default=False) for c in K3S_DISABLE
        ]),
        ("Disable components (RKE2)", [
            Field(f"rke2_disable_{c.replace('-', '_')}", c, "check", default=False) for c in RKE2_DISABLE
        ]),
        ("Node labels / taints", [
            Field("node_label", "node-label", "text", default="", height=2, help="key=value per line"),
            Field("node_taint", "node-taint", "text", default="", height=2, help="key=value:Effect per line"),
        ]),
    ],
}

DEFAULT_COLLAPSED = {"JoinConfiguration", "KubeProxyConfiguration", "Exec plugin", "Disable components (RKE2)"}


def defaults(ctype: str) -> dict:
    return {f.key: copy.deepcopy(f.default) for _, fields in SCHEMA[ctype] for f in fields}


def render(res: ConfigResult) -> str:
    return _render(res)


# --------------------------------------------------------------------------- kubeadm
def _node_registration(c) -> dict:
    nr: dict[str, Any] = {}
    if c.s("node_name"):
        _check_dns(c, c.s("node_name").lower(), "nodeRegistration.name")
        nr["name"] = c.s("node_name")
    sock = c.s("cri_socket")
    if sock:
        if not re.match(r"^(unix|npipe)://", sock):
            c.err(f"criSocket '{sock}' must be a URL like unix:///var/run/containerd/containerd.sock.")
        nr["criSocket"] = sock
    tx = c.raw("taints").strip()
    if tx.lower() == "none":
        nr["taints"] = []
    elif tx:
        nr["taints"] = _taints(c, tx, "Taint")
    args = []
    for line in _lines(c.raw("kubelet_extra_args")):
        if "=" not in line:
            c.err(f"kubeletExtraArgs '{line}' must be name=value.")
            continue
        k, v = line.split("=", 1)
        k = k.strip().lstrip("-")
        args.append({"name": k, "value": v.strip()})
    if args:
        nr["kubeletExtraArgs"] = args  # v1beta4: list of {name, value}
    return nr


def _gen_kubeadm(c) -> ConfigResult:
    join = c.s("output").startswith("join")
    adv = c.s("advertise_address")
    bind = _port(c, c.s("bind_port"), "bindPort", default=6443)
    nr = _node_registration(c)
    docs = []
    if not join:
        _ip(c, adv, "advertiseAddress", required=True)
        init: dict[str, Any] = {"apiVersion": KUBEADM_API, "kind": "InitConfiguration"}
        tok = c.s("bootstrap_token")
        ttl = c.s("token_ttl") or "24h0m0s"
        if not _DURATION.match(ttl):
            c.err(f"Token TTL '{ttl}' must be a Go duration like 24h0m0s or 2h.")
        elif not tok and ttl != "24h0m0s":
            c.warn("Token TTL only applies when a bootstrap token is set (kubeadm's generated token uses 24h).")
        if tok:
            if not _TOKEN.match(tok):
                c.err("Bootstrap token must match [a-z0-9]{6}.[a-z0-9]{16} (e.g. abcdef.0123456789abcdef).")
            init["bootstrapTokens"] = [{"token": tok, "ttl": ttl,
                                        "groups": ["system:bootstrappers:kubeadm:default-node-token"],
                                        "usages": ["signing", "authentication"]}]
        init["localAPIEndpoint"] = {"advertiseAddress": adv, "bindPort": bind}
        if nr:
            init["nodeRegistration"] = nr
        docs.append(init)

        cc: dict[str, Any] = {"apiVersion": KUBEADM_API, "kind": "ClusterConfiguration"}
        ver = c.s("kubernetes_version")
        if ver:
            if not _VERSION.match(ver):
                c.err(f"kubernetesVersion '{ver}' must look like v1.37.1, stable-1.37 or stable.")
            cc["kubernetesVersion"] = ver
        cn = c.s("cluster_name")
        if cn:
            if not _DNS1123_SUB.match(cn):
                c.err(f"clusterName '{cn}' must be a DNS-1123 subdomain (lowercase, digits, '-', '.').")
            cc["clusterName"] = cn
        cpe = c.s("control_plane_endpoint")
        if cpe:
            _hostport(c, cpe, "controlPlaneEndpoint")
            cc["controlPlaneEndpoint"] = cpe
        else:
            c.warn("No controlPlaneEndpoint: you cannot add more control-plane nodes later without re-creating certs.")
        pods = _cidrs(c, c.s("pod_subnet"), "podSubnet")
        svcs = _cidrs(c, c.s("service_subnet"), "serviceSubnet")
        _no_overlap(c, pods, svcs, "podSubnet", "serviceSubnet")
        try:
            a = ipaddress.ip_address(adv)
            for n in pods + svcs:
                if a in n:
                    c.err(f"advertiseAddress {adv} is inside {n}; pod/service subnets must not include node IPs.")
        except ValueError:
            pass
        net: dict[str, Any] = {}
        if c.s("dns_domain"):
            if not _DNS1123_SUB.match(c.s("dns_domain")):
                c.err(f"dnsDomain '{c.s('dns_domain')}' must be a DNS domain like cluster.local.")
            net["dnsDomain"] = c.s("dns_domain")
        if c.s("pod_subnet"):
            net["podSubnet"] = c.s("pod_subnet").replace(" ", "")
        else:
            c.warn("No podSubnet: most CNIs (Flannel, Calico's default manifests) expect one.")
        if c.s("service_subnet"):
            net["serviceSubnet"] = c.s("service_subnet").replace(" ", "")
        if net:
            cc["networking"] = net
        if c.s("image_repository"):
            cc["imageRepository"] = c.s("image_repository")
        sans = _csv(c.s("cert_sans"))
        for s_ in sans:
            _san(c, s_, "certSAN")
        if sans:
            cc["apiServer"] = {"certSANs": sans}
        dd = c.s("etcd_data_dir")
        if dd:
            if not dd.startswith("/"):
                c.err("etcd dataDir must be an absolute path.")
            cc["etcd"] = {"local": {"dataDir": dd}}
        docs.append(cc)
        if c.b("include_kubelet"):
            cg = c.s("cgroup_driver") or "systemd"
            if cg not in CGROUP_DRIVERS:
                c.err("cgroupDriver must be systemd or cgroupfs.")
            docs.append({"apiVersion": KUBELET_API, "kind": "KubeletConfiguration", "cgroupDriver": cg})
        if c.b("include_kube_proxy"):
            mode = c.s("proxy_mode") or "iptables"
            if mode not in PROXY_MODES:
                c.err(f"kube-proxy mode must be one of {', '.join(PROXY_MODES)}.")
            if mode == "ipvs":
                c.warn("ipvs kube-proxy mode is deprecated since Kubernetes v1.35; prefer nftables (GA since v1.33) or iptables.")
            kp: dict[str, Any] = {"apiVersion": KUBEPROXY_API, "kind": "KubeProxyConfiguration", "mode": mode}
            if c.s("pod_subnet"):
                kp["clusterCIDR"] = c.s("pod_subnet").replace(" ", "")
            docs.append(kp)
        return ConfigResult(docs, c.errors, c.warnings, "kubeadm-config.yaml",
                            "# kubeadm init --config kubeadm-config.yaml\n", KUBEADM_API + " (init)")

    # ---- join
    ep = c.s("join_endpoint") or c.s("control_plane_endpoint") or (f"{adv}:{bind}" if adv else "")
    if not ep:
        c.err("apiServerEndpoint is required for join (or set controlPlaneEndpoint / advertiseAddress).")
    else:
        _hostport(c, ep, "apiServerEndpoint")
        if not re.search(r":\d+$", ep):
            c.warn(f"apiServerEndpoint '{ep}' has no port; kubeadm will use 6443.")
    tok = c.s("join_token") or c.s("bootstrap_token")
    if not tok:
        c.err("Join token is required (kubeadm token create --print-join-command).")
    elif not _TOKEN.match(tok):
        c.err("Join token must match [a-z0-9]{6}.[a-z0-9]{16}.")
    bt: dict[str, Any] = {"apiServerEndpoint": ep, "token": tok}
    hashes = _lines(c.raw("ca_cert_hashes"))
    for h in hashes:
        if not _HASH.match(h):
            c.err(f"caCertHash '{h[:20]}…' must be sha256:<64 lowercase hex>.")
    if hashes:
        bt["caCertHashes"] = hashes
        if c.b("unsafe_skip_ca"):
            c.warn("caCertHashes given; unsafeSkipCAVerification is ignored and omitted.")
    elif c.b("unsafe_skip_ca"):
        bt["unsafeSkipCAVerification"] = True
        c.warn("unsafeSkipCAVerification disables CA pinning - only for labs.")
    else:
        c.err("Set at least one caCertHash, or tick unsafeSkipCAVerification (not recommended).")
    jc: dict[str, Any] = {"apiVersion": KUBEADM_API, "kind": "JoinConfiguration",
                          "discovery": {"bootstrapToken": bt}}
    if nr:
        jc["nodeRegistration"] = nr
    if c.b("join_control_plane"):
        _ip(c, adv, "advertiseAddress (this joining control-plane node)", required=True)
        cp: dict[str, Any] = {"localAPIEndpoint": {"advertiseAddress": adv, "bindPort": bind}}
        ck = c.s("certificate_key")
        if ck:
            if not _HEX64.match(ck):
                c.err("certificateKey must be 64 lowercase hex characters.")
            cp["certificateKey"] = ck
        else:
            c.warn("No certificateKey: copy the control-plane certificates to this node manually before joining.")
        jc["controlPlane"] = cp
    return ConfigResult([jc], c.errors, c.warnings, "kubeadm-join-config.yaml",
                        "# kubeadm join --config kubeadm-join-config.yaml\n", KUBEADM_API + " (join)")


# --------------------------------------------------------------------------- kind
def _gen_kind(c) -> ConfigResult:
    name = c.s("name")
    if not name:
        c.err("Cluster name is required.")
    elif len(name) > 50 or not _DNS1123_LABEL.match(name):
        c.err(f"Cluster name '{name}' must be lowercase letters, digits and '-' (max 50).")
    cfg: dict[str, Any] = {"kind": "Cluster", "apiVersion": KIND_API, "name": name}
    gates: dict[str, bool] = {}
    for line in _lines(c.raw("feature_gates")):
        if "=" not in line:
            c.err(f"featureGate '{line}' must be Name=true|false.")
            continue
        k, v = (x.strip() for x in line.split("=", 1))
        if not _GATE.match(k):
            c.err(f"featureGate name '{k}' must be CamelCase like InPlacePodVerticalScaling.")
        if v.lower() not in ("true", "false"):
            c.err(f"featureGate {k} value must be true or false.")
        gates[k] = v.lower() == "true"
    if gates:
        cfg["featureGates"] = gates

    fam = c.s("ip_family") or "ipv4"
    net: dict[str, Any] = {}
    if fam != "ipv4":
        net["ipFamily"] = fam
    addr = c.s("api_server_address")
    if addr:
        ip = _ip(c, addr, "apiServerAddress")
        if ip is not None and not ip.is_loopback:
            c.warn(f"apiServerAddress {addr} exposes the API server beyond localhost - kind is not hardened for that.")
        net["apiServerAddress"] = addr
    port = _port(c, c.s("api_server_port"), "apiServerPort")
    if port is not None:
        net["apiServerPort"] = port
    for key, k8s in (("pod_subnet", "podSubnet"), ("service_subnet", "serviceSubnet")):
        if c.s(key):
            nets = _cidrs(c, c.s(key), k8s)
            fams = {n.version for n in nets}
            if fam == "ipv4" and 6 in fams or fam == "ipv6" and 4 in fams:
                c.err(f"{k8s} {c.s(key)} does not match ipFamily {fam}.")
            if fam == "dual" and nets and len(fams) != 2:
                c.err(f"{k8s}: dual-stack needs an IPv4 and an IPv6 CIDR (comma-separated).")
            net[k8s] = c.s(key).replace(" ", "")
    if c.s("pod_subnet") and c.s("service_subnet"):
        _no_overlap(c, _cidrs(_Ctx({}), c.s("pod_subnet"), ""), _cidrs(_Ctx({}), c.s("service_subnet"), ""),
                    "podSubnet", "serviceSubnet")
    if c.b("disable_default_cni"):
        net["disableDefaultCNI"] = True
        c.warn("disableDefaultCNI: nodes stay NotReady until you install a CNI.")
    kpm = c.s("kube_proxy_mode")
    if kpm:
        if kpm not in KIND_PROXY_MODES:
            c.err(f"kubeProxyMode must be one of {', '.join(m for m in KIND_PROXY_MODES if m)}.")
        net["kubeProxyMode"] = kpm
        if kpm == "ipvs":
            c.warn("ipvs kube-proxy mode is deprecated since Kubernetes v1.35; prefer nftables (GA since v1.33) or iptables.")
        if kpm == "none" and not c.b("disable_default_cni"):
            c.warn("kubeProxyMode none: Services won't work unless your CNI replaces kube-proxy (e.g. Cilium).")
    if net:
        cfg["networking"] = net

    nodes = []
    first_cp = None
    cps = 0
    for i, r in enumerate(_rows(c, "nodes")):
        sc = _SubCtx(c, r, f"Node row #{i + 1}: ")
        role = sc.s("role") or "worker"
        if role not in KIND_ROLES:
            sc.err("role must be control-plane or worker.")
        cnt = sc.int("count", "count", lo=1, hi=20, required=True) or 0
        img = sc.s("image")
        if img and re.search(r"\s", img):
            sc.err(f"image '{img}' must not contain spaces.")
        labels = _parse_kv(sc, sc.s("labels"), "labels", label_rules=True)
        for _ in range(cnt):
            n: dict[str, Any] = {"role": role}
            if img:
                n["image"] = img
            if labels:
                n["labels"] = dict(labels)
            nodes.append(n)
            if role == "control-plane":
                cps += 1
                if first_cp is None:
                    first_cp = n
    if cps == 0:
        c.err("At least one control-plane node is required.")
    elif cps == 2 or (cps > 1 and cps % 2 == 0):
        c.warn(f"{cps} control-plane nodes: use an odd number (1, 3, 5) for etcd quorum.")
    if len(nodes) > 8:
        c.warn(f"{len(nodes)} nodes: each kind node is a container - make sure the host has enough RAM.")
    images = {n.get("image", "") for n in nodes}
    if len(images) > 1:
        c.warn("Nodes use different images; mixing Kubernetes versions is only supported within skew policy.")

    maps = []
    seen = set()
    for i, r in enumerate(_rows(c, "port_mappings")):
        sc = _SubCtx(c, r, f"Port mapping #{i + 1}: ")
        cp_ = _port(sc, sc.s("container_port"), "containerPort", required=True)
        hp = _port(sc, sc.s("host_port"), "hostPort", required=True)
        proto = sc.s("protocol") or "TCP"
        if proto not in PROTOCOLS:
            sc.err("protocol must be TCP, UDP or SCTP.")
        la = sc.s("listen_address")
        if la:
            _ip(sc, la, "listenAddress")
        key = (hp, proto, la or "0.0.0.0")
        if key in seen:
            sc.err(f"hostPort {hp}/{proto} is mapped twice.")
        seen.add(key)
        if hp is not None and hp < 1024:
            c.warn(f"hostPort {hp} < 1024 may need root / Docker Desktop privileges.")
        m: dict[str, Any] = {"containerPort": cp_, "hostPort": hp}
        if la:
            m["listenAddress"] = la
        if proto != "TCP":
            m["protocol"] = proto
        maps.append(m)
    if maps and first_cp is not None:
        first_cp["extraPortMappings"] = maps
    cfg["nodes"] = nodes
    return ConfigResult([cfg], c.errors, c.warnings, "kind-config.yaml",
                        f"# kind create cluster --config kind-config.yaml\n", KIND_API)


# --------------------------------------------------------------------------- kubeconfig
def _material(c, value: str, label: str, pem_kind: str):
    """Return (kind, value): ('data', base64) for PEM/base64 input, ('path', path) otherwise."""
    v = value.strip()
    if not v:
        return None, None
    if "-----BEGIN" in v:
        if pem_kind not in v:
            c.err(f"{label}: pasted PEM does not contain a {pem_kind} block.")
        if "-----END" not in v:
            c.err(f"{label}: pasted PEM is truncated (no -----END line).")
        for body in re.findall(r"-----BEGIN [^-]+-----(.*?)-----END", v, re.S):
            b = "".join(ln.strip() for ln in body.splitlines() if ln.strip() and ":" not in ln)
            try:
                base64.b64decode(b, validate=True)
            except (binascii.Error, ValueError):
                c.err(f"{label}: pasted PEM body is not valid base64 (copy/paste damage?).")
                break
        return "data", base64.b64encode((v + "\n").encode()).decode()
    compact = re.sub(r"\s+", "", v)
    if len(compact) > 40 and re.match(r"^[A-Za-z0-9+/=]+$", compact):
        try:
            dec = base64.b64decode(compact, validate=True)
            if b"-----BEGIN" not in dec:
                c.warn(f"{label}: base64 data does not decode to PEM.")
            return "data", compact
        except (binascii.Error, ValueError):
            c.err(f"{label}: looks like base64 but does not decode.")
            return None, None
    if "\n" in v:
        c.err(f"{label}: expected a file path, a PEM block, or base64 data.")
        return None, None
    if v.startswith("~"):
        c.warn(f"{label}: kubectl does not expand '~' in kubeconfig paths; use an absolute path.")
    elif not v.startswith("/") and not re.match(r"^[A-Za-z]:\\\\", v):
        c.warn(f"{label}: relative path '{v}' is resolved relative to the kubeconfig file's directory.")
    return "path", v


def _gen_kubeconfig(c) -> ConfigResult:
    cname, uname = c.s("cluster_name"), c.s("user_name")
    if not cname:
        c.err("Cluster name is required.")
    if not uname:
        c.err("User name is required.")
    for n, lbl in ((cname, "Cluster name"), (uname, "User name")):
        if n and re.search(r"\s", n):
            c.err(f"{lbl} '{n}' must not contain whitespace.")
    server = c.s("server")
    cl: dict[str, Any] = {}
    if not server:
        c.err("Server URL is required (e.g. https://192.168.1.10:6443).")
    else:
        u = urlparse(server)
        if u.scheme not in ("https", "http") or not u.hostname:
            c.err(f"Server URL '{server}' must look like https://host:port.")
        elif u.scheme == "http":
            c.warn("Server URL uses plain http - credentials are sent unencrypted.")
        try:
            u.port
        except ValueError:
            c.err(f"Server URL '{server}' has an invalid port.")
    cl["server"] = server
    mode = c.s("ca_mode") or CA_MODES[0]
    ca = c.raw("ca").strip()
    if mode.startswith("insecure"):
        if ca:
            c.err("insecure-skip-tls-verify cannot be combined with a CA certificate (kubectl rejects it).")
        cl["insecure-skip-tls-verify"] = True
        c.warn("insecure-skip-tls-verify disables server certificate checks.")
    elif mode.startswith("certificate"):
        kind_, val = _material(c, ca, "CA certificate", "CERTIFICATE")
        if kind_ == "data":
            cl["certificate-authority-data"] = val
        elif kind_ == "path":
            cl["certificate-authority"] = val
        else:
            c.err("CA certificate is empty: paste the CA PEM / base64, give a path, or pick another CA option.")
    if c.s("tls_server_name"):
        cl["tls-server-name"] = c.s("tls_server_name")
    if c.s("proxy_url"):
        pu = urlparse(c.s("proxy_url"))
        if pu.scheme not in ("http", "https", "socks5") or not pu.hostname:
            c.err("proxy-url must be http://, https:// or socks5://host:port.")
        cl["proxy-url"] = c.s("proxy_url")

    auth = c.s("auth_method") or "token"
    user: dict[str, Any] = {}
    if auth == "token":
        if not c.s("token"):
            c.err("Token is required for token auth.")
        user["token"] = c.s("token")
    elif auth == "client certificate":
        ck, cv = _material(c, c.raw("client_cert"), "Client certificate", "CERTIFICATE")
        kk, kv = _material(c, c.raw("client_key"), "Client key", "PRIVATE KEY")
        if not ck or not kk:
            c.err("Client certificate auth needs both a client certificate and a client key.")
        if ck:
            user["client-certificate-data" if ck == "data" else "client-certificate"] = cv
        if kk:
            user["client-key-data" if kk == "data" else "client-key"] = kv
    elif auth == "username/password":
        if not c.s("username") or not c.s("password"):
            c.err("Username and password are both required.")
        user["username"], user["password"] = c.s("username"), c.s("password")
        c.warn("Basic auth is not supported by Kubernetes API servers since v1.19 (only by some proxies).")
    elif auth == "exec plugin":
        cmd = c.s("exec_command")
        if not cmd:
            c.err("Exec plugin command is required (e.g. aws, kubelogin, gke-gcloud-auth-plugin).")
        api = c.s("exec_api_version") or EXEC_APIS[0]
        ex: dict[str, Any] = {"apiVersion": api, "command": cmd}
        args = _lines(c.raw("exec_args"))
        if args:
            ex["args"] = args
        env = []
        for line in _lines(c.raw("exec_env")):
            if "=" not in line:
                c.err(f"Exec env '{line}' must be NAME=value.")
                continue
            k, v = line.split("=", 1)
            env.append({"name": k.strip(), "value": v.strip()})
        if env:
            ex["env"] = env
        im = c.s("exec_interactive") or "IfAvailable"
        if im not in EXEC_INTERACTIVE:
            c.err("interactiveMode must be IfAvailable, Never or Always.")
        ex["interactiveMode"] = im
        if c.b("exec_provide_cluster_info"):
            ex["provideClusterInfo"] = True
        user["exec"] = ex
    elif auth != "none":
        c.err(f"Unknown auth method '{auth}'.")
    ctx_name = c.s("context_name") or f"{uname}@{cname}"
    ctx: dict[str, Any] = {"cluster": cname, "user": uname}
    ns = c.s("namespace")
    if ns:
        _check_dns(c, ns, "Namespace", "label")
        ctx["namespace"] = ns
    cfg: dict[str, Any] = {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": cname, "cluster": cl}],
        "users": [{"name": uname, "user": user}],
        "contexts": [{"name": ctx_name, "context": ctx}],
    }
    if c.b("set_current"):
        cfg["current-context"] = ctx_name
    cfg["preferences"] = {}
    fname = re.sub(r"[^A-Za-z0-9._-]+", "-", cname or "cluster").strip("-") + ".kubeconfig"
    if user.get("token") or "client-key-data" in user or user.get("password"):
        c.warn("This kubeconfig contains credentials - it is saved with mode 600; keep it private.")
    return ConfigResult([cfg], c.errors, c.warnings, fname,
                        f"# kubectl --kubeconfig {fname} get nodes   (or merge into ~/.kube/config)\n", "kubeconfig v1")


# --------------------------------------------------------------------------- k3s / RKE2
SERVER_ONLY = {"tls-san", "cluster-cidr", "service-cidr", "cluster-dns", "cluster-domain", "flannel-backend",
               "cni", "ingress-controller", "disable", "write-kubeconfig-mode", "cluster-init"}


def _gen_rancher(c) -> ConfigResult:
    distro = c.s("distro") or "k3s"
    if distro not in DISTROS:
        c.err("Distribution must be k3s or rke2.")
        distro = "k3s"
    role = c.s("role") or "server"
    agent = role == "agent"
    cfg: dict[str, Any] = {}
    tok, url = c.s("token"), c.s("server_url")
    if url:
        u = urlparse(url)
        if u.scheme != "https" or not u.hostname:
            c.err(f"server '{url}' must be https://<host>:<port>.")
        else:
            want = 9345 if distro == "rke2" else 6443
            try:
                port = u.port
            except ValueError:
                port = None
                c.err(f"server '{url}' has an invalid port.")
            if port is None:
                c.err(f"server URL needs an explicit port (https://{u.hostname}:{want}).")
            elif port != want:
                c.warn(f"{distro} joins via port {want} (got {port}).")
        cfg["server"] = url
    if tok:
        if len(tok) < 8 and not tok.startswith("K10"):
            c.warn("token is very short; use a long random secret (e.g. `openssl rand -hex 32`).")
        cfg["token"] = tok
    if agent:
        if not url:
            c.err("An agent needs the server URL to join.")
        if not tok:
            c.err("An agent needs the cluster token to join.")
    elif url and not tok:
        c.err("A server joining an existing cluster needs the token.")
    if c.b("cluster_init"):
        if distro != "k3s":
            c.err("cluster-init is a k3s option; RKE2 always uses embedded etcd on the first server.")
        elif agent:
            c.err("cluster-init only applies to servers.")
        elif url:
            c.err("cluster-init is for the FIRST server; joining servers set server: instead.")
        else:
            cfg["cluster-init"] = True
    if c.s("node_name"):
        nn = c.s("node_name")
        if not _DNS1123_SUB.match(nn):
            c.err(f"node-name '{nn}' must be lowercase DNS-1123 (letters, digits, '-', '.').")
        cfg["node-name"] = nn
    for key, flag in (("node_ip", "node-ip"), ("node_external_ip", "node-external-ip")):
        ips = _csv(c.s(key))
        for ip in ips:
            _ip(c, ip, flag)
        if ips:
            cfg[flag] = ips[0] if len(ips) == 1 else ",".join(ips)

    server_opts: dict[str, Any] = {}
    sans = _csv(c.s("tls_san"))
    for s_ in sans:
        _san(c, s_, "tls-san")
    if sans:
        server_opts["tls-san"] = sans
    cidr = _cidrs(c, c.s("cluster_cidr"), "cluster-cidr")
    svc = _cidrs(c, c.s("service_cidr"), "service-cidr")
    _no_overlap(c, cidr, svc, "cluster-cidr", "service-cidr")
    if c.s("cluster_cidr"):
        server_opts["cluster-cidr"] = c.s("cluster_cidr").replace(" ", "")
    if c.s("service_cidr"):
        server_opts["service-cidr"] = c.s("service_cidr").replace(" ", "")
    dns = c.s("cluster_dns")
    if dns:
        ip = _ip(c, dns, "cluster-dns")
        nets = svc or [ipaddress.ip_network("10.43.0.0/16")]
        if ip is not None and not any(ip in n for n in nets if n.version == ip.version):
            c.err(f"cluster-dns {dns} must be inside service-cidr ({', '.join(map(str, nets))}).")
        server_opts["cluster-dns"] = dns
    if c.s("cluster_domain"):
        if not _DNS1123_SUB.match(c.s("cluster_domain")):
            c.err("cluster-domain must be a DNS domain like cluster.local.")
        server_opts["cluster-domain"] = c.s("cluster_domain")
    if distro == "k3s":
        fb = c.s("flannel_backend")
        if fb:
            if fb not in K3S_FLANNEL:
                c.err(f"flannel-backend must be one of {', '.join(x for x in K3S_FLANNEL if x)}.")
            server_opts["flannel-backend"] = fb
            if fb == "wireguard-native":
                c.warn("wireguard-native needs the WireGuard kernel module on every node.")
            if fb == "none":
                c.warn("flannel-backend none: install a CNI yourself (and consider disable-network-policy).")
        for opt in ("cni", "ingress_controller"):
            if c.s(opt):
                c.warn(f"{opt.replace('_', '-')} is an RKE2 option - ignored for k3s.")
        disabled = [x for x in K3S_DISABLE if c.b(f"k3s_disable_{x.replace('-', '_')}")]
        if any(c.b(f"rke2_disable_{x.replace('-', '_')}") for x in RKE2_DISABLE):
            c.warn("RKE2 disable options are set but distribution is k3s - ignored.")
    else:
        cni = c.s("cni")
        if cni:
            if cni not in RKE2_CNI:
                c.err(f"cni must be one of {', '.join(x for x in RKE2_CNI if x)}.")
            server_opts["cni"] = ["multus", cni] if c.b("multus") else cni
        elif c.b("multus"):
            server_opts["cni"] = ["multus", "canal"]
        ic = c.s("ingress_controller")
        if ic:
            server_opts["ingress-controller"] = ic
            if ic == "ingress-nginx":
                c.warn("RKE2 ships ingress-nginx only for migration; traefik is the current default.")
        if c.s("flannel_backend"):
            c.warn("flannel-backend is a k3s option - ignored for RKE2 (use cni).")
        disabled = [x for x in RKE2_DISABLE if c.b(f"rke2_disable_{x.replace('-', '_')}")]
        if any(c.b(f"k3s_disable_{x.replace('-', '_')}") for x in K3S_DISABLE):
            c.warn("k3s disable options are set but distribution is rke2 - ignored.")
    if disabled:
        server_opts["disable"] = disabled
        if "coredns" in disabled or "rke2-coredns" in disabled:
            c.warn("Disabling CoreDNS leaves the cluster without DNS unless you deploy your own.")
    wkm = c.s("write_kubeconfig_mode")
    if wkm:
        if not re.match(r"^0?[0-7]{3}$", wkm):
            c.err(f"write-kubeconfig-mode '{wkm}' must be an octal mode like 0600 or 0644.")
        server_opts["write-kubeconfig-mode"] = wkm if wkm.startswith("0") else "0" + wkm
        if wkm.endswith("644") or wkm.endswith("666"):
            c.warn("write-kubeconfig-mode makes the admin kubeconfig world-readable on the node.")
    if agent:
        ignored = sorted(server_opts)
        if ignored:
            c.err(f"Server-only options set for an agent: {', '.join(ignored)}. Clear them or switch role to server.")
    else:
        cfg.update(server_opts)
    labels = []
    for line in _lines(c.raw("node_label")):
        if "=" not in line:
            c.err(f"node-label '{line}' must be key=value.")
            continue
        k, v = line.split("=", 1)
        _check_label_key(c, k.strip(), "node-label")
        if len(v) > 63 or (v and not _LABEL_NAME.match(v.strip())):
            c.err(f"node-label value '{v}' is not a valid label value.")
        labels.append(f"{k.strip()}={v.strip()}")
    if labels:
        cfg["node-label"] = labels
    taints = _taints(c, c.raw("node_taint"), "node-taint")
    if taints:
        cfg["node-taint"] = [f"{t['key']}{'=' + t['value'] if 'value' in t else ''}:{t['effect']}" for t in taints]
    if not cfg:
        c.warn("Config is empty - the distro will run with all defaults.")
    path = CONFIG_PATHS[distro]
    return ConfigResult([cfg], c.errors, c.warnings, "config.yaml",
                        f"# {distro} {role} config - save as {path}\n", f"{distro} {role}: {path}")


_BUILDERS = {"kubeadm": _gen_kubeadm, "kind": _gen_kind, "kubeconfig": _gen_kubeconfig, "k3s / RKE2": _gen_rancher}

# ---- per-platform modules (clusterplatforms/*) -------------------------------------------------
from clusterplatforms import (minikube as _mk, k0s as _k0s, microk8s as _m8, talos as _talos,  # noqa: E402
                              kubespray as _ks, capi as _capi, openshift as _ocp, eks as _eks, gke as _gke,
                              aks as _aks)

PLATFORM_MODULES = [_mk, _k0s, _m8, _talos, _ks, _capi, _ocp, _eks, _gke, _aks]
_TABLE_OWNER: dict = {}
COLLAPSED_BY_TYPE: dict[str, set] = {}     # per-type collapsed sections (names like "Networking" repeat)
for _m in PLATFORM_MODULES:
    SCHEMA[_m.TYPE] = _m.SCHEMA
    _BUILDERS[_m.TYPE] = _m.build
    COLLAPSED_BY_TYPE[_m.TYPE] = set(getattr(_m, "COLLAPSED", ()))
    for _sec, _fields in _m.SCHEMA:
        for _f in _fields:
            if _f.kind == "table":
                _TABLE_OWNER[_f.key] = _m

# Dropdown order, grouped. GROUPS maps each section header to its types.
GROUPS = {
    "Local": ["kind", "minikube", "k3s / RKE2", "k0s", "MicroK8s"],
    "Self-managed": ["kubeadm", "Talos", "Kubespray", "Cluster API", "OpenShift"],
    "Managed cloud": ["EKS", "GKE", "AKS"],
    "Access": ["kubeconfig"],
}
CONFIG_TYPES[:] = [t for ts in GROUPS.values() for t in ts]
GROUP_OF = {t: g for g, ts in GROUPS.items() for t in ts}
assert set(CONFIG_TYPES) == set(SCHEMA), (set(CONFIG_TYPES) ^ set(SCHEMA))


def generate(ctype: str, values: dict | None = None) -> ConfigResult:
    if ctype not in SCHEMA:
        return ConfigResult(None, [f"Unknown config type '{ctype}'. Supported: {', '.join(CONFIG_TYPES)}"], [])
    merged = defaults(ctype)
    merged.update(values or {})
    c = _Ctx(merged)
    try:
        return _BUILDERS[ctype](c)
    except Exception as exc:  # never crash the GUI
        c.err(f"Internal error while building {ctype}: {exc!r}")
        return ConfigResult(None, c.errors, c.warnings)


def check_row(table_key: str, row: dict) -> list[str]:
    c = _Ctx({})
    sc = _SubCtx(c, {**row_defaults(table_key), **row}, "")
    if table_key == "nodes":
        if (sc.s("role") or "worker") not in KIND_ROLES:
            sc.err("role must be control-plane or worker.")
        sc.int("count", "count", lo=1, hi=20, required=True)
        _parse_kv(sc, sc.s("labels"), "labels", label_rules=True)
    elif table_key == "port_mappings":
        _port(sc, sc.s("container_port"), "containerPort", required=True)
        _port(sc, sc.s("host_port"), "hostPort", required=True)
        if sc.s("listen_address"):
            _ip(sc, sc.s("listen_address"), "listenAddress")
    elif table_key in _TABLE_OWNER and hasattr(_TABLE_OWNER[table_key], "check_row"):
        _TABLE_OWNER[table_key].check_row(table_key, sc)
    return c.errors


# Hover help for every field (see field_docs.py)
import field_docs as _field_docs  # noqa: E402
_field_docs.apply_cluster(SCHEMA)
