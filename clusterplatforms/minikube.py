"""
minikube: persistent config (~/.minikube/config/config.json) and/or an equivalent
`minikube start ...` script (start-minikube.sh).

Verified against minikube v1.39.0:
  * config.json keys = the "Configurable fields" of `minikube config --help` that a start
    form needs (driver, container-runtime, cpus, memory, disk-size, kubernetes-version,
    profile, feature-gates, insecure-registry). minikube stores every value as a string.
    nodes / cni / addons / HA / extra-config etc. are NOT config keys - they are start flags.
  * start flags = `minikube start --help`; drivers = `minikube config defaults driver`
    (+ macOS/Windows-only drivers); addons = `minikube addons list`.
"""
from __future__ import annotations

import ipaddress
import re

from .common import (Field, ConfigResult, OutFile, _cidrs, _csv, _lines, _port, _san, did_you_mean, required,
                     shell_script, to_json)

TYPE = "minikube"
GROUP = "Local"

OUTPUTS = ["both (config.json + start-minikube.sh)", "config.json", "start script (start-minikube.sh)"]
DRIVERS = ["docker", "podman", "kvm2", "qemu2", "virtualbox", "vfkit", "hyperkit", "hyperv", "vmware", "none",
           "ssh", ""]
DRIVER_OS = {"kvm2": "Linux", "none": "Linux", "vfkit": "macOS", "hyperkit": "macOS (Intel)", "hyperv": "Windows"}
KIC_DRIVERS = {"docker", "podman"}
RUNTIMES = ["", "containerd", "docker", "cri-o"]
CNIS = ["", "auto", "bridge", "calico", "cilium", "flannel", "kindnet", "false"]
K8S_VERSIONS = ["stable", "latest", "v1.37.0", "v1.36.4", "v1.35.8", "v1.34.9"]
ADDONS = ["ambassador", "amd-gpu-device-plugin", "auto-pause", "cloud-spanner", "csi-hostpath-driver", "dashboard",
          "default-storageclass", "efk", "freshpod", "gcp-auth", "gvisor", "headlamp", "inaccel", "ingress",
          "ingress-dns", "inspektor-gadget", "istio", "istio-provisioner", "kong", "kubeflow", "kubetail", "kubevirt",
          "logviewer", "metallb", "metrics-server", "nvidia-device-plugin", "nvidia-driver-installer",
          "nvidia-gpu-device-plugin", "olm", "pod-security-policy", "portainer", "registry", "registry-aliases",
          "registry-creds", "storage-provisioner", "storage-provisioner-rancher", "traefik", "volcano",
          "volumesnapshots", "yakd"]
EXTRA_COMPONENTS = ("kubelet", "kubeadm", "apiserver", "controller-manager", "etcd", "proxy", "scheduler")
WAIT = ["", "apiserver,system_pods", "all", "none", "apiserver,system_pods,default_sa,apps_running,node_ready,kubelet"]
# keys accepted by `minikube config set` that this form writes to config.json
CONFIG_KEYS = ["driver", "container-runtime", "cpus", "memory", "disk-size", "kubernetes-version", "profile",
               "feature-gates", "insecure-registry"]
CONFIG_PATH = "~/.minikube/config/config.json"

TABLES: dict = {}
SCHEMA = [
    ("Output", [
        Field("mk_output", "Generate", "combo", default=OUTPUTS[0], options=OUTPUTS,
              help=f"config.json = minikube's persistent defaults ({CONFIG_PATH}, read by every `minikube start`); "
                   "start-minikube.sh = the same settings as flags plus multi-node / CNI / addons, which "
                   "config.json cannot hold"),
    ]),
    ("Machine", [
        Field("mk_profile", "profile", default="minikube", required=True,
              help="cluster/profile name (kube context name); `minikube -p <name> ...`"),
        Field("mk_driver", "driver", "combo", default="docker", options=DRIVERS,
              help="blank = auto-detect. kvm2/none: Linux only; vfkit/hyperkit: macOS; hyperv: Windows"),
        Field("mk_cpus", "cpus", "ecombo", default="2", options=["2", "4", "6", "8", "max", "no-limit"],
              help=">= 2; 'no-limit' only with docker/podman"),
        Field("mk_memory", "memory", "ecombo", default="4096",
              options=["2200", "4096", "6g", "8g", "16g", "max", "no-limit"],
              help="MB, or <n>[b|k|m|g]; >= 1800mb; 'no-limit' only with docker/podman"),
        Field("mk_disk_size", "disk-size", "ecombo", default="20000mb", options=["20000mb", "20g", "40g", "80g"],
              help="<n>[b|k|m|g]; ignored by docker/podman (uses the host filesystem)"),
        Field("mk_nodes", "nodes", default="1", help="total nodes (start flag only)"),
        Field("mk_ha", "ha (3+ control-plane nodes)", "check", default=False),
    ]),
    ("Kubernetes", [
        Field("mk_k8s_version", "kubernetes-version", "ecombo", default="stable", options=K8S_VERSIONS,
              help="stable / latest / vX.Y.Z (`minikube config defaults kubernetes-version`)"),
        Field("mk_runtime", "container-runtime", "combo", default="", options=RUNTIMES,
              help="blank = auto (docker on most drivers)"),
        Field("mk_cni", "cni", "ecombo", default="", options=CNIS,
              help="blank = auto (kindnet for multi-node); or a path to a CNI manifest (start flag only)"),
        Field("mk_feature_gates", "feature-gates", default="", help="Name=true|false, comma-separated"),
        Field("mk_extra_config", "extra-config", "text", default="", height=3,
              help="component.key=value per line; components: " + ", ".join(EXTRA_COMPONENTS)),
        Field("mk_apiserver_names", "apiserver-names", default="", help="extra API server cert SANs, comma-separated"),
        Field("mk_apiserver_port", "apiserver-port", default="", help="blank = 8443"),
        Field("mk_service_cidr", "service-cluster-ip-range", default="", help="blank = 10.96.0.0/12"),
        Field("mk_dns_domain", "dns-domain", default="", help="blank = cluster.local"),
    ]),
    ("Addons", [
        Field("mk_addons", "addons", "text", default="", height=3,
              help="one per line or comma-separated, enabled at start (`minikube addons list`), e.g. "
                   "ingress, metrics-server, dashboard, registry, csi-hostpath-driver"),
    ]),
    ("Networking & registries", [
        Field("mk_ports", "ports", default="", help="docker/podman only; e.g. 80:80,443:443 or 30080"),
        Field("mk_listen_address", "listen-address", "ecombo", default="", options=["", "127.0.0.1", "0.0.0.0"],
              help="docker/podman only"),
        Field("mk_static_ip", "static-ip", default="", help="docker/podman only; private IPv4, last octet 2-254"),
        Field("mk_subnet", "subnet", default="", help="docker/podman only; e.g. 192.168.58.0/24"),
        Field("mk_network", "network", default="", help="existing docker/podman/kvm/qemu network name"),
        Field("mk_mount_string", "mount-string", default="", help="/host/path:/guest/path"),
        Field("mk_insecure_registry", "insecure-registry", default="", help="comma-separated host[:port] or CIDR"),
        Field("mk_registry_mirror", "registry-mirror", default="", help="comma-separated URLs (start flag only)"),
        Field("mk_image_mirror_country", "image-mirror-country", default="", help="e.g. cn (start flag only)"),
    ]),
    ("Start options", [
        Field("mk_wait", "wait", "ecombo", default="", options=WAIT, help="blank = apiserver,system_pods"),
        Field("mk_keep_context", "keep-context", "check", default=False),
        Field("mk_embed_certs", "embed-certs (kubeconfig)", "check", default=False),
        Field("mk_delete_on_failure", "delete-on-failure", "check", default=False),
    ]),
]
COLLAPSED = {"Networking & registries", "Start options"}

_SIZE = re.compile(r"^(\d+)([bkmg]?)b?$", re.I)
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _size_mb(v: str):
    m = _SIZE.match(v.strip())
    if not m:
        return None
    n, u = int(m.group(1)), (m.group(2) or "m").lower()
    return {"b": n / 1024 / 1024, "k": n / 1024, "m": n, "g": n * 1024}[u]


def _addons(c):
    out = []
    for line in _lines(c.s("mk_addons")):
        for a in _csv(line):
            if a not in ADDONS:
                c.err(f"addons: unknown addon '{a}'.{did_you_mean(a, ADDONS)} (see `minikube addons list`)")
            elif a in out:
                c.warn(f"addons: '{a}' listed twice.")
            else:
                out.append(a)
    if "gcp-auth" in out:
        c.warn("gcp-auth mounts your GCP credentials into pods; it needs `gcloud auth application-default login`.")
    if "ingress-dns" in out and "ingress" not in out:
        c.warn("ingress-dns is normally used together with the ingress addon.")
    return out


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    out_mode = c.s("mk_output") or OUTPUTS[0]
    want_json = out_mode != OUTPUTS[2]
    want_sh = out_mode != OUTPUTS[1]
    cfg: dict[str, str] = {}
    flags: list = []
    flag_only: list[str] = []

    def both(key, val, flag_val=None):
        cfg[key] = val
        flags.append((f"--{key}", val if flag_val is None else flag_val))

    def flag(name, val=None, label=None):
        flags.append((f"--{name}", val))
        flag_only.append(label or name)

    prof = c.s("mk_profile")
    if prof and not _PROFILE.match(prof):
        c.err(f"profile '{prof}' may only contain letters, digits, '.', '_' and '-' and must start with a "
              "letter or digit.")
    elif prof and prof != prof.lower():
        c.warn("profile has upper-case letters; the kube context and docker container names will too.")

    drv = c.s("mk_driver")
    if drv and drv not in DRIVERS:
        c.err(f"driver '{drv}' is not a minikube driver.{did_you_mean(drv, DRIVERS)}")
    if drv in DRIVER_OS:
        c.warn(f"driver {drv} only works on {DRIVER_OS[drv]}.")
    if drv == "hyperkit":
        c.warn("hyperkit is deprecated (Intel Macs only); prefer vfkit or docker on macOS.")
    if drv == "none":
        c.warn("driver none runs Kubernetes directly on this host as root (Linux, no isolation); "
               "it needs a container runtime + cri-dockerd/containerd and conntrack installed.")
    kic = drv in KIC_DRIVERS or drv == ""

    cpus = c.s("mk_cpus")
    if cpus:
        if cpus in ("max", "no-limit"):
            if cpus == "no-limit" and drv not in KIC_DRIVERS:
                c.err("cpus 'no-limit' only works with the docker or podman driver.")
        elif not cpus.isdigit():
            c.err(f"cpus '{cpus}' must be a whole number, 'max' or 'no-limit'.")
        elif int(cpus) < 2:
            c.err(f"cpus {cpus}: minikube requires at least 2 CPUs.")
    mem = c.s("mk_memory")
    if mem:
        if mem in ("max", "no-limit"):
            if mem == "no-limit" and drv not in KIC_DRIVERS:
                c.err("memory 'no-limit' only works with the docker or podman driver.")
        else:
            mb = _size_mb(mem)
            if mb is None:
                c.err(f"memory '{mem}' must be <number>[b|k|m|g] (plain number = MB), 'max' or 'no-limit'.")
            elif mb < 1800:
                c.err(f"memory {mem} is below minikube's minimum of 1800MB.")
            elif mb < 2200:
                c.warn(f"memory {mem}: minikube recommends at least 2200MB.")
    disk = c.s("mk_disk_size")
    if disk:
        mb = _size_mb(disk)
        if mb is None:
            c.err(f"disk-size '{disk}' must be <number>[b|k|m|g], e.g. 20000mb or 40g.")
        elif mb < 2000:
            c.err(f"disk-size {disk} is below minikube's minimum of 2000MB.")
        elif drv in KIC_DRIVERS:
            c.warn("disk-size is ignored by the docker/podman drivers.")

    nodes = c.int("mk_nodes", "nodes", lo=1, hi=100)
    ha = c.b("mk_ha")
    if ha and nodes is not None and 1 < nodes < 3:
        c.err("ha needs at least 3 nodes (3 control-plane nodes); set nodes to 3 or more.")
    if (ha or (nodes or 1) > 1) and drv == "none":
        c.err("driver none supports a single node only (no ha / multi-node).")

    ver = c.s("mk_k8s_version")
    if ver and ver not in ("stable", "latest") and not re.match(r"^v?\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$", ver):
        c.err(f"kubernetes-version '{ver}' must be stable, latest or vX.Y.Z (e.g. v1.37.0).")
    elif ver and ver[0].isdigit():
        c.warn(f"kubernetes-version '{ver}': minikube expects a leading 'v' (v{ver}).")
        ver = "v" + ver

    rt = c.s("mk_runtime")
    if rt and rt not in RUNTIMES:
        c.err(f"container-runtime '{rt}' must be containerd, docker or cri-o.")
    if rt == "docker" and drv == "podman":
        c.warn("the podman driver is meant for containerd or cri-o; the docker runtime may not work.")

    # ---- config.json + flags (same keys) -----------------------------------------------------
    if prof:
        flags.append(("--profile", prof))
        if prof != "minikube":
            cfg["profile"] = prof
    if drv:
        both("driver", drv)
    if cpus:
        both("cpus", cpus)
    if mem:
        both("memory", mem)
    if disk:
        both("disk-size", disk)
    if ver:
        both("kubernetes-version", ver)
    if rt:
        both("container-runtime", rt)
    fg = c.s("mk_feature_gates").replace(" ", "")
    if fg:
        for item in _csv(fg):
            m = re.match(r"^([A-Z][A-Za-z0-9]+)=(true|false)$", item)
            if not m:
                c.err(f"feature-gates '{item}' must be Name=true|false.")
        both("feature-gates", fg)
    ireg = _csv(c.s("mk_insecure_registry"))
    for r in ireg:
        if not re.match(r"^[A-Za-z0-9.\-]+(:\d+)?$", r) and not _is_cidr(r):
            c.err(f"insecure-registry '{r}' must be host[:port] or a CIDR.")
    if ireg:
        both("insecure-registry", ",".join(ireg))

    # ---- start-flag-only settings ----------------------------------------------------------
    if nodes and nodes > 1:
        flag("nodes", str(nodes))
    if ha:
        flag("ha")
    cni = c.s("mk_cni")
    if cni:
        if cni not in CNIS and "/" not in cni and not cni.endswith((".yaml", ".yml")):
            c.err(f"cni '{cni}' must be one of {', '.join(x for x in CNIS if x)} or a path to a CNI manifest.")
        if cni == "false" and ((nodes or 1) > 1 or ha):
            c.err("cni false (no CNI) cannot be used with multiple nodes.")
        flag("cni", cni)
    extra = []
    for line in _lines(c.s("mk_extra_config")):
        m = re.match(r"^([a-z-]+)\.([A-Za-z0-9_.\-]+)=(.*)$", line)
        if not m:
            c.err(f"extra-config '{line}' must be component.key=value.")
            continue
        if m.group(1) not in EXTRA_COMPONENTS:
            c.err(f"extra-config '{line}': component must be one of {', '.join(EXTRA_COMPONENTS)}.")
        extra.append(line)
    for e in extra:
        flags.append(("--extra-config", e))
    if extra:
        flag_only.append("extra-config")
    names = _csv(c.s("mk_apiserver_names"))
    for n in names:
        _san(c, n, "apiserver-names")
    if names:
        flag("apiserver-names", ",".join(names))
    p = _port(c, c.s("mk_apiserver_port"), "apiserver-port")
    if p:
        flag("apiserver-port", str(p))
    if c.s("mk_service_cidr"):
        nets = _cidrs(c, c.s("mk_service_cidr"), "service-cluster-ip-range")
        if nets:
            flag("service-cluster-ip-range", str(nets[0]))
    dd = c.s("mk_dns_domain")
    if dd:
        if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$", dd):
            c.err(f"dns-domain '{dd}' is not a valid DNS domain.")
        flag("dns-domain", dd)
    addons = _addons(c)
    if addons:
        flag("addons", ",".join(addons))

    kic_only = {"mk_ports": "ports", "mk_listen_address": "listen-address", "mk_static_ip": "static-ip",
                "mk_subnet": "subnet"}
    for key, name in kic_only.items():
        if c.s(key) and not kic:
            c.err(f"{name} only works with the docker or podman driver.")
    ports = _csv(c.s("mk_ports"))
    for pt in ports:
        if not re.match(r"^((\d{1,3}\.){3}\d{1,3}:)?(\d+)(:\d+)?(/(tcp|udp))?$", pt):
            c.err(f"ports '{pt}' must look like 8080, 8080:80 or 127.0.0.1:8080:80.")
    if ports:
        flag("ports", ",".join(ports))
    la = c.s("mk_listen_address")
    if la:
        try:
            ipaddress.ip_address(la)
        except ValueError:
            c.err(f"listen-address '{la}' is not an IP address.")
        flag("listen-address", la)
    sip = c.s("mk_static_ip")
    if sip:
        try:
            ip = ipaddress.ip_address(sip)
            if ip.version != 4 or not ip.is_private or not 2 <= int(sip.split(".")[-1]) <= 254:
                c.err(f"static-ip {sip} must be a private IPv4 address with last octet 2-254.")
        except ValueError:
            c.err(f"static-ip '{sip}' is not an IPv4 address.")
        if (nodes or 1) > 1 or ha:
            c.err("static-ip cannot be combined with multiple nodes.")
        flag("static-ip", sip)
    sub = c.s("mk_subnet")
    if sub:
        nets = _cidrs(c, sub, "subnet")
        if sip and nets:
            try:
                if ipaddress.ip_address(sip) not in nets[0]:
                    c.err(f"static-ip {sip} is not inside subnet {nets[0]}.")
            except ValueError:
                pass
        flag("subnet", sub)
    if c.s("mk_network"):
        flag("network", c.s("mk_network"))
    ms = c.s("mk_mount_string")
    if ms:
        if not re.match(r"^[^:]+:/[^:]*$", ms) and not re.match(r"^[A-Za-z]:\\[^:]*:/", ms):
            c.err(f"mount-string '{ms}' must be /host/path:/guest/path.")
        flag("mount-string", ms)
    rm = _csv(c.s("mk_registry_mirror"))
    for u in rm:
        if not re.match(r"^https?://", u):
            c.err(f"registry-mirror '{u}' must be an http(s):// URL.")
    if rm:
        flag("registry-mirror", ",".join(rm))
    imc = c.s("mk_image_mirror_country")
    if imc:
        if not re.match(r"^[a-z]{2}$", imc):
            c.err("image-mirror-country must be a 2-letter country code, e.g. cn.")
        flag("image-mirror-country", imc)
    w = c.s("mk_wait")
    if w:
        okw = {"apiserver", "system_pods", "default_sa", "apps_running", "node_ready", "kubelet", "extra", "all",
               "none", "true", "false"}
        bad = [x for x in _csv(w) if x not in okw]
        if bad:
            c.err(f"wait: unknown component(s) {', '.join(bad)}.")
        flag("wait", w)
    for key, name in (("mk_keep_context", "keep-context"), ("mk_embed_certs", "embed-certs"),
                      ("mk_delete_on_failure", "delete-on-failure")):
        if c.b(key):
            flag(name)

    if want_json and not want_sh and flag_only:
        c.warn("config.json cannot hold " + ", ".join(sorted(set(flag_only))) +
               " - they are `minikube start` flags only; choose 'both' or 'start script' to keep them.")

    files = []
    if want_json:
        files.append(OutFile("config.json", to_json(cfg)))
    if want_sh:
        title = f"minikube start for profile '{prof or 'minikube'}' - generated by k8s-manifest-gui"
        comments = ["Usage: ./start-minikube.sh [more minikube start flags]   e.g. --dry-run to only validate",
                    f"Delete with: minikube delete -p {prof or 'minikube'}"]
        if want_json:
            comments.insert(1, f"config.json (same defaults) goes to {CONFIG_PATH} "
                               "(or $MINIKUBE_HOME/.minikube/config/config.json)")
        files.append(OutFile("start-minikube.sh",
                             shell_script(title, comments, ['"${MINIKUBE:-minikube}"', "start"], flags), 0o755))
    info = f"minikube v1.39 · {'config.json + ' if want_json else ''}{'start script' if want_sh else ''}".rstrip(" +")
    return ConfigResult(None, c.errors, c.warnings, files[0].name if files else "config.json", info=info,
                        files=files)


def _is_cidr(v):
    try:
        ipaddress.ip_network(v, strict=False)
        return "/" in v
    except ValueError:
        return False


def check_row(key, sc):
    pass
