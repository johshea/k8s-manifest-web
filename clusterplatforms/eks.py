"""
Amazon EKS: eksctl ClusterConfig (eksctl.io/v1alpha5) with managed node groups.
Validated against the JSON schema printed by `eksctl utils schema` (eksctl 0.231.0) - every key and
enum is checked offline. `eksctl create cluster --dry-run` is not used: it needs AWS credentials (STS).
"""
from __future__ import annotations

import re

from .common import (TABLE_FIELDS, Field, ConfigResult, OutFile, _cidrs, _csv, _lines, _parse_kv, _rows, _SubCtx,
                     required, table, to_yaml, TAINT_EFFECTS)

TYPE = "EKS"
GROUP = "Managed cloud"

REGIONS = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "ca-west-1", "eu-west-1", "eu-west-2",
           "eu-west-3", "eu-central-1", "eu-central-2", "eu-north-1", "eu-south-1", "eu-south-2", "ap-south-1",
           "ap-south-2", "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4", "ap-southeast-5",
           "ap-southeast-7", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3", "ap-east-1", "sa-east-1",
           "me-south-1", "me-central-1", "il-central-1", "af-south-1", "mx-central-1"]
VERSIONS = ["1.34", "1.35", "1.33", "1.32", "1.31"]
INSTANCE_TYPES = ["m7i.large", "m6i.large", "m5.large", "t3.medium", "t3.large", "c7i.xlarge", "r7i.large",
                  "m7g.large", "t4g.medium", "g5.xlarge"]
AMI_FAMILIES = ["AmazonLinux2023", "Bottlerocket", "Ubuntu2404", "UbuntuPro2404", "Ubuntu2204", "AmazonLinux2",
                "BottlerocketFips", "WindowsServer2022CoreContainer", "WindowsServer2025CoreContainer"]
VOLUME_TYPES = ["gp3", "gp2", "io1", "io2", "st1", "sc1"]
NAT = ["Single", "HighlyAvailable", "Disable"]
AUTH_MODES = ["API_AND_CONFIG_MAP", "API", "CONFIG_MAP"]
LOG_TYPES = ["api", "audit", "authenticator", "controllerManager", "scheduler", "all"]
ADDONS = ["vpc-cni", "coredns", "kube-proxy", "eks-pod-identity-agent", "aws-ebs-csi-driver", "aws-efs-csi-driver",
          "metrics-server", "amazon-cloudwatch-observability", "snapshot-controller", "eks-node-monitoring-agent"]

TABLE_FIELDS["eks_nodegroups"] = [
    Field("name", "name", default="ng-1", required=True),
    Field("instance_types", "instanceType(s)", "ecombo", default="m7i.large", options=INSTANCE_TYPES,
          help="one type, or comma-separated list (instanceTypes - good for spot)"),
    Field("min", "minSize", default="1"),
    Field("desired", "desiredCapacity", default="2"),
    Field("max", "maxSize", default="3"),
    Field("volume_size", "volumeSize (GiB)", default="80"),
    Field("volume_type", "volumeType", "combo", default="gp3", options=VOLUME_TYPES),
    Field("ami_family", "amiFamily", "combo", default="AmazonLinux2023", options=AMI_FAMILIES),
    Field("spot", "spot", "check", default=False),
    Field("private", "privateNetworking", "check", default=True),
    Field("labels", "labels", default="", help="key=value, comma-separated"),
    Field("taints", "taints", default="", help="key=value:Effect, comma-separated"),
]

SCHEMA = [
    ("Cluster", [
        Field("ek_name", "metadata.name", default="demo-eks", required=True),
        Field("ek_region", "metadata.region", "ecombo", default="us-west-2", options=REGIONS, required=True),
        Field("ek_version", "metadata.version", "ecombo", default=VERSIONS[0], options=VERSIONS,
              help="EKS minor version (eksctl 0.231 default 1.34)"),
        Field("ek_tags", "metadata.tags", default="", help="key=value, comma-separated (e.g. owner=john,env=dev)"),
        Field("ek_auto_mode", "autoModeConfig.enabled (EKS Auto Mode)", "check", default=False,
              help="AWS manages nodes (no managed node groups needed)"),
    ]),
    ("Managed node groups", [
        table("eks_nodegroups", "managedNodeGroups", ["name", "instance_types", "min", "desired", "max", "spot"],
              default=[{f.key: f.default for f in TABLE_FIELDS["eks_nodegroups"]}]),
    ]),
    ("VPC & networking", [
        Field("ek_vpc_cidr", "vpc.cidr", default="", help="blank = 192.168.0.0/16 (new VPC)"),
        Field("ek_nat", "vpc.nat.gateway", "combo", default="Single", options=NAT),
        Field("ek_azs", "availabilityZones", default="", help="blank = eksctl picks; e.g. us-west-2a,us-west-2b"),
        Field("ek_public", "endpoint publicAccess", "check", default=True),
        Field("ek_private", "endpoint privateAccess", "check", default=False),
        Field("ek_public_cidrs", "vpc.publicAccessCIDRs", default="", help="restrict the public endpoint"),
        Field("ek_ip_family", "kubernetesNetworkConfig.ipFamily", "combo", default="IPv4", options=["IPv4", "IPv6"]),
        Field("ek_service_cidr", "serviceIPv4CIDR", default=""),
    ]),
    ("Access, IAM & add-ons", [
        Field("ek_auth_mode", "accessConfig.authenticationMode", "combo", default="API_AND_CONFIG_MAP",
              options=AUTH_MODES),
        Field("ek_oidc", "iam.withOIDC (IRSA)", "check", default=True),
        Field("ek_addons", "addons", default="vpc-cni,coredns,kube-proxy,eks-pod-identity-agent",
              help="comma-separated EKS add-on names: " + ", ".join(ADDONS)),
        Field("ek_logging", "cloudWatch clusterLogging", default="", help="comma-separated: " + ", ".join(LOG_TYPES)),
    ]),
]
COLLAPSED = {"Access, IAM & add-ons"}


def check_row(key, sc):
    if key != "eks_nodegroups":
        return
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$", sc.s("name") or ""):
        sc.err("name must be 1-63 letters, digits or '-'.")
    types = _csv(sc.s("instance_types"))
    if not types:
        sc.err("at least one instance type is required.")
    for t in types:
        if not re.match(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$", t):
            sc.err(f"instance type '{t}' doesn't look like an EC2 type (e.g. m7i.large).")
    vals = [sc.int(k, k, lo=0, hi=1000) for k in ("min", "desired", "max")]
    if None not in vals and not vals[0] <= vals[1] <= vals[2]:
        sc.err("need minSize <= desiredCapacity <= maxSize.")
    sc.int("volume_size", "volumeSize", lo=4, hi=16384)
    _parse_kv(sc, sc.s("labels"), "labels", label_rules=True)
    for t in _csv(sc.s("taints")):
        if not re.match(r"^[^=:\s]+(=[^:\s]*)?:(NoSchedule|PreferNoSchedule|NoExecute)$", t):
            sc.err(f"taint '{t}' must be key[=value]:Effect.")
    am = sc.s("ami_family")
    if am.startswith("Windows") and any(t.startswith(("t4g", "m7g", "c7g", "r7g")) for t in types):
        sc.err("Windows AMIs need x86_64 instance types (no Graviton *g types).")


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    name = c.s("ek_name")
    if name and not re.match(r"^[a-zA-Z][a-zA-Z0-9-]{0,99}$", name):
        c.err("metadata.name must start with a letter and contain only letters, digits and '-' (max 100).")
    region = c.s("ek_region")
    if region and not re.match(r"^[a-z]{2}(-gov)?-[a-z]+-\d$", region):
        c.err(f"region '{region}' is not an AWS region name.")
    ver = c.s("ek_version")
    if ver and not re.match(r"^1\.\d{2}$", ver):
        c.err(f"version '{ver}' must be an EKS minor version like 1.34.")
    meta = {"name": name, "region": region}
    if ver:
        meta["version"] = ver
    tags = _parse_kv(c, c.s("ek_tags"), "tags")
    if tags:
        meta["tags"] = tags
    cfg: dict = {"apiVersion": "eksctl.io/v1alpha5", "kind": "ClusterConfig", "metadata": meta}
    cfg["iam"] = {"withOIDC": c.b("ek_oidc")}
    cfg["accessConfig"] = {"authenticationMode": c.s("ek_auth_mode") or "API_AND_CONFIG_MAP"}
    if c.s("ek_auth_mode") not in AUTH_MODES:
        c.err("authenticationMode must be API, API_AND_CONFIG_MAP or CONFIG_MAP.")
    vpc: dict = {}
    if c.s("ek_vpc_cidr"):
        nets = _cidrs(c, c.s("ek_vpc_cidr"), "vpc.cidr")
        if nets:
            if nets[0].prefixlen < 16 or nets[0].prefixlen > 24:
                c.err("vpc.cidr must be between /16 and /24.")
            vpc["cidr"] = str(nets[0])
    vpc["nat"] = {"gateway": c.s("ek_nat") or "Single"}
    pub, priv = c.b("ek_public"), c.b("ek_private")
    if not pub and not priv:
        c.err("enable at least one of endpoint publicAccess / privateAccess.")
    vpc["clusterEndpoints"] = {"publicAccess": pub, "privateAccess": priv}
    pcidrs = _csv(c.s("ek_public_cidrs"))
    for p in pcidrs:
        _cidrs(c, p, "publicAccessCIDRs")
    if pcidrs:
        if not pub:
            c.warn("publicAccessCIDRs has no effect with publicAccess disabled.")
        vpc["publicAccessCIDRs"] = pcidrs
    elif pub:
        c.warn("the public API endpoint is open to 0.0.0.0/0; consider publicAccessCIDRs or privateAccess.")
    cfg["vpc"] = vpc
    azs = _csv(c.s("ek_azs"))
    for z in azs:
        if region and not z.startswith(region):
            c.err(f"availability zone '{z}' is not in region {region}.")
    if azs:
        if len(azs) < 2:
            c.err("EKS needs at least 2 availability zones.")
        cfg["availabilityZones"] = azs
    knc: dict = {}
    fam = c.s("ek_ip_family") or "IPv4"
    if fam != "IPv4":
        knc["ipFamily"] = fam
        if not c.b("ek_oidc"):
            c.err("IPv6 clusters require iam.withOIDC: true.")
    if c.s("ek_service_cidr"):
        nets = _cidrs(c, c.s("ek_service_cidr"), "serviceIPv4CIDR")
        if nets:
            if not (nets[0].prefixlen >= 12 and nets[0].prefixlen <= 24) or not nets[0].is_private:
                c.err("serviceIPv4CIDR must be a private /12-/24 block.")
            knc["serviceIPv4CIDR"] = str(nets[0])
    if knc:
        cfg["kubernetesNetworkConfig"] = knc
    auto = c.b("ek_auto_mode")
    if auto:
        cfg["autoModeConfig"] = {"enabled": True}
    ngs = []
    names = set()
    for i, r in enumerate(_rows(c, "eks_nodegroups"), 1):
        sc = _SubCtx(c, r, f"node group #{i} ({r['name']}): ")
        check_row("eks_nodegroups", sc)
        if r["name"] in names:
            sc.err("duplicate node group name.")
        names.add(r["name"])
        types = _csv(r["instance_types"])
        ng: dict = {"name": r["name"], "amiFamily": r["ami_family"] or "AmazonLinux2023"}
        if len(types) == 1 and not r.get("spot"):
            ng["instanceType"] = types[0]
        elif types:
            ng["instanceTypes"] = types
            if r.get("spot") and len(types) == 1:
                sc.warn("spot with a single instance type risks capacity shortages; list 2+ similar types.")
        for k, yk in (("min", "minSize"), ("desired", "desiredCapacity"), ("max", "maxSize"),
                      ("volume_size", "volumeSize")):
            if str(r[k]).isdigit():
                ng[yk] = int(r[k])
        ng["volumeType"] = r["volume_type"] or "gp3"
        if r.get("spot"):
            ng["spot"] = True
        ng["privateNetworking"] = bool(r.get("private"))
        lab = _parse_kv(sc, r["labels"], "labels", label_rules=True)
        if lab:
            ng["labels"] = lab
        tl = []
        for t in _csv(r["taints"]):
            m = re.match(r"^([^=:\s]+)(?:=([^:\s]*))?:(\S+)$", t)
            if m and m.group(3) in TAINT_EFFECTS:
                tl.append({"key": m.group(1), **({"value": m.group(2)} if m.group(2) else {}), "effect": m.group(3)})
        if tl:
            ng["taints"] = tl
        ngs.append(ng)
    if ngs:
        cfg["managedNodeGroups"] = ngs
    elif not auto:
        c.warn("no node groups and Auto Mode off: the cluster will have no nodes to run pods.")
    if ngs and any(n["privateNetworking"] for n in ngs) and (c.s("ek_nat") == "Disable"):
        c.err("private node groups need a NAT gateway (vpc.nat.gateway Single or HighlyAvailable).")
    addons = _csv(c.s("ek_addons"))
    for a in addons:
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", a):
            c.err(f"addon '{a}' is not a valid add-on name.")
        elif a not in ADDONS:
            c.warn(f"addon '{a}' is not in the common EKS add-on list - check `eksctl utils describe-addon-versions`.")
    if auto and addons:
        c.warn("Auto Mode manages vpc-cni/coredns/kube-proxy itself; extra add-ons may be unnecessary.")
    if addons:
        cfg["addons"] = [{"name": a} for a in addons]
    logs = _csv(c.s("ek_logging"))
    bad = [x for x in logs if x not in LOG_TYPES]
    if bad:
        c.err(f"clusterLogging types must be among {', '.join(LOG_TYPES)} (got {', '.join(bad)}).")
    if logs:
        cfg["cloudWatch"] = {"clusterLogging": {"enableTypes": logs}}
    header = (f"# eksctl create cluster -f eksctl-{name or 'cluster'}.yaml   (needs AWS credentials; "
              "creates billable resources)\n# preview first: eksctl create cluster -f ... --dry-run\n")
    return ConfigResult([cfg], c.errors, c.warnings, f"eksctl-{name or 'cluster'}.yaml", header=header,
                        info="eksctl.io/v1alpha5 ClusterConfig")
