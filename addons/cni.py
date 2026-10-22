"""
addons.cni - CNI add-on generators: Calico (Tigera operator), Cilium (Helm), Flannel, Canal, Antrea (Helm)
and Multus (+ NetworkAttachmentDefinition builder). No GUI dependency.

Each build(c) returns a ConfigResult whose files are the config objects plus an install.sh with pinned
versions. pod_cidrs(values) returns the pod networks the add-on will use (for the cluster cross-check).
"""
from __future__ import annotations

import ipaddress
from typing import Any

from .common import (VERSIONS, TABLE_FIELDS, ConfigResult, Field, OutFile, _SubCtx, cidr, cidr_list, dns,
                     helm_install, kv, rows, script, table, to_json, to_yaml, version, yaml_file, _csv, _lines)

CALICO_ENCAP = ["VXLANCrossSubnet", "VXLAN", "IPIP", "IPIPCrossSubnet", "None"]
ENABLED_DISABLED = ["Enabled", "Disabled"]
OPT_ENABLED = ["", "Enabled", "Disabled"]
CALICO_PROVIDERS = ["", "Kind", "EKS", "GKE", "AKS", "OpenShift", "RKE2", "TKG", "DockerEnterprise"]
CALICO_DATAPLANES = ["", "Iptables", "Nftables", "BPF"]
CALICO_AUTODETECT = ["", "firstFound", "kubernetes", "interface", "skipInterface", "canReach", "cidrs"]
CILIUM_IPAM = ["cluster-pool", "kubernetes", "eni", "azure"]
CILIUM_METHODS = ["helm", "cilium CLI"]
CILIUM_KPR = ["false", "true"]
CILIUM_ROUTING = ["tunnel", "native"]
CILIUM_TUNNEL = ["vxlan", "geneve"]
CILIUM_ENCRYPTION = ["none", "wireguard", "ipsec"]
CILIUM_POLICY = ["default", "always", "never"]
FLANNEL_BACKENDS = ["vxlan", "host-gw", "wireguard", "udp", "ipip", "ipsec"]
CANAL_BACKENDS = ["vxlan", "host-gw"]
ANTREA_ENCAP = ["encap", "noEncap", "hybrid", "networkPolicyOnly"]
ANTREA_TUNNEL = ["geneve", "vxlan", "gre"]
ANTREA_ENCRYPTION = ["none", "ipsec", "wireguard"]
MULTUS_FLAVORS = ["thick", "thin"]
NAD_TYPES = ["macvlan", "ipvlan", "bridge", "host-device"]
MACVLAN_MODES = ["bridge", "private", "vepa", "passthru"]
IPVLAN_MODES = ["l2", "l3", "l3s"]
NAD_IPAM = ["host-local", "whereabouts", "static", "dhcp", "none"]

CALICO_BASE = "https://raw.githubusercontent.com/projectcalico/calico/{v}/manifests"

TABLE_FIELDS.update({
    "cal_pools": [
        Field("name", "Pool name", default="default-ipv4-ippool", required=True),
        Field("cidr", "CIDR", default="192.168.0.0/16", required=True, help="e.g. 10.244.0.0/16 or fd00:10:244::/56"),
        Field("encapsulation", "Encapsulation", "combo", default="VXLANCrossSubnet", options=CALICO_ENCAP),
        Field("nat_outgoing", "natOutgoing", "combo", default="Enabled", options=ENABLED_DISABLED),
        Field("block_size", "blockSize", default="26", help="IPv4 20-32 (default 26), IPv6 116-128 (default 122)"),
        Field("node_selector", "nodeSelector", default="all()", help="Calico selector, e.g. all() or zone == 'a'"),
    ],
})


# =========================================================================== Calico
CALICO_SCHEMA = [
    ("Version & install", [
        Field("cal_version", "Calico version", "ecombo", default=VERSIONS["calico"], options=[VERSIONS["calico"]],
              required=True),
        Field("cal_provider", "kubernetesProvider", "combo", default="", options=CALICO_PROVIDERS),
        Field("cal_wait", "install.sh waits until Calico is Available", "check", default=True),
    ]),
    ("IP pools", [table("cal_pools", "IP pools", ["name", "cidr", "encapsulation", "nat_outgoing", "block_size"],
                        "pod address ranges (Installation.spec.calicoNetwork.ipPools)",
                        default=[{"name": "default-ipv4-ippool", "cidr": "192.168.0.0/16",
                                  "encapsulation": "VXLANCrossSubnet", "nat_outgoing": "Enabled",
                                  "block_size": "26", "node_selector": "all()"}])]),
    ("Networking", [
        Field("cal_bgp", "BGP", "combo", default="", options=OPT_ENABLED),
        Field("cal_mtu", "MTU", default="", help="blank = auto-detect"),
        Field("cal_dataplane", "linuxDataplane", "combo", default="", options=CALICO_DATAPLANES),
        Field("cal_host_ports", "hostPorts", "combo", default="", options=OPT_ENABLED),
        Field("cal_autodetect", "IPv4 address autodetection", "combo", default="", options=CALICO_AUTODETECT),
        Field("cal_autodetect_value", "Autodetection value", default="",
              help="interface / skipInterface: regex (eth.*); canReach: IP or host; cidrs: comma-separated"),
    ]),
    ("eBPF dataplane", [
        Field("cal_kube_proxy_mgmt", "kubeProxyManagement", "combo", default="", options=OPT_ENABLED),
        Field("cal_bpf_bootstrap", "bpfNetworkBootstrap", "combo", default="", options=OPT_ENABLED),
        Field("cal_api_host", "API server host (KUBERNETES_SERVICE_HOST)", default="",
              help="real address of the API server (not the ClusterIP), e.g. 172.18.0.2 or api.example.com"),
        Field("cal_api_port", "API server port", default="6443"),
    ]),
    ("Components", [
        Field("cal_apiserver", "Calico API server (APIServer CR)", "check", default=True),
        Field("cal_goldmane", "Goldmane flow aggregator", "check", default=False),
        Field("cal_whisker", "Whisker observability UI", "check", default=False),
    ]),
]


def calico_pod_cidrs(c) -> list:
    out = []
    for r in rows(c, "cal_pools"):
        try:
            out.append(ipaddress.ip_network(r["cidr"].strip(), strict=True))
        except ValueError:
            pass
    return out


def build_calico(c) -> ConfigResult:
    v = version(c, "cal_version", "Calico version", v_prefix=True)
    bgp = c.s("cal_bgp")
    pools, names, nets = [], set(), []
    prs = rows(c, "cal_pools")
    if not prs:
        c.err("Add at least one IP pool.")
    for i, r in enumerate(prs, 1):
        sc = _SubCtx(c, r, f"IP pool #{i}: ")
        name = sc.s("name")
        dns(sc, name, "pool name")
        if name in names:
            sc.err(f"duplicate pool name '{name}'.")
        names.add(name)
        net = cidr(sc, sc.s("cidr"), "CIDR")
        enc = sc.s("encapsulation") or "VXLANCrossSubnet"
        if enc not in CALICO_ENCAP:
            sc.err(f"encapsulation must be one of {', '.join(CALICO_ENCAP)}.")
        bs = sc.int("block_size", "blockSize")
        pool: dict[str, Any] = {"name": name}
        if net:
            for other in nets:
                if other.version == net.version and other.overlaps(net):
                    sc.err(f"{net} overlaps another pool ({other}).")
            nets.append(net)
            lo, hi = (20, 32) if net.version == 4 else (116, 128)
            if bs is not None and not lo <= bs <= hi:
                sc.err(f"blockSize {bs} must be {lo}-{hi} for IPv{net.version}.")
            elif bs is not None and bs < net.prefixlen:
                sc.err(f"blockSize /{bs} is larger than the pool {net}.")
            if net.version == 6 and enc.startswith("IPIP"):
                sc.err("IPIP encapsulation is IPv4-only; use VXLAN or None for IPv6 pools.")
            if net.version == 4 and net.prefixlen > 24:
                sc.warn(f"{net} is small: each node takes at least one /{bs or 26} block.")
        if bs is not None:
            pool["blockSize"] = bs
        pool["cidr"] = sc.s("cidr")
        pool["encapsulation"] = enc
        nat = sc.s("nat_outgoing") or "Enabled"
        if nat not in ENABLED_DISABLED:
            sc.err("natOutgoing must be Enabled or Disabled.")
        pool["natOutgoing"] = nat
        ns_ = sc.s("node_selector") or "all()"
        pool["nodeSelector"] = ns_
        if enc.startswith("IPIP") and bgp == "Disabled":
            sc.err("IPIP encapsulation needs BGP (Calico routes IPIP over BGP). Enable BGP or use VXLAN.")
        if enc == "None" and bgp == "Disabled":
            sc.err("Encapsulation None with BGP Disabled leaves pods unroutable between nodes.")
        if enc == "None":
            sc.warn("Encapsulation None needs a network that routes pod CIDRs (BGP peering or one L2 segment).")
        pools.append(pool)
    net_spec: dict[str, Any] = {}
    if bgp:
        net_spec["bgp"] = bgp
    if pools:
        net_spec["ipPools"] = pools
    mtu = c.int("cal_mtu", "MTU", lo=576, hi=9000)
    if mtu:
        net_spec["mtu"] = mtu
    dp = c.s("cal_dataplane")
    if dp:
        net_spec["linuxDataplane"] = dp
    if c.s("cal_host_ports"):
        net_spec["hostPorts"] = c.s("cal_host_ports")
    ad, adv = c.s("cal_autodetect"), c.s("cal_autodetect_value")
    if ad:
        if ad == "firstFound":
            det: Any = {"firstFound": True}
        elif ad == "kubernetes":
            det = {"kubernetes": "NodeInternalIP"}
        elif ad == "cidrs":
            nl = cidr_list(c, adv, "autodetection CIDR")
            if not nl:
                c.err("Autodetection 'cidrs' needs at least one CIDR in the value field.")
            det = {"cidrs": [str(n) for n in nl]}
        else:
            if not adv:
                c.err(f"Autodetection '{ad}' needs a value (interface regex, or an address for canReach).")
            det = {ad: adv}
        net_spec["nodeAddressAutodetectionV4"] = det
    elif adv:
        c.warn("Autodetection value is ignored without an autodetection method.")
    kpm, boot = c.s("cal_kube_proxy_mgmt"), c.s("cal_bpf_bootstrap")
    if kpm:
        net_spec["kubeProxyManagement"] = kpm
    if boot:
        net_spec["bpfNetworkBootstrap"] = boot
    host, port = c.s("cal_api_host"), c.s("cal_api_port") or "6443"
    if dp == "BPF":
        if not host and boot != "Enabled":
            c.warn("eBPF needs the API server address: set the API server host (kubernetes-services-endpoint "
                   "ConfigMap) or bpfNetworkBootstrap Enabled.")
        if kpm != "Enabled":
            c.warn("With eBPF, kube-proxy must be disabled: set kubeProxyManagement Enabled (operator patches "
                   "kube-proxy) or disable kube-proxy yourself.")
        if any(p["encapsulation"].startswith("IPIP") for p in pools):
            c.warn("eBPF supports IPIP but VXLAN is the tested default; consider VXLAN.")
    elif kpm or boot:
        c.warn("kubeProxyManagement / bpfNetworkBootstrap only matter with linuxDataplane BPF.")
    if host and not port.isdigit():
        c.err("API server port must be a number.")
    spec: dict[str, Any] = {}
    if c.s("cal_provider"):
        spec["kubernetesProvider"] = c.s("cal_provider")
    spec["calicoNetwork"] = net_spec
    docs: list = [{"apiVersion": "operator.tigera.io/v1", "kind": "Installation", "metadata": {"name": "default"},
                   "spec": spec}]
    if c.b("cal_apiserver"):
        docs.append({"apiVersion": "operator.tigera.io/v1", "kind": "APIServer", "metadata": {"name": "default"},
                     "spec": {}})
    for key, kind in (("cal_goldmane", "Goldmane"), ("cal_whisker", "Whisker")):
        if c.b(key):
            docs.append({"apiVersion": "operator.tigera.io/v1", "kind": kind, "metadata": {"name": "default"}})
    if c.b("cal_whisker") and not c.b("cal_goldmane"):
        c.warn("Whisker shows flows from Goldmane; enable Goldmane too.")
    files = [yaml_file("custom-resources.yaml", docs, header=f"# Calico {v} operator custom resources\n")]
    if host:
        files.append(yaml_file("kubernetes-services-endpoint.yaml", [{
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "kubernetes-services-endpoint", "namespace": "tigera-operator"},
            "data": {"KUBERNETES_SERVICE_HOST": host, "KUBERNETES_SERVICE_PORT": port}}]))
    base = CALICO_BASE.format(v=v)
    body = [f'VERSION="{v}"', f'BASE="{CALICO_BASE.format(v="$VERSION")}"', "",
            "# 1. operator CRDs + Tigera operator (server-side apply: the CRDs are too big for client-side apply)",
            '"$KUBECTL" apply --server-side --force-conflicts -f "$BASE/operator-crds.yaml"',
            '"$KUBECTL" apply --server-side --force-conflicts -f "$BASE/tigera-operator.yaml"',
            '"$KUBECTL" wait --for=condition=Established crd/installations.operator.tigera.io --timeout=120s',
            '"$KUBECTL" -n tigera-operator rollout status deployment/tigera-operator --timeout=5m']
    if host:
        body += ["", "# 2. API server endpoint for the eBPF dataplane",
                 '"$KUBECTL" apply -f kubernetes-services-endpoint.yaml']
    body += ["", "# 3. Installation / APIServer custom resources", '"$KUBECTL" apply -f custom-resources.yaml']
    if c.b("cal_wait"):
        body += ["", "# 4. wait until the operator reports Calico Available",
                 "wait_exists tigerastatus/calico",
                 '"$KUBECTL" wait --for=condition=Available tigerastatus/calico --timeout=15m',
                 '"$KUBECTL" get tigerastatus']
    files.append(script(f"Install Calico {v} with the Tigera operator", [
        f"manifests: {base}/operator-crds.yaml, tigera-operator.yaml",
        "kind: create the cluster with networking.disableDefaultCNI: true (and podSubnet = the pool CIDR)",
        "k3s: --flannel-backend=none --disable-network-policy; RKE2: cni: none"], body, wait=c.b("cal_wait")))
    return ConfigResult(None, c.errors, c.warnings, files=files, info=f"Calico {v} (Tigera operator)")


# =========================================================================== Cilium
CILIUM_SCHEMA = [
    ("Version & install", [
        Field("cil_version", "Cilium version", "ecombo", default=VERSIONS["cilium"], options=[VERSIONS["cilium"]],
              required=True),
        Field("cil_method", "Install with", "combo", default="helm", options=CILIUM_METHODS),
        Field("cil_namespace", "Namespace", default="kube-system"),
    ]),
    ("IPAM", [
        Field("cil_ipam", "ipam.mode", "combo", default="cluster-pool", options=CILIUM_IPAM),
        Field("cil_pool_cidrs", "clusterPoolIPv4PodCIDRList", default="10.0.0.0/8", help="comma-separated CIDRs"),
        Field("cil_pool_mask", "clusterPoolIPv4MaskSize", default="24"),
    ]),
    ("Datapath", [
        Field("cil_kpr", "kubeProxyReplacement", "combo", default="false", options=CILIUM_KPR),
        Field("cil_api_host", "k8sServiceHost", "ecombo", default="", options=["", "auto"],
              help="API server address reachable without kube-proxy; 'auto' reads it from cluster-info"),
        Field("cil_api_port", "k8sServicePort", default="", help="e.g. 6443"),
        Field("cil_routing", "routingMode", "combo", default="tunnel", options=CILIUM_ROUTING),
        Field("cil_tunnel", "tunnelProtocol", "combo", default="vxlan", options=CILIUM_TUNNEL),
        Field("cil_native_cidr", "ipv4NativeRoutingCIDR", default="", help="native routing: the pod CIDR, e.g. 10.0.0.0/8"),
        Field("cil_auto_routes", "autoDirectNodeRoutes", "check", default=False),
        Field("cil_bpf_masq", "bpf.masquerade", "check", default=False),
        Field("cil_mtu", "MTU", default="", help="blank = auto"),
    ]),
    ("Hubble", [
        Field("cil_hubble", "hubble.enabled", "check", default=True),
        Field("cil_relay", "hubble.relay.enabled", "check", default=False),
        Field("cil_ui", "hubble.ui.enabled", "check", default=False),
    ]),
    ("Security", [
        Field("cil_encryption", "Transparent encryption", "combo", default="none", options=CILIUM_ENCRYPTION),
        Field("cil_node_encryption", "encryption.nodeEncryption", "check", default=False),
        Field("cil_policy_mode", "policyEnforcementMode", "combo", default="default", options=CILIUM_POLICY),
    ]),
    ("Features", [
        Field("cil_bgp", "bgpControlPlane.enabled", "check", default=False),
        Field("cil_gateway", "gatewayAPI.enabled", "check", default=False),
        Field("cil_l2", "l2announcements.enabled", "check", default=False),
        Field("cil_operator_replicas", "operator.replicas", default="", help="blank = 2; use 1 on single-node clusters"),
    ]),
]


def cilium_pod_cidrs(c) -> list:
    if (c.s("cil_ipam") or "cluster-pool") != "cluster-pool":
        return []
    out = []
    for p in _csv(c.s("cil_pool_cidrs")):
        try:
            out.append(ipaddress.ip_network(p, strict=True))
        except ValueError:
            pass
    return out


def build_cilium(c) -> ConfigResult:
    v = version(c, "cil_version", "Cilium version", v_prefix=False)
    ns = c.s("cil_namespace") or "kube-system"
    dns(c, ns, "Namespace", "label")
    vals: dict[str, Any] = {}
    ipam = c.s("cil_ipam") or "cluster-pool"
    if ipam not in CILIUM_IPAM:
        c.err(f"ipam.mode must be one of {', '.join(CILIUM_IPAM)}.")
    vals["ipam"] = {"mode": ipam}
    if ipam == "cluster-pool":
        nets = cidr_list(c, c.s("cil_pool_cidrs"), "clusterPoolIPv4PodCIDRList")
        if not nets:
            c.err("clusterPoolIPv4PodCIDRList needs at least one CIDR.")
        mask = c.int("cil_pool_mask", "clusterPoolIPv4MaskSize", lo=16, hi=30)
        op: dict[str, Any] = {"clusterPoolIPv4PodCIDRList": [str(n) for n in nets if n.version == 4]}
        if any(n.version == 6 for n in nets):
            c.err("clusterPoolIPv4PodCIDRList takes IPv4 CIDRs only.")
        if mask:
            op["clusterPoolIPv4MaskSize"] = mask
            for n in nets:
                if mask < n.prefixlen:
                    c.err(f"mask size /{mask} is larger than the pool {n}.")
        vals["ipam"]["operator"] = op
    elif c.s("cil_pool_cidrs") not in ("", "10.0.0.0/8"):
        c.warn(f"clusterPoolIPv4PodCIDRList is only used with ipam.mode cluster-pool (ignored for {ipam}).")
    kpr = c.s("cil_kpr") or "false"
    vals["kubeProxyReplacement"] = kpr == "true"
    host, port = c.s("cil_api_host"), c.s("cil_api_port")
    if host:
        vals["k8sServiceHost"] = host
    if port:
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            c.err("k8sServicePort must be 1-65535.")
        else:
            vals["k8sServicePort"] = int(port)
    if kpr == "true" and not host:
        c.err("kubeProxyReplacement=true needs k8sServiceHost (the real API server address, or 'auto'): "
              "without kube-proxy the ClusterIP of the kubernetes Service isn't reachable.")
    if host and host != "auto" and not port:
        c.err("Set k8sServicePort together with k8sServiceHost (e.g. 6443).")
    routing = c.s("cil_routing") or "tunnel"
    vals["routingMode"] = routing
    if routing == "tunnel":
        vals["tunnelProtocol"] = c.s("cil_tunnel") or "vxlan"
        if c.s("cil_native_cidr"):
            c.warn("ipv4NativeRoutingCIDR is only used with routingMode native.")
    else:
        nc = c.s("cil_native_cidr")
        if nc:
            cidr(c, nc, "ipv4NativeRoutingCIDR")
            vals["ipv4NativeRoutingCIDR"] = nc
        elif ipam not in ("eni", "azure"):
            c.err("routingMode native needs ipv4NativeRoutingCIDR (the CIDR that is routable without "
                  "masquerading, usually the pod CIDR).")
        if c.b("cil_auto_routes"):
            vals["autoDirectNodeRoutes"] = True
    if c.b("cil_auto_routes") and routing == "tunnel":
        c.warn("autoDirectNodeRoutes only applies to native routing (nodes on one L2 segment).")
    if ipam == "eni":
        vals["eni"] = {"enabled": True}
        vals["routingMode"] = "native"
        vals.pop("tunnelProtocol", None)
        vals["egressMasqueradeInterfaces"] = "eth+"
        c.warn("ENI IPAM: EKS only. Remove the aws-node (VPC CNI) DaemonSet first and give the operator IAM "
               "permissions for ENIs (see Cilium's EKS guide).")
    if ipam == "azure":
        vals["azure"] = {"enabled": True}
        vals["routingMode"] = "native"
        vals.pop("tunnelProtocol", None)
        c.warn("Azure IPAM also needs azure.resourceGroup / subscriptionID / credentials in values.yaml; on AKS "
               "prefer --network-plugin none with cluster-pool, or AKS's built-in Cilium dataplane.")
    if c.b("cil_bpf_masq"):
        vals["bpf"] = {"masquerade": True}
        if kpr != "true":
            c.err("bpf.masquerade needs kubeProxyReplacement=true.")
    mtu = c.int("cil_mtu", "MTU", lo=1280, hi=9000)
    if mtu:
        vals["MTU"] = mtu
    hub: dict[str, Any] = {"enabled": c.b("cil_hubble")}
    if c.b("cil_relay") or c.b("cil_ui"):
        if not c.b("cil_hubble"):
            c.err("Hubble relay / UI need hubble.enabled.")
        hub["relay"] = {"enabled": c.b("cil_relay") or c.b("cil_ui")}
        if c.b("cil_ui"):
            hub["ui"] = {"enabled": True}
            if not c.b("cil_relay"):
                c.warn("Hubble UI needs Hubble Relay; relay was enabled too.")
    vals["hubble"] = hub
    enc = c.s("cil_encryption") or "none"
    if enc != "none":
        vals["encryption"] = {"enabled": True, "type": enc}
        if c.b("cil_node_encryption"):
            if enc != "wireguard":
                c.err("encryption.nodeEncryption is only supported with WireGuard.")
            vals["encryption"]["nodeEncryption"] = True
        if enc == "wireguard":
            c.warn("WireGuard needs kernel >= 5.6 (or the wireguard module) on every node.")
    elif c.b("cil_node_encryption"):
        c.err("encryption.nodeEncryption needs WireGuard encryption.")
    pm = c.s("cil_policy_mode") or "default"
    if pm != "default":
        vals["policyEnforcementMode"] = pm
        if pm == "always":
            c.warn("policyEnforcementMode always denies all traffic without an allowing policy (even DNS).")
    if c.b("cil_bgp"):
        vals["bgpControlPlane"] = {"enabled": True}
    if c.b("cil_gateway"):
        vals["gatewayAPI"] = {"enabled": True}
        if kpr != "true":
            c.err("gatewayAPI needs kubeProxyReplacement=true.")
    if c.b("cil_l2"):
        vals["l2announcements"] = {"enabled": True}
        if kpr != "true":
            c.err("l2announcements needs kubeProxyReplacement=true.")
    reps = c.int("cil_operator_replicas", "operator.replicas", lo=1, hi=10)
    if reps:
        vals["operator"] = {"replicas": reps}
    files = [OutFile("values.yaml", f"# Cilium {v} Helm values (chart cilium/cilium)\n" + to_yaml(vals))]
    method = c.s("cil_method") or "helm"
    body = [f'VERSION="{v}"']
    if c.b("cil_gateway"):
        gv = VERSIONS["gateway-api"]
        body += ["", f"# Gateway API CRDs ({gv}, needed before Cilium starts with gatewayAPI.enabled)",
                 f'"$KUBECTL" apply --server-side -f https://github.com/kubernetes-sigs/gateway-api/releases/download/{gv}/standard-install.yaml']
    if enc == "ipsec":
        body += ["", "# IPsec key (generated on your machine, never written to disk)",
                 f'if ! "$KUBECTL" -n {ns} get secret cilium-ipsec-keys >/dev/null 2>&1; then',
                 f'  "$KUBECTL" -n {ns} create secret generic cilium-ipsec-keys \\',
                 '    --from-literal=keys="3+ rfc4106(gcm(aes)) $(dd if=/dev/urandom count=20 bs=1 2>/dev/null | '
                 "od -An -tx1 | tr -d ' \\n') 128\"",
                 "fi"]
    if method == "helm":
        body += [""] + helm_install("cilium", "cilium", "cilium", "$VERSION", ns, "values.yaml")
    else:
        body += ["", "# needs the cilium CLI (https://github.com/cilium/cilium-cli, tested with "
                     f"{VERSIONS['cilium-cli']})",
                 f'cilium install --version "$VERSION" --namespace {ns} --values values.yaml']
    body += ["", f'"$KUBECTL" -n {ns} rollout status daemonset/cilium --timeout=10m',
             f'"$KUBECTL" -n {ns} rollout status deployment/cilium-operator --timeout=10m',
             f'if command -v cilium >/dev/null; then cilium status --wait --namespace {ns}; fi']
    files.append(script(f"Install Cilium {v} ({method})", [
        "kind: networking.disableDefaultCNI: true (kubeProxyMode: none with kubeProxyReplacement=true)",
        "k3s: --flannel-backend=none --disable-network-policy (--disable-kube-proxy for KPR); RKE2: cni: none"],
        body, helm=method == "helm"))
    return ConfigResult(None, c.errors, c.warnings, files=files, info=f"Cilium {v} ({method})")


# =========================================================================== Flannel
FLANNEL_SCHEMA = [
    ("Version & install", [
        Field("fl_version", "Flannel version", "ecombo", default=VERSIONS["flannel"], options=[VERSIONS["flannel"]],
              required=True),
        Field("fl_iface", "--iface", default="", help="interface or IP for inter-node traffic; blank = default route"),
    ]),
    ("net-conf.json", [
        Field("fl_network", "Network", default="10.244.0.0/16", required=True),
        Field("fl_subnet_len", "SubnetLen", default="", help="per-node subnet prefix; blank = 24"),
        Field("fl_ipv6", "EnableIPv6", "check", default=False),
        Field("fl_ipv6_network", "IPv6Network", default="", help="e.g. fd00:10:244::/56"),
        Field("fl_nftables", "EnableNFTables", "check", default=False),
    ]),
    ("Backend", [
        Field("fl_backend", "Backend Type", "combo", default="vxlan", options=FLANNEL_BACKENDS),
        Field("fl_vni", "VNI (vxlan)", default="", help="blank = 1"),
        Field("fl_port", "Port (vxlan)", default="", help="UDP port; blank = kernel default 8472"),
        Field("fl_direct_routing", "DirectRouting (vxlan / ipip)", "check", default=False),
        Field("fl_wg_keepalive", "PersistentKeepaliveInterval (wireguard)", default="", help="seconds; blank = off"),
    ]),
]


def flannel_netconf(c) -> dict:
    net: dict[str, Any] = {}
    n4 = cidr(c, c.s("fl_network"), "Network")
    net["Network"] = c.s("fl_network")
    sl = c.int("fl_subnet_len", "SubnetLen", lo=8, hi=30)
    if sl:
        if n4 and sl <= n4.prefixlen:
            c.err(f"SubnetLen /{sl} must be longer than the Network prefix /{n4.prefixlen}.")
        net["SubnetLen"] = sl
    if c.b("fl_ipv6"):
        n6 = cidr(c, c.s("fl_ipv6_network"), "IPv6Network")
        if n6 and n6.version != 6:
            c.err("IPv6Network must be an IPv6 CIDR.")
        net["EnableIPv6"] = True
        net["IPv6Network"] = c.s("fl_ipv6_network")
    elif c.s("fl_ipv6_network"):
        c.warn("IPv6Network is ignored unless EnableIPv6 is ticked.")
    if n4 and n4.version != 4:
        c.err("Network must be an IPv4 CIDR (use IPv6Network for IPv6).")
    if c.b("fl_nftables"):
        net["EnableNFTables"] = True
    be: dict[str, Any] = {"Type": c.s("fl_backend") or "vxlan"}
    t = be["Type"]
    if t not in FLANNEL_BACKENDS:
        c.err(f"Backend Type must be one of {', '.join(FLANNEL_BACKENDS)}.")
    vni = c.int("fl_vni", "VNI", lo=1, hi=16777215)
    port = c.int("fl_port", "Port", lo=1, hi=65535)
    if t == "vxlan":
        if vni:
            be["VNI"] = vni
        if port:
            be["Port"] = port
    elif vni or port:
        c.warn("VNI / Port only apply to the vxlan backend.")
    if c.b("fl_direct_routing"):
        if t in ("vxlan", "ipip"):
            be["DirectRouting"] = True
        else:
            c.warn("DirectRouting only applies to vxlan / ipip.")
    ka = c.int("fl_wg_keepalive", "PersistentKeepaliveInterval", lo=0, hi=3600)
    if t == "wireguard" and ka:
        be["PersistentKeepaliveInterval"] = ka
    if t == "udp":
        c.warn("The udp backend is for debugging / very old kernels only (slow); prefer vxlan.")
    if t in ("ipip", "ipsec"):
        c.warn(f"The {t} backend is experimental in Flannel.")
    if t == "host-gw":
        c.warn("host-gw needs every node on one L2 network (direct routes to other nodes).")
    net["Backend"] = be
    return net


def flannel_pod_cidrs(c) -> list:
    try:
        return [ipaddress.ip_network(c.s("fl_network"), strict=True)]
    except ValueError:
        return []


FLANNEL_CNI_CONF = {"name": "cbr0", "cniVersion": "1.0.0", "plugins": [
    {"type": "flannel", "delegate": {"hairpinMode": True, "isDefaultGateway": True}},
    {"type": "portmap", "capabilities": {"portMappings": True}}]}


def build_flannel(c) -> ConfigResult:
    v = version(c, "fl_version", "Flannel version", v_prefix=True)
    netconf = flannel_netconf(c)
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "kube-flannel-cfg", "namespace": "kube-flannel",
                       "labels": {"app": "flannel", "k8s-app": "flannel", "tier": "node"}},
          "data": {"cni-conf.json": to_json(FLANNEL_CNI_CONF) + "\n", "net-conf.json": to_json(netconf) + "\n"}}
    files = [yaml_file("kube-flannel-cfg.yaml", [cm], header=f"# Flannel {v} configuration (overrides the upstream ConfigMap)\n")]
    body = [f'VERSION="{v}"',
            '"$KUBECTL" apply -f "https://github.com/flannel-io/flannel/releases/download/$VERSION/kube-flannel.yml"',
            '"$KUBECTL" apply -f kube-flannel-cfg.yaml']
    iface = c.s("fl_iface")
    if iface:
        if " " in iface:
            c.err("--iface must be one interface name or IP.")
        body += ['"$KUBECTL" -n kube-flannel patch daemonset kube-flannel-ds --type=json -p '
                 f"'[{{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\",\"value\":\"--iface={iface}\"}}]'"]
    body += ["# restart so every pod reads the new net-conf.json",
             '"$KUBECTL" -n kube-flannel rollout restart daemonset/kube-flannel-ds',
             '"$KUBECTL" -n kube-flannel rollout status daemonset/kube-flannel-ds --timeout=5m']
    files.append(script(f"Install Flannel {v}", [
        "The cluster's pod CIDR (kubeadm podSubnet / --cluster-cidr) must equal Network; flannel uses the",
        "per-node podCIDR the controller-manager allocates (--kube-subnet-mgr).",
        "kind: networking.disableDefaultCNI: true"], body))
    return ConfigResult(None, c.errors, c.warnings, files=files, info=f"Flannel {v} ({netconf['Backend']['Type']})")


# =========================================================================== Canal
CANAL_SCHEMA = [
    ("Version & install", [
        Field("cn_version", "Calico (canal.yaml) version", "ecombo", default=VERSIONS["canal"],
              options=[VERSIONS["canal"]], required=True),
    ]),
    ("canal-config", [
        Field("cn_network", "Pod network (flannel Network)", default="10.244.0.0/16", required=True),
        Field("cn_backend", "Flannel backend", "combo", default="vxlan", options=CANAL_BACKENDS),
        Field("cn_iface", "canal_iface", default="", help="blank = interface of the default route"),
        Field("cn_masquerade", "masquerade", "check", default=True),
        Field("cn_mtu", "veth_mtu", default="0", help="0 = auto-detect"),
    ]),
]

CANAL_CNI_CONF = {"name": "k8s-pod-network", "cniVersion": "1.0.0", "plugins": [
    {"type": "calico", "log_level": "info", "log_file_path": "/var/log/calico/cni/cni.log",
     "datastore_type": "kubernetes", "nodename": "__KUBERNETES_NODE_NAME__", "mtu": "__CNI_MTU__",
     "ipam": {"type": "host-local", "subnet": "usePodCidr"}, "policy": {"type": "k8s"},
     "kubernetes": {"kubeconfig": "__KUBECONFIG_FILEPATH__"}},
    {"type": "portmap", "snat": True, "capabilities": {"portMappings": True}}]}


def canal_pod_cidrs(c) -> list:
    try:
        return [ipaddress.ip_network(c.s("cn_network"), strict=True)]
    except ValueError:
        return []


def build_canal(c) -> ConfigResult:
    v = version(c, "cn_version", "Calico version", v_prefix=True)
    n = cidr(c, c.s("cn_network"), "Pod network")
    if n and n.version != 4:
        c.err("Canal's flannel network must be IPv4.")
    be = c.s("cn_backend") or "vxlan"
    mtu = c.int("cn_mtu", "veth_mtu", lo=0, hi=9000)
    if mtu and mtu < 576:
        c.err("veth_mtu must be 0 (auto) or >= 576.")
    # the "mtu": __CNI_MTU__ placeholder is a bare (unquoted) token in upstream's template
    cni = to_json(CANAL_CNI_CONF).replace('"__CNI_MTU__"', "__CNI_MTU__")
    data = {"typha_service_name": "none", "canal_iface": c.s("cn_iface"),
            "masquerade": "true" if c.b("cn_masquerade") else "false", "veth_mtu": str(mtu or 0),
            "cni_network_config": cni,
            "net-conf.json": to_json({"Network": c.s("cn_network"), "Backend": {"Type": be}}) + "\n"}
    if be == "host-gw":
        c.warn("host-gw needs every node on one L2 network.")
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "canal-config", "namespace": "kube-system"},
          "data": data}
    files = [yaml_file("canal-config.yaml", [cm], header=f"# Canal (Calico {v} policy + flannel networking) configuration\n")]
    body = [f'VERSION="{v}"',
            '"$KUBECTL" apply --server-side --force-conflicts -f '
            '"https://raw.githubusercontent.com/projectcalico/calico/$VERSION/manifests/canal.yaml"',
            '"$KUBECTL" apply --server-side --force-conflicts -f canal-config.yaml',
            '"$KUBECTL" -n kube-system rollout restart daemonset/canal',
            '"$KUBECTL" -n kube-system rollout status daemonset/canal --timeout=10m']
    files.append(script(f"Install Canal (Calico {v} + flannel)", [
        "The cluster pod CIDR (podSubnet / --cluster-cidr) must equal the flannel Network.",
        "kind: networking.disableDefaultCNI: true"], body))
    return ConfigResult(None, c.errors, c.warnings, files=files, info=f"Canal {v}")


# =========================================================================== Antrea
ANTREA_SCHEMA = [
    ("Version & install", [
        Field("an_version", "Antrea chart version", "ecombo", default=VERSIONS["antrea"],
              options=[VERSIONS["antrea"]], required=True),
    ]),
    ("antrea-config (agent)", [
        Field("an_encap", "trafficEncapMode", "combo", default="encap", options=ANTREA_ENCAP),
        Field("an_tunnel", "tunnelType", "combo", default="geneve", options=ANTREA_TUNNEL),
        Field("an_encryption", "trafficEncryptionMode", "combo", default="none", options=ANTREA_ENCRYPTION),
        Field("an_wg_port", "wireGuard.port", default="51820"),
        Field("an_mtu", "defaultMTU", default="", help="blank = auto"),
        Field("an_no_snat", "noSNAT", "check", default=False),
        Field("an_transport_iface", "transportInterface", default="", help="blank = interface of the node IP"),
    ]),
    ("Proxy & IPAM", [
        Field("an_proxy_all", "antreaProxy.proxyAll (replace kube-proxy)", "check", default=False),
        Field("an_api_override", "kubeAPIServerOverride", default="", help="host:port of the API server, e.g. 172.18.0.2:6443"),
        Field("an_node_ipam", "nodeIPAM.enable", "check", default=False),
        Field("an_cluster_cidrs", "nodeIPAM.clusterCIDRs", default="", help="comma-separated pod CIDRs"),
        Field("an_feature_gates", "featureGates", "text", default="", height=2, help="Name=true|false per line"),
    ]),
]


def antrea_pod_cidrs(c) -> list:
    if not c.b("an_node_ipam"):
        return []
    out = []
    for p in _csv(c.s("an_cluster_cidrs")):
        try:
            out.append(ipaddress.ip_network(p, strict=True))
        except ValueError:
            pass
    return out


def build_antrea(c) -> ConfigResult:
    v = version(c, "an_version", "Antrea chart version", v_prefix=False)
    vals: dict[str, Any] = {}
    enc_mode = c.s("an_encap") or "encap"
    vals["trafficEncapMode"] = enc_mode
    tun = c.s("an_tunnel") or "geneve"
    if enc_mode in ("encap", "hybrid"):
        vals["tunnelType"] = tun
    elif tun != "geneve":
        c.warn("tunnelType is only used with trafficEncapMode encap / hybrid.")
    crypt = c.s("an_encryption") or "none"
    vals["trafficEncryptionMode"] = crypt
    if crypt == "ipsec" and enc_mode not in ("encap",):
        c.err("IPsec encryption needs trafficEncapMode encap.")
    if crypt == "wireguard":
        p = c.int("an_wg_port", "wireGuard.port", lo=1, hi=65535)
        if p:
            vals["wireGuard"] = {"port": p}
    if enc_mode == "networkPolicyOnly":
        c.warn("networkPolicyOnly chains Antrea after the platform CNI (EKS VPC CNI, AKS Azure CNI, GKE); "
               "Antrea then only enforces NetworkPolicies.")
    mtu = c.int("an_mtu", "defaultMTU", lo=1280, hi=9000)
    if mtu:
        vals["defaultMTU"] = mtu
    if c.b("an_no_snat"):
        vals["noSNAT"] = True
    if c.s("an_transport_iface"):
        vals["transportInterface"] = c.s("an_transport_iface")
    api = c.s("an_api_override")
    if c.b("an_proxy_all"):
        vals["antreaProxy"] = {"proxyAll": True}
        if not api:
            c.err("antreaProxy.proxyAll (no kube-proxy) needs kubeAPIServerOverride (host:port of the API server).")
    if api:
        vals["kubeAPIServerOverride"] = api
    if c.b("an_node_ipam"):
        nets = cidr_list(c, c.s("an_cluster_cidrs"), "nodeIPAM.clusterCIDRs")
        if not nets:
            c.err("nodeIPAM.enable needs at least one cluster CIDR.")
        vals["nodeIPAM"] = {"enable": True, "clusterCIDRs": [str(n) for n in nets]}
        c.warn("Antrea NodeIPAM replaces kube-controller-manager's --allocate-node-cidrs; turn that off.")
    gates = {}
    for line in _lines(c.raw("an_feature_gates")):
        if "=" not in line or line.split("=", 1)[1].strip().lower() not in ("true", "false"):
            c.err(f"featureGates '{line}' must be Name=true|false.")
            continue
        k, val = line.split("=", 1)
        gates[k.strip()] = val.strip().lower() == "true"
    if gates:
        vals["featureGates"] = gates
    files = [OutFile("values.yaml", f"# Antrea {v} Helm values (rendered into the antrea-config ConfigMap)\n"
                     + to_yaml(vals))]
    body = [f'VERSION="{v}"']
    extra = []
    if crypt == "ipsec":
        body += ['# IPsec pre-shared key generated at install time (not stored in values.yaml)',
                 'PSK="$(od -An -N16 -tx1 /dev/urandom | tr -d \' \\n\')"']
        extra = ['--set ipsec.psk="$PSK"']
    body += helm_install("antrea", "antrea", "antrea", "$VERSION", "kube-system", "values.yaml", extra)
    body += ['"$KUBECTL" -n kube-system rollout status daemonset/antrea-agent --timeout=10m']
    files.append(script(f"Install Antrea {v} (Helm)", ["kind: networking.disableDefaultCNI: true"], body, helm=True))
    return ConfigResult(None, c.errors, c.warnings, files=files, info=f"Antrea {v}")


# =========================================================================== Multus
MULTUS_SCHEMA = [
    ("Version & install", [
        Field("mu_version", "Multus version", "ecombo", default=VERSIONS["multus"], options=[VERSIONS["multus"]],
              required=True),
        Field("mu_flavor", "DaemonSet flavor", "combo", default="thick", options=MULTUS_FLAVORS),
        Field("mu_whereabouts", "Install Whereabouts IPAM", "check", default=False),
    ]),
    ("NetworkAttachmentDefinition", [
        Field("mu_nad_name", "Name", default="macvlan-conf", required=True),
        Field("mu_nad_namespace", "Namespace", default="default"),
        Field("mu_type", "Plugin type", "combo", default="macvlan", options=NAD_TYPES),
        Field("mu_master", "master (macvlan / ipvlan)", default="eth0", help="host interface; blank = default route interface"),
        Field("mu_macvlan_mode", "macvlan mode", "combo", default="bridge", options=MACVLAN_MODES),
        Field("mu_ipvlan_mode", "ipvlan mode", "combo", default="l2", options=IPVLAN_MODES),
        Field("mu_bridge", "bridge (bridge plugin)", default="br0"),
        Field("mu_is_gateway", "isGateway (bridge)", "check", default=False),
        Field("mu_ip_masq", "ipMasq (bridge)", "check", default=False),
        Field("mu_device", "device (host-device)", default="", help="e.g. eth1 or a PCI address"),
        Field("mu_mtu", "mtu", default="", help="blank = plugin default"),
    ]),
    ("IPAM", [
        Field("mu_ipam", "IPAM type", "combo", default="host-local", options=NAD_IPAM),
        Field("mu_subnet", "subnet / range", default="192.168.1.0/24"),
        Field("mu_range_start", "rangeStart", default="192.168.1.200"),
        Field("mu_range_end", "rangeEnd", default="192.168.1.216"),
        Field("mu_gateway", "gateway", default="192.168.1.1"),
        Field("mu_routes", "routes", "text", default="", height=2, help="dst[ via gw] per line, e.g. 0.0.0.0/0"),
        Field("mu_exclude", "exclude (whereabouts)", default="", help="comma-separated CIDRs"),
        Field("mu_static", "addresses (static)", "text", default="", height=2, help="ip/prefix[ gateway] per line"),
    ]),
    ("Example pod", [
        Field("mu_example", "Example pod using the network", "check", default=True),
    ]),
]


def _ip_in(c, ip, net, label):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        c.err(f"{label} '{ip}' is not an IP address.")
        return None
    if net and a not in net:
        c.err(f"{label} {ip} is outside {net}.")
    return a


def multus_nad_config(c) -> dict:
    name = c.s("mu_nad_name")
    dns(c, name, "NetworkAttachmentDefinition name")
    t = c.s("mu_type") or "macvlan"
    conf: dict[str, Any] = {"cniVersion": "1.0.0", "name": name, "type": t}
    if t == "macvlan":
        if c.s("mu_master"):
            conf["master"] = c.s("mu_master")
        conf["mode"] = c.s("mu_macvlan_mode") or "bridge"
    elif t == "ipvlan":
        if c.s("mu_master"):
            conf["master"] = c.s("mu_master")
        conf["mode"] = c.s("mu_ipvlan_mode") or "l2"
    elif t == "bridge":
        conf["bridge"] = c.s("mu_bridge") or "br0"
        if c.b("mu_is_gateway"):
            conf["isGateway"] = True
        if c.b("mu_ip_masq"):
            conf["ipMasq"] = True
    elif t == "host-device":
        if not c.s("mu_device"):
            c.err("host-device needs the device (interface name or PCI address) to move into the pod.")
        conf["device"] = c.s("mu_device")
    mtu = c.int("mu_mtu", "mtu", lo=576, hi=9216)
    if mtu:
        conf["mtu"] = mtu
    ipam_t = c.s("mu_ipam") or "host-local"
    net = None
    routes = []
    for line in _lines(c.raw("mu_routes")):
        parts = line.replace(" via ", " ").split()
        r = {"dst": parts[0]}
        cidr(c, parts[0], "route dst")
        if len(parts) > 1:
            _ip_in(c, parts[1], None, "route gateway")
            r["gw"] = parts[1]
        routes.append(r)
    if ipam_t == "host-local":
        net = cidr(c, c.s("mu_subnet"), "subnet")
        rng: dict[str, Any] = {"subnet": c.s("mu_subnet")}
        for key, k2 in (("mu_range_start", "rangeStart"), ("mu_range_end", "rangeEnd"), ("mu_gateway", "gateway")):
            if c.s(key):
                _ip_in(c, c.s(key), net, k2)
                rng[k2] = c.s(key)
        if c.s("mu_range_start") and c.s("mu_range_end"):
            try:
                if ipaddress.ip_address(c.s("mu_range_start")) > ipaddress.ip_address(c.s("mu_range_end")):
                    c.err("rangeStart must not be after rangeEnd.")
            except ValueError:
                pass
        ipam: dict[str, Any] = {"type": "host-local", "ranges": [[rng]]}
    elif ipam_t == "whereabouts":
        net = cidr(c, c.s("mu_subnet"), "range")
        ipam = {"type": "whereabouts", "range": c.s("mu_subnet")}
        if c.s("mu_range_start"):
            _ip_in(c, c.s("mu_range_start"), net, "range_start")
            ipam["range_start"] = c.s("mu_range_start")
        if c.s("mu_range_end"):
            _ip_in(c, c.s("mu_range_end"), net, "range_end")
            ipam["range_end"] = c.s("mu_range_end")
        ex = cidr_list(c, c.s("mu_exclude"), "exclude")
        if ex:
            ipam["exclude"] = [str(e) for e in ex]
        if c.s("mu_gateway"):
            _ip_in(c, c.s("mu_gateway"), net, "gateway")
            ipam["gateway"] = c.s("mu_gateway")
        if not c.b("mu_whereabouts"):
            c.warn("IPAM whereabouts needs the Whereabouts plugin: tick 'Install Whereabouts IPAM'.")
    elif ipam_t == "static":
        addrs = []
        for line in _lines(c.raw("mu_static")):
            parts = line.split()
            try:
                ipaddress.ip_interface(parts[0])
            except ValueError:
                c.err(f"static address '{parts[0]}' must be ip/prefix, e.g. 192.168.1.10/24.")
            a = {"address": parts[0]}
            if len(parts) > 1:
                _ip_in(c, parts[1], None, "static gateway")
                a["gateway"] = parts[1]
            addrs.append(a)
        if not addrs:
            c.err("static IPAM needs at least one address (ip/prefix per line).")
        ipam = {"type": "static", "addresses": addrs}
    elif ipam_t == "dhcp":
        ipam = {"type": "dhcp"}
        c.warn("dhcp IPAM needs the CNI dhcp daemon running on every node (`/opt/cni/bin/dhcp daemon`) and a "
               "DHCP server on the attached network.")
        if t == "bridge" and not c.b("mu_is_gateway"):
            pass
    else:
        ipam = {}
    if routes and ipam_t in ("host-local", "whereabouts", "static"):
        ipam["routes"] = routes
    elif routes:
        c.warn("routes are ignored for this IPAM type.")
    conf["ipam"] = ipam
    if t in ("macvlan", "ipvlan") and ipam_t == "none":
        c.warn("No IPAM: the interface comes up without an address.")
    return conf


def build_multus(c) -> ConfigResult:
    v = version(c, "mu_version", "Multus version", v_prefix=True)
    ns = c.s("mu_nad_namespace") or "default"
    dns(c, ns, "Namespace", "label")
    conf = multus_nad_config(c)
    nad = {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition",
           "metadata": {"name": conf["name"], "namespace": ns}, "spec": {"config": to_json(conf, indent=2)}}
    files = [yaml_file("network-attachment-definition.yaml", [nad])]
    if c.b("mu_example"):
        pod = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"name": f"{conf['name']}-test", "namespace": ns,
                            "annotations": {"k8s.v1.cni.cncf.io/networks": conf["name"]}},
               "spec": {"containers": [{"name": "shell", "image": "busybox:1.37",
                                        "command": ["sh", "-c", "ip addr; sleep 3600"]}]}}
        files.append(yaml_file("example-pod.yaml", [pod],
                               header="# kubectl exec -it <pod> -- ip addr   (net1 is the extra interface)\n"))
    flavor = c.s("mu_flavor") or "thick"
    dsfile = "multus-daemonset-thick.yml" if flavor == "thick" else "multus-daemonset.yml"
    body = [f'VERSION="{v}"',
            f'"$KUBECTL" apply -f "https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/$VERSION/deployments/{dsfile}"',
            '"$KUBECTL" -n kube-system rollout status daemonset/kube-multus-ds --timeout=5m']
    if c.b("mu_whereabouts"):
        wv = VERSIONS["whereabouts"]
        wb = f"https://raw.githubusercontent.com/k8snetworkplumbingwg/whereabouts/{wv}/doc/crds"
        body += ["", f"# Whereabouts {wv} (cluster-wide IPAM for secondary networks)",
                 f'"$KUBECTL" apply -f {wb}/daemonset-install.yaml',
                 f'"$KUBECTL" apply -f {wb}/whereabouts.cni.cncf.io_ippools.yaml',
                 f'"$KUBECTL" apply -f {wb}/whereabouts.cni.cncf.io_overlappingrangeipreservations.yaml']
    body += ["", '"$KUBECTL" wait --for=condition=Established crd/network-attachment-definitions.k8s.cni.cncf.io --timeout=120s',
             '"$KUBECTL" apply -f network-attachment-definition.yaml']
    if c.b("mu_example"):
        body += ['"$KUBECTL" apply -f example-pod.yaml']
    files.append(script(f"Install Multus {v} ({flavor}) + the NetworkAttachmentDefinition", [
        "Multus is a meta-plugin: keep your primary CNI (Calico, Cilium, ...) and add secondary interfaces.",
        f"The {conf['type']} binary must exist in /opt/cni/bin on every node (containernetworking plugins)."], body))
    return ConfigResult(None, c.errors, c.warnings, files=files, info=f"Multus {v} + {conf['type']}")


TYPES = {
    "Calico": (CALICO_SCHEMA, build_calico, calico_pod_cidrs),
    "Cilium": (CILIUM_SCHEMA, build_cilium, cilium_pod_cidrs),
    "Flannel": (FLANNEL_SCHEMA, build_flannel, flannel_pod_cidrs),
    "Canal": (CANAL_SCHEMA, build_canal, canal_pod_cidrs),
    "Antrea": (ANTREA_SCHEMA, build_antrea, antrea_pod_cidrs),
    "Multus": (MULTUS_SCHEMA, build_multus, lambda c: []),
}
PRIMARY_CNI = ["Calico", "Cilium", "Flannel", "Canal", "Antrea"]
COLLAPSED = {"eBPF dataplane", "Components", "Hubble", "Security", "Features", "Proxy & IPAM", "Example pod"}


def check_row(table_key: str, sc) -> None:
    if table_key == "cal_pools":
        from manifest_gen import _Ctx
        ctx = _Ctx({"cal_pools": [sc.v], "cal_version": VERSIONS["calico"]})
        build_calico(ctx)
        sc.errors.extend(e.split(": ", 1)[1] if e.startswith("IP pool #") else e for e in ctx.errors
                         if e.startswith("IP pool #"))
