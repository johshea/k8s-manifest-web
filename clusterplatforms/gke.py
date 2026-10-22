"""
Google Kubernetes Engine: a `gcloud container clusters create` (Standard) or `create-auto` (Autopilot)
script. Chosen over Config Connector ContainerCluster YAML because it needs no existing management
cluster with Config Connector installed - it's the form most people actually run.
Flags and enum values are checked against `gcloud container clusters create[-auto] --help` (SDK 5xx).
"""
from __future__ import annotations

import re

from .common import (Field, ConfigResult, OutFile, _cidrs, _csv, _no_overlap, _parse_kv, required, sh_quote, shell_script)

TYPE = "GKE"
GROUP = "Managed cloud"

REGIONS = ["us-central1", "us-east1", "us-east4", "us-east5", "us-west1", "us-west2", "us-west3", "us-west4",
           "us-south1", "northamerica-northeast1", "northamerica-northeast2", "southamerica-east1", "europe-west1",
           "europe-west2", "europe-west3", "europe-west4", "europe-west6", "europe-west9", "europe-north1",
           "europe-central2", "asia-east1", "asia-east2", "asia-northeast1", "asia-northeast3", "asia-south1",
           "asia-southeast1", "australia-southeast1", "me-west1", "africa-south1"]
CHANNELS = ["regular", "rapid", "stable", "extended", "None"]
MACHINES = ["e2-standard-4", "e2-standard-2", "e2-medium", "n2-standard-4", "n2d-standard-4", "c3-standard-4",
            "t2a-standard-4", "n4-standard-4", "g2-standard-4"]
DISK_TYPES = ["pd-balanced", "pd-standard", "pd-ssd", "hyperdisk-balanced", "hyperdisk-extreme",
              "hyperdisk-throughput"]
IMAGE_TYPES = ["COS_CONTAINERD", "UBUNTU_CONTAINERD", "WINDOWS_LTSC_CONTAINERD"]
ADDONS = ["HttpLoadBalancing", "HorizontalPodAutoscaling", "KubernetesDashboard", "NetworkPolicy", "NodeLocalDNS",
          "ConfigConnector", "GcePersistentDiskCsiDriver", "GcpFilestoreCsiDriver", "BackupRestore",
          "GcsFuseCsiDriver", "HighScaleCheckpointing", "LustreCsiDriver", "RayOperator", "SlurmOperator",
          "NodeReadinessController", "CloudRun"]

SCHEMA = [
    ("Cluster", [
        Field("gk_mode", "mode", "combo", default="Standard", options=["Standard", "Autopilot"],
              help="Autopilot = `create-auto` (Google manages nodes; node-pool fields are ignored)"),
        Field("gk_name", "cluster name", default="demo-gke", required=True),
        Field("gk_project", "--project", default="my-project-id", required=True,
              help="placeholder - set to your project id"),
        Field("gk_location", "--location (region or zone)", "ecombo", default="us-central1", options=REGIONS,
              help="region = regional (HA control plane); zone like us-central1-a = zonal"),
        Field("gk_channel", "--release-channel", "combo", default="regular", options=CHANNELS),
        Field("gk_version", "--cluster-version", default="", help="blank = channel default, e.g. 1.34"),
        Field("gk_labels", "--labels", default="", help="key=value, comma-separated (lowercase)"),
    ]),
    ("Default node pool (Standard)", [
        Field("gk_machine", "--machine-type", "ecombo", default="e2-standard-4", options=MACHINES),
        Field("gk_nodes", "--num-nodes (per zone)", default="1"),
        Field("gk_node_locations", "--node-locations", default="", help="zones, comma-separated"),
        Field("gk_autoscale", "--enable-autoscaling", "check", default=True),
        Field("gk_min", "--min-nodes (per zone)", default="1"),
        Field("gk_max", "--max-nodes (per zone)", default="3"),
        Field("gk_disk_type", "--disk-type", "combo", default="pd-balanced", options=DISK_TYPES),
        Field("gk_disk_size", "--disk-size (GB)", default="100"),
        Field("gk_image", "--image-type", "combo", default="COS_CONTAINERD", options=IMAGE_TYPES),
        Field("gk_spot", "--spot", "check", default=False),
        Field("gk_node_labels", "--node-labels", default=""),
        Field("gk_node_taints", "--node-taints", default="", help="key=value:Effect, comma-separated"),
    ]),
    ("Networking", [
        Field("gk_network", "--network", default="", help="blank = default"),
        Field("gk_subnetwork", "--subnetwork", default=""),
        Field("gk_pod_cidr", "--cluster-ipv4-cidr", default="", help="VPC-native pod range, e.g. 10.4.0.0/14"),
        Field("gk_svc_cidr", "--services-ipv4-cidr", default="", help="e.g. 10.8.0.0/20"),
        Field("gk_dpv2", "--enable-dataplane-v2", "check", default=True),
        Field("gk_private", "--enable-private-nodes", "check", default=False),
        Field("gk_master_cidr", "--master-ipv4-cidr", default="172.16.0.0/28"),
        Field("gk_authorized", "--master-authorized-networks", default="", help="CIDRs allowed to reach the API"),
    ]),
    ("Security & add-ons", [
        Field("gk_workload_identity", "--workload-pool (Workload Identity)", "check", default=True),
        Field("gk_shielded", "--enable-shielded-nodes", "check", default=True),
        Field("gk_addons", "--addons (Standard)", default="", help="blank = default; " + ", ".join(ADDONS)),
    ]),
]
COLLAPSED = {"Networking", "Security & add-ons"}

_NAME = re.compile(r"^[a-z]([-a-z0-9]{0,38}[a-z0-9])?$")
_PROJECT = re.compile(r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]+-[a-z]+\d+$")
_ZONE = re.compile(r"^[a-z]+-[a-z]+\d+-[a-z]$")


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    auto = c.s("gk_mode") == "Autopilot"
    name, project, loc = c.s("gk_name"), c.s("gk_project"), c.s("gk_location")
    if name and not _NAME.match(name):
        c.err("cluster name must be lowercase letters, digits and '-', start with a letter, max 40 chars.")
    if project and not _PROJECT.match(project):
        c.err("--project must be a project ID (6-30 chars, lowercase letters, digits, '-').")
    if project in ("my-project-id",):
        c.warn("--project is still the placeholder 'my-project-id'.")
    zonal = bool(_ZONE.match(loc))
    if loc and not (zonal or _REGION.match(loc)):
        c.err(f"--location '{loc}' is not a GCP region (us-central1) or zone (us-central1-a).")
    if auto and zonal:
        c.err("Autopilot clusters are regional: use a region, not a zone.")
    flags: list = [("--project", project), ("--location", loc)]
    ch = c.s("gk_channel") or "regular"
    if ch not in CHANNELS:
        c.err(f"--release-channel must be one of {', '.join(CHANNELS)}.")
    if auto and ch == "None":
        c.err("Autopilot clusters must be enrolled in a release channel.")
    flags.append(("--release-channel", ch))
    ver = c.s("gk_version")
    if ver:
        if not re.match(r"^(latest|\d+\.\d+(\.\d+(-gke\.\d+)?)?)$", ver):
            c.err("--cluster-version must look like 1.34, 1.34.1 or 1.34.1-gke.1234 (or 'latest').")
        flags.append(("--cluster-version", ver))
    labels = _parse_kv(c, c.s("gk_labels"), "--labels")
    for k, v in labels.items():
        if not re.match(r"^[a-z][a-z0-9_-]{0,62}$", k) or not re.match(r"^[a-z0-9_-]{0,63}$", v):
            c.err(f"GCP label '{k}={v}' must be lowercase letters, digits, '_' or '-'.")
    if labels:
        flags.append(("--labels", ",".join(f"{k}={v}" for k, v in labels.items())))

    if not auto:
        flags.append(("--machine-type", c.s("gk_machine") or "e2-standard-4"))
        if not re.match(r"^[a-z0-9]+-[a-z0-9-]+$", c.s("gk_machine") or "e2-standard-4"):
            c.err("--machine-type doesn't look like a GCE machine type (e.g. e2-standard-4).")
        n = c.int("gk_nodes", "--num-nodes", lo=0, hi=1000, required=True)
        if n is not None:
            flags.append(("--num-nodes", str(n)))
            if n == 0:
                c.warn("--num-nodes 0: the default pool will be empty (system pods can't schedule).")
            if not zonal and n:
                c.warn(f"regional cluster: --num-nodes is per zone, so {n} x 3 zones = {n * 3} nodes by default.")
        zones = _csv(c.s("gk_node_locations"))
        for z in zones:
            if not _ZONE.match(z) or (loc and not z.startswith(loc.rsplit("-", 1)[0] if zonal else loc)):
                c.err(f"--node-locations zone '{z}' is not a zone of {loc}.")
        if zones:
            flags.append(("--node-locations", ",".join(zones)))
        if c.b("gk_autoscale"):
            lo = c.int("gk_min", "--min-nodes", lo=0, hi=1000, required=True)
            hi = c.int("gk_max", "--max-nodes", lo=1, hi=1000, required=True)
            if lo is not None and hi is not None:
                if lo > hi:
                    c.err("--min-nodes must be <= --max-nodes.")
                if n is not None and not lo <= n <= hi:
                    c.warn("--num-nodes is outside the autoscaling range.")
            flags += [("--enable-autoscaling", None), ("--min-nodes", c.s("gk_min")), ("--max-nodes", c.s("gk_max"))]
        dt = c.s("gk_disk_type") or "pd-balanced"
        if dt not in DISK_TYPES:
            c.err(f"--disk-type must be one of {', '.join(DISK_TYPES)}.")
        flags.append(("--disk-type", dt))
        ds = c.int("gk_disk_size", "--disk-size", lo=10, hi=65536)
        if ds is not None:
            flags.append(("--disk-size", str(ds)))
        img = c.s("gk_image") or "COS_CONTAINERD"
        if img.startswith("WINDOWS"):
            c.err("Windows images can't be used for the default node pool; add a Windows pool after creation.")
        flags.append(("--image-type", img))
        if c.b("gk_spot"):
            flags.append(("--spot", None))
        nl = _parse_kv(c, c.s("gk_node_labels"), "--node-labels", label_rules=True)
        if nl:
            flags.append(("--node-labels", ",".join(f"{k}={v}" for k, v in nl.items())))
        taints = _csv(c.s("gk_node_taints"))
        for t in taints:
            if not re.match(r"^[^=:\s]+=[^:\s]*:(NoSchedule|PreferNoSchedule|NoExecute)$", t):
                c.err(f"--node-taints '{t}' must be key=value:Effect.")
        if taints:
            flags.append(("--node-taints", ",".join(taints)))
        if c.b("gk_shielded"):
            flags.append(("--enable-shielded-nodes", None))
        addons = _csv(c.s("gk_addons"))
        for a in addons:
            if a.split("=")[0] not in ADDONS:
                c.err(f"--addons '{a}' is not a GKE add-on (one of: {', '.join(ADDONS)}).")
        if "NetworkPolicy" in addons and c.b("gk_dpv2"):
            c.err("the NetworkPolicy (Calico) add-on can't be combined with Dataplane V2 (which enforces policy itself).")
        if addons:
            flags.append(("--addons", ",".join(addons)))
        if c.b("gk_workload_identity"):
            flags.append(("--workload-pool", f"{project}.svc.id.goog"))
        if c.b("gk_dpv2"):
            flags.append(("--enable-dataplane-v2", None))
        flags.append(("--enable-ip-alias", None))
    for k, f in (("gk_network", "--network"), ("gk_subnetwork", "--subnetwork")):
        if c.s(k):
            if not re.match(r"^[a-z]([-a-z0-9]*[a-z0-9])?$|^projects/", c.s(k)):
                c.err(f"{f} '{c.s(k)}' is not a valid network name.")
            flags.append((f, c.s(k)))
    pods = _cidrs(c, c.s("gk_pod_cidr"), "--cluster-ipv4-cidr") if c.s("gk_pod_cidr") else []
    svcs = _cidrs(c, c.s("gk_svc_cidr"), "--services-ipv4-cidr") if c.s("gk_svc_cidr") else []
    for nets, f in ((pods, "--cluster-ipv4-cidr"), (svcs, "--services-ipv4-cidr")):
        for n in nets:
            if n.version != 4:
                c.err(f"{f} must be IPv4.")
        if nets:
            flags.append((f, str(nets[0])))
    if pods and pods[0].prefixlen > 21:
        c.err("--cluster-ipv4-cidr must be /21 or larger (GKE needs room for per-node /24 ranges).")
    _no_overlap(c, pods, svcs, "--cluster-ipv4-cidr", "--services-ipv4-cidr")
    if c.b("gk_private"):
        flags.append(("--enable-private-nodes", None))
        m = _cidrs(c, c.s("gk_master_cidr"), "--master-ipv4-cidr") if c.s("gk_master_cidr") else []
        if m:
            if m[0].prefixlen != 28 or not m[0].is_private:
                c.err("--master-ipv4-cidr must be a private /28 (e.g. 172.16.0.0/28).")
            _no_overlap(c, m, pods + svcs, "--master-ipv4-cidr", "pod/service range")
            flags.append(("--master-ipv4-cidr", str(m[0])))
    auth = _csv(c.s("gk_authorized"))
    for a in auth:
        _cidrs(c, a, "--master-authorized-networks")
    if auth:
        flags += [("--enable-master-authorized-networks", None), ("--master-authorized-networks", ",".join(auth))]
    elif not c.b("gk_private"):
        c.warn("the control plane endpoint is reachable from any IP; consider --master-authorized-networks.")
    sub = "create-auto" if auto else "create"
    cmd = ['"${GCLOUD:-gcloud}"', "container", "clusters", sub, name]
    comments = ["Generated by k8s-manifest-gui. Needs `gcloud auth login` + billing; creates billable resources.",
                "Extra flags are passed through, e.g.:  ./create-gke.sh --async"]
    post = ["", f'"${{GCLOUD:-gcloud}}" container clusters get-credentials {sh_quote(name)} --location {sh_quote(loc)} --project {sh_quote(project)}']
    script = shell_script(f"GKE {'Autopilot' if auto else 'Standard'} cluster '{name}'", comments, cmd, flags,
                          post=post)
    res = ConfigResult([], c.errors, c.warnings, f"create-gke-{name or 'cluster'}.sh",
                       info=f"gcloud container clusters {sub}")
    res.files = [OutFile(res.filename, script, 0o755)]
    return res
