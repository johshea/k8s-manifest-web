"""
Kubespray (v2.32): Ansible inventory (inventory.ini) + group_vars/k8s_cluster/{k8s-cluster,addons}.yml.
Copy the output into kubespray/inventory/<name>/ and run:
  ansible-playbook -i inventory/<name>/inventory.ini --become cluster.yml
Validated with `ansible-inventory --list` and kubespray's own validate_inventory role (offline: it only
uses inventory vars), plus every variable name is checked to exist in the kubespray 2.32.0 tree.
"""
from __future__ import annotations

import re

from .common import (TABLE_FIELDS, Field, ConfigResult, OutFile, _cidrs, _csv, _ip, _no_overlap, _port, _rows,
                     _SubCtx, _san, required, table, to_yaml)

TYPE = "Kubespray"
GROUP = "Self-managed"

KUBE_VERSIONS = ["1.35.8", "1.36.4", "1.34.11"]   # from roles/kubespray_defaults/vars/main/checksums.yml (2.32.0)
SUPPORTED_MINORS = ("1.34", "1.35", "1.36")
PLUGINS = ["calico", "cilium", "flannel", "kube-ovn", "kube-router", "macvlan", "cni", "custom_cni", "cloud", "none"]
PROXY_MODES = ["ipvs", "iptables", "nftables"]
RUNTIMES = ["containerd", "crio", "docker"]
DNS_MODES = ["coredns", "coredns_dual", "manual", "none"]
ETCD_TYPES = ["host", "kubeadm", "docker"]
ROLES = ["control-plane+etcd", "node", "control-plane+etcd+node", "control-plane", "etcd"]
ADDONS = ["helm", "metrics_server", "local_path_provisioner", "cert_manager", "ingress_nginx", "metallb",
          "argocd", "gateway_api", "node_feature_discovery", "kube_vip"]

TABLE_FIELDS["ks_hosts"] = [
    Field("name", "inventory name", default="node1", required=True),
    Field("ansible_host", "ansible_host (SSH address)", default="192.168.10.11", required=True),
    Field("ip", "ip (bind address)", default="", help="blank = default interface"),
    Field("roles", "roles", "combo", default="control-plane+etcd", options=ROLES),
    Field("ansible_user", "ansible_user", default=""),
]

SCHEMA = [
    ("Cluster", [
        Field("ks_inventory", "inventory name", default="mycluster", required=True,
              help="files go to kubespray/inventory/<name>/"),
        Field("ks_version", "kube_version", "ecombo", default=KUBE_VERSIONS[0], options=KUBE_VERSIONS,
              help="kubespray 2.32 supports 1.34.0 - 1.36.4"),
        Field("ks_cluster_name", "cluster_name (DNS domain)", default="cluster.local"),
        Field("ks_runtime", "container_manager", "combo", default="containerd", options=RUNTIMES),
        Field("ks_etcd", "etcd_deployment_type", "combo", default="host", options=ETCD_TYPES),
        Field("ks_user", "ansible_user (all hosts)", default="ubuntu"),
        table("ks_hosts", "Hosts", ["name", "ansible_host", "ip", "roles"],
              default=[{"name": f"node{i}", "ansible_host": f"192.168.10.1{i}", "ip": "",
                        "roles": "control-plane+etcd" if i <= 3 else "node", "ansible_user": ""}
                       for i in range(1, 6)]),
    ]),
    ("Networking", [
        Field("ks_plugin", "kube_network_plugin", "combo", default="calico", options=PLUGINS),
        Field("ks_proxy", "kube_proxy_mode", "combo", default="ipvs", options=PROXY_MODES),
        Field("ks_strict_arp", "kube_proxy_strict_arp", "check", default=False, help="needed by MetalLB with ipvs"),
        Field("ks_svc", "kube_service_addresses", default="10.233.0.0/18"),
        Field("ks_pods", "kube_pods_subnet", default="10.233.64.0/18"),
        Field("ks_node_prefix", "kube_network_node_prefix", default="24"),
        Field("ks_ipv6", "ipv6_stack (dual-stack)", "check", default=False),
        Field("ks_dns", "dns_mode", "combo", default="coredns", options=DNS_MODES),
        Field("ks_nodelocaldns", "enable_nodelocaldns", "check", default=True),
        Field("ks_api_port", "kube_apiserver_port", default="6443"),
        Field("ks_sans", "supplementary_addresses_in_ssl_keys", default="", help="extra API cert SANs (LB IP/DNS)"),
    ]),
    ("Options & add-ons", [
        Field("ks_kubeconfig_localhost", "kubeconfig_localhost (copy admin.conf back)", "check", default=True),
        Field("ks_encrypt", "kube_encrypt_secret_data", "check", default=False),
        Field("ks_autorenew", "auto_renew_certificates", "check", default=False),
        Field("ks_addons", "addons (<name>_enabled)", default="helm,metrics_server",
              help="comma-separated: " + ", ".join(ADDONS)),
        Field("ks_metallb_range", "metallb ip range", default="", help="e.g. 192.168.10.200-192.168.10.220"),
    ]),
]
COLLAPSED = {"Options & add-ons"}

_HOSTNAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def check_row(key, sc):
    if key != "ks_hosts":
        return
    if not _HOSTNAME.match(sc.s("name") or ""):
        sc.err("inventory name must be a lowercase hostname (it becomes the node name).")
    h = sc.s("ansible_host")
    if not h:
        sc.err("ansible_host is required.")
    elif not re.match(r"^[A-Za-z0-9.:-]+$", h):
        sc.err(f"ansible_host '{h}' is not an IP or hostname.")
    if sc.s("ip"):
        _ip(sc, sc.s("ip"), "ip")
    if sc.s("roles") not in ROLES:
        sc.err(f"roles must be one of {', '.join(ROLES)}.")


def _ini(groups, hostvars):
    lines = ["# Generated by k8s-manifest-gui (kubespray inventory)"]
    lines.append("[all]")
    for h, hv in hostvars.items():
        lines.append(" ".join([h] + [f"{k}={v}" for k, v in hv.items()]))
    for g in ("kube_control_plane", "etcd", "kube_node"):
        lines += ["", f"[{g}]"] + groups[g]
    lines += ["", "[k8s_cluster:children]", "kube_control_plane", "kube_node", ""]
    return "\n".join(lines)


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    inv = c.s("ks_inventory")
    if inv and not re.match(r"^[A-Za-z0-9_.-]+$", inv):
        c.err("inventory name must be a simple directory name.")
    groups = {"kube_control_plane": [], "etcd": [], "kube_node": []}
    hostvars: dict = {}
    seen_addr = set()
    for i, r in enumerate(_rows(c, "ks_hosts"), 1):
        sc = _SubCtx(c, r, f"host #{i} ({r['name']}): ")
        check_row("ks_hosts", sc)
        if r["name"] in hostvars:
            sc.err("duplicate inventory name.")
        if r["ansible_host"] in seen_addr:
            sc.err("duplicate ansible_host.")
        seen_addr.add(r["ansible_host"])
        hv = {"ansible_host": r["ansible_host"]}
        if r.get("ip"):
            hv["ip"] = r["ip"]
        if r.get("ansible_user"):
            hv["ansible_user"] = r["ansible_user"]
        roles = r["roles"] or "node"
        if "etcd" in roles:
            hv["etcd_member_name"] = f"etcd{len(groups['etcd']) + 1}"
        hostvars[r["name"]] = hv
        if "control-plane" in roles:
            groups["kube_control_plane"].append(r["name"])
        if "etcd" in roles:
            groups["etcd"].append(r["name"])
        if roles.endswith("node") or roles == "node":
            groups["kube_node"].append(r["name"])
    ncp, netcd = len(groups["kube_control_plane"]), len(groups["etcd"])
    etcd_type = c.s("ks_etcd") or "host"
    if not ncp:
        c.err("at least one host needs the control-plane role (kube_control_plane can't be empty).")
    if etcd_type != "kubeadm" and not netcd:
        c.err("the etcd group is empty: give some hosts the etcd role (or use etcd_deployment_type kubeadm).")
    if netcd and netcd % 2 == 0:
        c.err(f"etcd needs an odd number of members for quorum (got {netcd}).")
    if not groups["kube_node"]:
        c.warn("no host has the node role: workloads will only run if control-plane taints are removed.")
    k8s_hosts = set(groups["kube_control_plane"]) | set(groups["kube_node"])

    ver = c.s("ks_version")
    if ver:
        v = ver.lstrip("v")
        if not re.match(r"^\d+\.\d+\.\d+$", v):
            c.err("kube_version must be a full version like 1.35.8 (no 'v').")
        elif not v.startswith(tuple(m + "." for m in SUPPORTED_MINORS)):
            c.err(f"kube_version {v} isn't supported by kubespray 2.32 (1.34.0 - 1.36.4).")
        elif v.startswith("1.36.") and int(v.split(".")[2]) > 4 or v.startswith("1.35.") and int(v.split(".")[2]) > 8 \
                or v.startswith("1.34.") and int(v.split(".")[2]) > 11:
            c.err(f"kube_version {v} has no checksums in kubespray 2.32 (latest: 1.36.4 / 1.35.8 / 1.34.11).")
        ver = v
    runtime = c.s("ks_runtime") or "containerd"
    if etcd_type == "docker" and runtime != "docker":
        c.err("etcd_deployment_type docker requires container_manager docker.")
    if runtime == "docker":
        c.warn("container_manager docker uses cri-dockerd; containerd is the default and best supported.")
    plugin, proxy = c.s("ks_plugin") or "calico", c.s("ks_proxy") or "ipvs"
    for v, opts, n in ((plugin, PLUGINS, "kube_network_plugin"), (proxy, PROXY_MODES, "kube_proxy_mode"),
                       (c.s("ks_dns") or "coredns", DNS_MODES, "dns_mode"), (runtime, RUNTIMES, "container_manager"),
                       (etcd_type, ETCD_TYPES, "etcd_deployment_type")):
        if v not in opts:
            c.err(f"{n} must be one of {', '.join(opts)}.")
    svc = _cidrs(c, c.s("ks_svc"), "kube_service_addresses")
    pods = _cidrs(c, c.s("ks_pods"), "kube_pods_subnet")
    for nets, n in ((svc, "kube_service_addresses"), (pods, "kube_pods_subnet")):
        if not nets:
            c.err(f"{n} is required.")
        elif nets[0].version != 4:
            c.err(f"{n} is the IPv4 range; IPv6 uses the *_ipv6 variables.")
    _no_overlap(c, pods, svc, "kube_pods_subnet", "kube_service_addresses")
    prefix = c.int("ks_node_prefix", "kube_network_node_prefix", lo=16, hi=28, required=True)
    if prefix is not None and pods:
        if prefix <= pods[0].prefixlen:
            c.err(f"kube_network_node_prefix /{prefix} must be longer than kube_pods_subnet /{pods[0].prefixlen}.")
        elif 2 ** (prefix - pods[0].prefixlen) < len(k8s_hosts):
            c.err(f"kube_pods_subnet {pods[0]} split into /{prefix} fits only {2 ** (prefix - pods[0].prefixlen)} "
                  f"nodes but the inventory has {len(k8s_hosts)}.")
        if 2 ** (32 - prefix) - 2 < 110:
            c.err(f"kube_network_node_prefix /{prefix} gives fewer than 110 pod IPs per node (kubelet max_pods).")
    port = _port(c, c.s("ks_api_port"), "kube_apiserver_port", default=6443)
    sans = _csv(c.s("ks_sans"))
    for s in sans:
        _san(c, s, "supplementary_addresses_in_ssl_keys")
    if ncp > 1 and not sans:
        c.warn("multiple control planes: add your load balancer address to supplementary_addresses_in_ssl_keys "
               "(kubespray otherwise uses its localhost nginx proxy on each node).")
    cn = c.s("ks_cluster_name") or "cluster.local"
    if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$", cn):
        c.err("cluster_name must be a DNS domain (it's the cluster DNS suffix).")

    k8s: dict = {"kube_version": ver} if ver else {}
    k8s.update({
        "cluster_name": cn,
        "kube_network_plugin": plugin,
        "kube_proxy_mode": proxy,
        "kube_service_addresses": str(svc[0]) if svc else "",
        "kube_pods_subnet": str(pods[0]) if pods else "",
        "kube_network_node_prefix": prefix,
        "kube_apiserver_port": port,
        "container_manager": runtime,
        "dns_mode": c.s("ks_dns") or "coredns",
        "enable_nodelocaldns": c.b("ks_nodelocaldns"),
        "kubeconfig_localhost": c.b("ks_kubeconfig_localhost"),
        "kube_encrypt_secret_data": c.b("ks_encrypt"),
        "auto_renew_certificates": c.b("ks_autorenew"),
        "etcd_deployment_type": etcd_type,
    })
    if c.b("ks_strict_arp"):
        k8s["kube_proxy_strict_arp"] = True
    if c.b("ks_ipv6"):
        k8s["ipv6_stack"] = True
    if sans:
        k8s["supplementary_addresses_in_ssl_keys"] = sans
    addons: dict = {}
    for a in _csv(c.s("ks_addons")):
        if a not in ADDONS:
            c.err(f"addon '{a}' is not one of: {', '.join(ADDONS)}.")
        else:
            addons[f"{a}_enabled"] = True
    rng = c.s("ks_metallb_range")
    if addons.get("metallb_enabled"):
        if proxy == "ipvs" and not c.b("ks_strict_arp"):
            c.err("MetalLB with kube_proxy_mode ipvs requires kube_proxy_strict_arp.")
        if not rng:
            c.err("MetalLB needs an ip range.")
        else:
            m = re.match(r"^\s*([0-9.]+)\s*-\s*([0-9.]+)\s*$", rng)
            if m:
                a, b = _ip(c, m.group(1), "metallb range start"), _ip(c, m.group(2), "metallb range end")
                if a and b and a > b:
                    c.err("metallb range start must be <= end.")
            else:
                _cidrs(c, rng, "metallb range")
            addons["metallb_config"] = {"address_pools": {"primary": {"ip_range": [rng], "auto_assign": True}},
                                        "layer2": ["primary"]}
    elif rng:
        c.warn("metallb ip range is ignored unless the metallb addon is enabled.")
    if addons.get("metallb_enabled") and addons.get("kube_vip_enabled"):
        c.warn("kube_vip and metallb both announce addresses; make sure their ranges don't clash.")
    allv = {"ansible_user": c.s("ks_user")} if c.s("ks_user") else {}
    prefix_dir = ""
    files = [OutFile(prefix_dir + "inventory.ini", _ini(groups, hostvars))]
    if allv:
        files.append(OutFile("group_vars/all/all.yml", "# Generated by k8s-manifest-gui\n" + to_yaml(allv)))
    files.append(OutFile("group_vars/k8s_cluster/k8s-cluster.yml",
                         "# Generated by k8s-manifest-gui - overrides of kubespray defaults\n" + to_yaml(k8s)))
    files.append(OutFile("group_vars/k8s_cluster/addons.yml",
                         "# Generated by k8s-manifest-gui\n" + (to_yaml(addons) if addons else "{}\n")))
    res = ConfigResult([], c.errors, c.warnings, "inventory.ini",
                       info=f"kubespray inventory '{inv}' ({ncp} cp / {netcd} etcd / {len(groups['kube_node'])} node)")
    res.files = files
    return res
