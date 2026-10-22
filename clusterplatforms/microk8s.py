"""
MicroK8s launch configuration (version 0.1.0 / 0.2.0), read at `snap install microk8s` from
/var/snap/microk8s/common/.microk8s.yaml or applied with `snap set microk8s config="$(cat ...)"`.
Keys follow canonical/microk8s-cluster-agent pkg/k8sinit/schema.go.
"""
from __future__ import annotations

import re

from .common import Field, ConfigResult, OutFile, _csv, _lines, _san, did_you_mean, required, to_yaml

TYPE = "MicroK8s"
GROUP = "Local"

CHANNELS = ["1.36/stable", "1.35/stable", "1.34/stable", "latest/stable", "latest/edge"]
CORE_ADDONS = ["dns", "rbac", "hostpath-storage", "ingress", "metrics-server", "dashboard", "registry",
               "cert-manager", "metallb", "observability", "community", "gpu", "helm", "helm3", "host-access",
               "mayastor", "minio", "prometheus", "rook-ceph", "storage", "kube-ovn", "cis-hardening"]
ARG_SECTIONS = {"extraKubeAPIServerArgs": "kube-apiserver", "extraKubeletArgs": "kubelet",
                "extraKubeProxyArgs": "kube-proxy", "extraKubeControllerManagerArgs": "kube-controller-manager",
                "extraKubeSchedulerArgs": "kube-scheduler", "extraContainerdArgs": "containerd"}

TABLES: dict = {}
SCHEMA = [
    ("Install", [
        Field("m8_version", "launch config version", "combo", default="0.1.0", options=["0.1.0", "0.2.0"]),
        Field("m8_channel", "snap channel (for the install hint)", "ecombo", default=CHANNELS[0], options=CHANNELS),
    ]),
    ("Addons", [
        Field("m8_addons", "enable", "text", default="dns\nhostpath-storage", height=4,
              help="one addon per line; optional args after a colon, e.g. metallb:10.64.140.43-10.64.140.49"),
        Field("m8_disable", "disable", default="", help="comma-separated addons to disable (e.g. ha-cluster)"),
    ]),
    ("Kubernetes arguments", [
        Field("m8_apiserver_args", "extraKubeAPIServerArgs", "text", default="", height=2,
              help="--flag=value per line ('--flag' alone = remove the flag)"),
        Field("m8_kubelet_args", "extraKubeletArgs", "text", default="", height=2, help="--flag=value per line"),
        Field("m8_proxy_args", "extraKubeProxyArgs", "text", default="", height=2, help="--flag=value per line"),
        Field("m8_cm_args", "extraKubeControllerManagerArgs", "text", default="", height=2),
        Field("m8_sched_args", "extraKubeSchedulerArgs", "text", default="", height=2),
        Field("m8_containerd_args", "extraContainerdArgs", "text", default="", height=2),
        Field("m8_sans", "extraSANs", default="", help="comma-separated IPs / DNS names for the API cert"),
    ]),
    ("Registries & cluster", [
        Field("m8_registry_mirrors", "containerdRegistryConfigs", "text", default="", height=2,
              help="registry=mirror-URL per line, e.g. docker.io=https://mirror.example.com"),
        Field("m8_cluster_token", "persistentClusterToken", default="",
              help="32+ char token so other nodes can `microk8s join <ip>:25000/<token>` at any time"),
        Field("m8_join_url", "join.url", default="", help="join an existing cluster: <ip>:25000/<token>"),
        Field("m8_join_worker", "join.worker", "check", default=False),
    ]),
]
COLLAPSED = {"Registries & cluster"}


def _args(c, key, section):
    out = {}
    for line in _lines(c.s(key)):
        m = re.match(r"^(--[a-z0-9][a-z0-9-]*)(?:=(.*))?$", line)
        if not m:
            c.err(f"{section}: '{line}' must be --flag=value (or --flag to remove it).")
            continue
        out[m.group(1)] = m.group(2) if m.group(2) is not None else None
    return out


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    if (c.s("m8_version") or "0.1.0") not in ("0.1.0", "0.2.0"):
        c.err("launch config version must be 0.1.0 or 0.2.0 (what the MicroK8s cluster-agent understands).")
    cfg: dict = {"version": c.s("m8_version") or "0.1.0"}
    addons = []
    names = set()
    for line in _lines(c.s("m8_addons")):
        name, _, args = line.partition(":")
        name = name.strip()
        if not re.match(r"^([a-z0-9-]+/)?[a-z0-9][a-z0-9-]*$", name):
            c.err(f"addon '{name}' is not a valid addon name.")
            continue
        base = name.split("/")[-1]
        if "/" not in name and base not in CORE_ADDONS:
            c.warn(f"addon '{name}' is not a core/community addon I know of.{did_you_mean(name, CORE_ADDONS)}")
        names.add(base)
        a = {"name": name}
        if args.strip():
            a["args"] = [x for x in re.split(r"\s+", args.strip()) if x]
        addons.append(a)
    for d in _csv(c.s("m8_disable")):
        if d in names:
            c.err(f"addon '{d}' is both enabled and disabled.")
        addons.append({"name": d, "disable": True})
    if addons:
        cfg["addons"] = addons
    if "metallb" in names and not any(a.get("args") for a in addons if a["name"] == "metallb"):
        c.warn("metallb needs an address range, e.g. metallb:10.64.140.43-10.64.140.49.")
    if "rbac" not in names:
        pass
    for key, section in (("m8_apiserver_args", "extraKubeAPIServerArgs"), ("m8_kubelet_args", "extraKubeletArgs"),
                         ("m8_proxy_args", "extraKubeProxyArgs"), ("m8_cm_args", "extraKubeControllerManagerArgs"),
                         ("m8_sched_args", "extraKubeSchedulerArgs"), ("m8_containerd_args", "extraContainerdArgs")):
        a = _args(c, key, section)
        if a:
            cfg[section] = a
    sans = _csv(c.s("m8_sans"))
    for s in sans:
        _san(c, s, "extraSANs")
    if sans:
        cfg["extraSANs"] = sans
    regs = {}
    for line in _lines(c.s("m8_registry_mirrors")):
        k, eq, url = line.partition("=")
        if not eq or not re.match(r"^https?://", url.strip()):
            c.err(f"containerdRegistryConfigs '{line}' must be registry=https://mirror.")
            continue
        regs[k.strip()] = f'server = "https://{k.strip()}"\n\n[host."{url.strip()}"]\n' \
                          f'capabilities = ["pull", "resolve"]\n'
    if regs:
        cfg["containerdRegistryConfigs"] = regs
    tok = c.s("m8_cluster_token")
    if tok:
        if not re.match(r"^[A-Za-z0-9]{32,}$", tok):
            c.err("persistentClusterToken must be at least 32 alphanumeric characters.")
        cfg["persistentClusterToken"] = tok
    ju = c.s("m8_join_url")
    if ju:
        if not re.match(r"^[A-Za-z0-9.\-\[\]:]+:\d+/[A-Za-z0-9/]+$", ju):
            c.err("join.url must look like 10.0.0.10:25000/<token> (as printed by `microk8s add-node`).")
        cfg["join"] = {"url": ju, **({"worker": True} if c.b("m8_join_worker") else {})}
    elif c.b("m8_join_worker"):
        c.err("join.worker needs a join.url.")
    if ju and addons:
        c.warn("addons are cluster-wide: on a joining node they are normally left to the first node.")
    chan = c.s("m8_channel") or CHANNELS[0]
    if not re.match(r"^(latest|\d+\.\d+(-[a-z]+)?)/(stable|candidate|beta|edge)$", chan):
        c.err(f"snap channel '{chan}' must look like 1.36/stable.")
    header = ("# MicroK8s launch configuration\n"
              "# Before install: sudo mkdir -p /var/snap/microk8s/common && "
              "sudo cp microk8s-config.yaml /var/snap/microk8s/common/.microk8s.yaml\n"
              f"#                 sudo snap install microk8s --classic --channel={chan}\n"
              "# Existing node:  sudo snap set microk8s config=\"$(cat microk8s-config.yaml)\"\n")
    return ConfigResult(None, c.errors, c.warnings, "microk8s-config.yaml",
                        info=f"launch configuration v{cfg['version']}",
                        files=[OutFile("microk8s-config.yaml", header + "---\n" + to_yaml(cfg),
                                       0o600 if tok else None)])


def check_row(key, sc):
    pass
