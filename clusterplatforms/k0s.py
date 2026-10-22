"""
k0s: k0sctl.yaml (k0sctl.k0sproject.io/v1beta1 Cluster - multi-node install over SSH) and/or
k0s.yaml (k0s.k0sproject.io/v1beta1 ClusterConfig - `k0s install controller --config`).
Field names checked against `k0sctl init --k0s` and `k0s config create` (k0s v1.36.4+k0s.1,
k0sctl v0.33.1); k0s.yaml validated with `k0s config validate --config`.
"""
from __future__ import annotations

import re

from .common import (TABLE_FIELDS, Field, ConfigResult, OutFile, _cidrs, _csv, _ip, _lines, _no_overlap, _port,
                     _san, _rows, _SubCtx, _parse_kv, required, table, to_yaml, _DNS1123_SUB)

TYPE = "k0s"
GROUP = "Local"

OUTPUTS = ["k0sctl.yaml (multi-node over SSH)", "k0s.yaml (ClusterConfig only)", "both"]
ROLES = ["controller", "worker", "controller+worker", "single"]
PROVIDERS = ["kuberouter", "calico", "custom"]
PROXY = ["iptables", "ipvs", "nftables"]
STORAGE = ["etcd", "kine"]
K0S_VERSIONS = ["", "v1.36.4+k0s.1", "v1.35.8+k0s.0", "v1.34.9+k0s.0"]

TABLE_FIELDS["k0s_hosts"] = [
    Field("role", "role", "combo", default="worker", options=ROLES),
    Field("address", "ssh address", default="", required=True, help="IP or hostname"),
    Field("user", "ssh user", default="root"),
    Field("port", "ssh port", default="22"),
    Field("key_path", "keyPath", default="", help="blank = ~/.ssh/id_rsa etc. / ssh-agent"),
    Field("hostname", "hostname override", default=""),
    Field("install_flags", "installFlags", default="", help="comma-separated, e.g. --debug,--labels=a=b"),
]

SCHEMA = [
    ("Output", [
        Field("k0_output", "Generate", "combo", default=OUTPUTS[0], options=OUTPUTS,
              help="k0sctl apply --config k0sctl.yaml installs every host; k0s.yaml is the ClusterConfig for "
                   "`k0s install controller --config /etc/k0s/k0s.yaml`"),
        Field("k0_cluster_name", "cluster name", default="k0s-cluster", required=True),
        Field("k0_version", "k0s version", "ecombo", default="", options=K0S_VERSIONS,
              help="blank = latest stable chosen by k0sctl"),
    ]),
    ("Hosts (k0sctl)", [
        table("k0s_hosts", "Hosts", ["role", "address", "user", "port"],
              default=[{"role": "controller", "address": "10.0.0.10", "user": "root", "port": "22", "key_path": "",
                        "hostname": "", "install_flags": ""},
                       {"role": "worker", "address": "10.0.0.11", "user": "root", "port": "22", "key_path": "",
                        "hostname": "", "install_flags": ""}]),
    ]),
    ("API & storage", [
        Field("k0_api_address", "api.address", default="", help="blank = first controller's IP"),
        Field("k0_api_port", "api.port", default="", help="blank = 6443"),
        Field("k0_api_sans", "api.sans", default="", help="comma-separated extra IPs / DNS names"),
        Field("k0_external_address", "api.externalAddress", default="",
              help="load balancer address for HA control planes"),
        Field("k0_storage", "storage.type", "combo", default="etcd", options=STORAGE,
              help="kine = sqlite/mysql/postgres datasource (single controller)"),
        Field("k0_kine_ds", "kine dataSource", default="", help="e.g. sqlite:///var/lib/k0s/db/state.db"),
    ]),
    ("Network", [
        Field("k0_provider", "network.provider", "combo", default="kuberouter", options=PROVIDERS,
              help="custom = bring your own CNI"),
        Field("k0_pod_cidr", "podCIDR", default="10.244.0.0/16"),
        Field("k0_service_cidr", "serviceCIDR", default="10.96.0.0/12"),
        Field("k0_cluster_domain", "clusterDomain", default="cluster.local"),
        Field("k0_proxy_mode", "kubeProxy.mode", "combo", default="iptables", options=PROXY),
        Field("k0_proxy_disabled", "kubeProxy.disabled", "check", default=False),
        Field("k0_nllb", "nodeLocalLoadBalancing (EnvoyProxy)", "check", default=False,
              help="workers reach all controllers via a local proxy - recommended for HA without an external LB"),
    ]),
    ("Telemetry & options", [
        Field("k0_telemetry", "telemetry.enabled", "check", default=False),
        Field("k0_disable_wait", "options.wait.enabled = false", "check", default=False),
    ]),
]
COLLAPSED = {"Telemetry & options"}


def check_row(key, sc):
    if key != "k0s_hosts":
        return
    if sc.s("role") not in ROLES:
        sc.err(f"role must be one of {', '.join(ROLES)}.")
    a = sc.s("address")
    if not a:
        sc.err("ssh address is required.")
    else:
        _san(sc, a, "ssh address")
    _port(sc, sc.s("port") or "22", "ssh port")
    if sc.s("hostname") and not _DNS1123_SUB.match(sc.s("hostname")):
        sc.err(f"hostname '{sc.s('hostname')}' must be a lowercase DNS name.")
    for fl in _csv(sc.s("install_flags")):
        if not fl.startswith("--"):
            sc.err(f"installFlags '{fl}' must start with --.")


def _cluster_config(c, first_controller):
    spec: dict = {}
    api: dict = {}
    addr = c.s("k0_api_address")
    if addr:
        _ip(c, addr, "api.address")
        api["address"] = addr
    port = _port(c, c.s("k0_api_port"), "api.port")
    if port:
        api["port"] = port
    sans = _csv(c.s("k0_api_sans"))
    for s in sans:
        _san(c, s, "api.sans")
    if sans:
        api["sans"] = sans
    ext = c.s("k0_external_address")
    if ext:
        _san(c, ext, "api.externalAddress")
        api["externalAddress"] = ext
    if api:
        spec["api"] = api
    net: dict = {}
    prov = c.s("k0_provider") or "kuberouter"
    if prov not in PROVIDERS:
        c.err(f"network.provider must be one of {', '.join(PROVIDERS)}.")
    net["provider"] = prov
    pods = _cidrs(c, c.s("k0_pod_cidr"), "podCIDR")
    svcs = _cidrs(c, c.s("k0_service_cidr"), "serviceCIDR")
    _no_overlap(c, pods, svcs, "podCIDR", "serviceCIDR")
    for nets, key in ((pods, "podCIDR"), (svcs, "serviceCIDR")):
        if len(nets) > 1:
            c.err(f"{key}: k0s takes one IPv4 CIDR here (dual-stack uses network.dualStack).")
        elif nets:
            net[key] = str(nets[0])
    dom = c.s("k0_cluster_domain")
    if dom:
        if not _DNS1123_SUB.match(dom):
            c.err(f"clusterDomain '{dom}' is not a valid DNS domain.")
        net["clusterDomain"] = dom
    mode = c.s("k0_proxy_mode") or "iptables"
    if mode not in PROXY:
        c.err(f"kubeProxy.mode must be one of {', '.join(PROXY)}.")
    kp = {"mode": mode}
    if c.b("k0_proxy_disabled"):
        kp = {"disabled": True}
        if prov != "custom":
            c.warn("kubeProxy.disabled with kuberouter/calico: Services need a kube-proxy replacement "
                   "(e.g. Cilium via provider custom).")
    net["kubeProxy"] = kp
    if mode == "ipvs" and not c.b("k0_proxy_disabled"):
        c.warn("ipvs kube-proxy mode is deprecated since Kubernetes v1.35; prefer nftables or iptables.")
    if c.b("k0_nllb"):
        net["nodeLocalLoadBalancing"] = {"enabled": True, "type": "EnvoyProxy"}
    spec["network"] = net
    st = c.s("k0_storage") or "etcd"
    if st not in STORAGE:
        c.err("storage.type must be etcd or kine.")
    storage: dict = {"type": st}
    if st == "kine":
        ds = c.s("k0_kine_ds")
        if not ds:
            c.err("kine dataSource is required with storage.type kine.")
        elif not re.match(r"^(sqlite|mysql|postgres|nats)://", ds):
            c.err("kine dataSource must start with sqlite://, mysql://, postgres:// or nats://.")
        storage["kine"] = {"dataSource": ds}
    elif c.s("k0_kine_ds"):
        c.warn("kine dataSource is ignored with storage.type etcd.")
    spec["storage"] = storage
    spec["telemetry"] = {"enabled": c.b("k0_telemetry")}
    return {"apiVersion": "k0s.k0sproject.io/v1beta1", "kind": "ClusterConfig",
            "metadata": {"name": c.s("k0_cluster_name") or "k0s"}, "spec": spec}


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    mode = c.s("k0_output") or OUTPUTS[0]
    name = c.s("k0_cluster_name")
    if name and not _DNS1123_SUB.match(name):
        c.err(f"cluster name '{name}' must be a lowercase DNS-1123 name.")
    ver = c.s("k0_version")
    if ver and not re.match(r"^v\d+\.\d+\.\d+(-[a-z0-9.]+)?\+k0s\.\d+$", ver):
        c.err(f"k0s version '{ver}' must look like v1.36.4+k0s.1.")
    hosts = []
    controllers = 0
    seen = set()
    for i, r in enumerate(_rows(c, "k0s_hosts"), 1):
        sc = _SubCtx(c, r, f"host #{i}: ")
        check_row("k0s_hosts", sc)
        if r["address"] in seen:
            sc.err(f"duplicate address {r['address']}.")
        seen.add(r["address"])
        if r["role"] in ("controller", "controller+worker", "single"):
            controllers += 1
        ssh = {"address": r["address"], "user": r["user"] or "root", "port": int(r["port"] or 22)
               if str(r["port"] or "22").isdigit() else 22}
        if r["key_path"]:
            ssh["keyPath"] = r["key_path"]
        h = {"role": r["role"], "ssh": ssh}
        if r["hostname"]:
            h["hostname"] = r["hostname"]
        if _csv(r["install_flags"]):
            h["installFlags"] = _csv(r["install_flags"])
        hosts.append(h)
    if mode != OUTPUTS[1]:
        if not hosts:
            c.err("k0sctl.yaml needs at least one host.")
        elif controllers == 0:
            c.err("k0sctl.yaml needs at least one controller (controller, controller+worker or single).")
        if any(h["role"] == "single" for h in hosts) and len(hosts) > 1:
            c.err("role single means a one-node cluster; use controller+worker / worker for more hosts.")
        if controllers == 2:
            c.warn("2 controllers: etcd needs an odd number (1, 3, 5) for quorum.")
        if controllers > 1 and not c.s("k0_external_address") and not c.b("k0_nllb"):
            c.warn("multiple controllers: set api.externalAddress (load balancer) or enable "
                   "nodeLocalLoadBalancing so workers survive a controller failure.")
        if controllers > 1 and (c.s("k0_storage") == "kine"):
            c.err("kine storage supports a single controller; use etcd for HA.")
    first = next((h["ssh"]["address"] for h in hosts if h["role"] != "worker"), "")
    cc = _cluster_config(c, first)
    files = []
    if mode != OUTPUTS[1]:
        k0 = {"config": {**cc, "metadata": {"name": cc["metadata"]["name"]}}}
        if ver:
            k0 = {"version": ver, **k0}
        doc = {"apiVersion": "k0sctl.k0sproject.io/v1beta1", "kind": "Cluster",
               "metadata": {"name": name or "k0s-cluster"},
               "spec": {"hosts": hosts, "k0s": k0}}
        if c.b("k0_disable_wait"):
            doc["spec"]["options"] = {"wait": {"enabled": False}}
        files.append(OutFile("k0sctl.yaml", "# k0sctl apply --config k0sctl.yaml\n" + to_yaml(doc)))
    if mode != OUTPUTS[0]:
        files.append(OutFile("k0s.yaml", "# k0s install controller --config /etc/k0s/k0s.yaml  "
                                         "(validate: k0s config validate --config k0s.yaml)\n" + to_yaml(cc)))
    return ConfigResult(None, c.errors, c.warnings, files[0].name if files else "k0sctl.yaml",
                        info="k0sctl.k0sproject.io/v1beta1 · k0s.k0sproject.io/v1beta1", files=files)
