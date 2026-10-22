"""
OpenShift: install-config.yaml (apiVersion v1) for `openshift-install` 4.x.
Validated with `openshift-install create manifests` (platform none, offline) and the installer's
InstallConfig CRD schema (all platforms).
"""
from __future__ import annotations

import ipaddress
import json
import re

from .common import (TABLE_FIELDS, Field, ConfigResult, OutFile, _cidrs, _csv, _ip, _no_overlap, _rows, _SubCtx,
                     _DNS1123_SUB, required, table, to_yaml)

TYPE = "OpenShift"
GROUP = "Self-managed"

PLATFORMS = ["none", "aws", "azure", "gcp", "vsphere", "baremetal"]
AWS_REGIONS = ["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1", "ap-southeast-1",
               "ap-northeast-1", "ca-central-1"]
AZURE_CLOUDS = ["AzurePublicCloud", "AzureUSGovernmentCloud", "AzureChinaCloud", "AzureStackCloud"]
FAKE_PULL_SECRET = '{"auths":{"fake.example.com":{"auth":"Zm9vOmJhcg=="}}}'

TABLE_FIELDS["ocp_bm_hosts"] = [
    Field("name", "name", default="master-0", required=True),
    Field("role", "role", "combo", default="master", options=["master", "worker"]),
    Field("bmc_address", "bmc.address", default="redfish-virtualmedia://192.168.111.1:8000/redfish/v1/Systems/1",
          help="ipmi://, redfish://, redfish-virtualmedia://, idrac-virtualmedia://, ..."),
    Field("bmc_user", "bmc.username", default="admin"),
    Field("bmc_password", "bmc.password", default=""),
    Field("boot_mac", "bootMACAddress", default="52:54:00:00:00:01"),
    Field("insecure", "bmc.disableCertificateVerification", "check", default=False),
]

SCHEMA = [
    ("Cluster", [
        Field("oc_name", "metadata.name", default="ocp", required=True, help="cluster name; API = api.<name>.<baseDomain>"),
        Field("oc_base_domain", "baseDomain", default="example.com", required=True),
        Field("oc_platform", "platform", "combo", default="none", options=PLATFORMS,
              help="none = UPI / bring-your-own infra (the only one testable offline)"),
        Field("oc_cp_replicas", "controlPlane.replicas", "combo", default="3", options=["3", "1", "4", "5"],
              help="1 = single-node OpenShift (platform none, 0 workers)"),
        Field("oc_workers", "compute[worker].replicas", default="3"),
        Field("oc_hyperthreading", "hyperthreading", "combo", default="Enabled", options=["Enabled", "Disabled"]),
        Field("oc_fips", "fips", "check", default=False),
        Field("oc_publish", "publish", "combo", default="External", options=["External", "Internal"]),
    ]),
    ("Credentials", [
        Field("oc_pull_secret", "pullSecret (JSON)", "text", default=FAKE_PULL_SECRET, height=3,
              help="from console.redhat.com/openshift/install/pull-secret; the default is a fake placeholder"),
        Field("oc_ssh_key", "sshKey (public)", "text", default="", height=2),
    ]),
    ("Networking", [
        Field("oc_network_type", "networking.networkType", "combo", default="OVNKubernetes", options=["OVNKubernetes"]),
        Field("oc_cluster_net", "clusterNetwork cidr", default="10.128.0.0/14"),
        Field("oc_host_prefix", "clusterNetwork hostPrefix", default="23"),
        Field("oc_service_net", "serviceNetwork", default="172.30.0.0/16"),
        Field("oc_machine_net", "machineNetwork cidr", default="10.0.0.0/16", help="the subnet your nodes live in"),
    ]),
    ("Platform: aws / azure / gcp", [
        Field("oc_region", "region", "ecombo", default="us-east-1", options=AWS_REGIONS,
              help="aws/azure/gcp region (azure e.g. eastus, gcp e.g. us-central1)"),
        Field("oc_instance_type", "compute instance type", default="", help="aws m6i.xlarge / azure Standard_D4s_v5 / gcp n2-standard-4"),
        Field("oc_azure_rg", "azure baseDomainResourceGroupName", default=""),
        Field("oc_azure_cloud", "azure cloudName", "combo", default="AzurePublicCloud", options=AZURE_CLOUDS),
        Field("oc_gcp_project", "gcp projectID", default=""),
    ]),
    ("Platform: vsphere", [
        Field("oc_vc_server", "vCenter server", default="vcenter.example.com"),
        Field("oc_vc_user", "vCenter user", default="administrator@vsphere.local"),
        Field("oc_vc_password", "vCenter password", default=""),
        Field("oc_vc_datacenter", "datacenter", default="dc1"),
        Field("oc_vc_cluster", "computeCluster", default="cluster1", help="name or /dc/host/cluster path"),
        Field("oc_vc_datastore", "datastore", default="datastore1"),
        Field("oc_vc_network", "network (port group)", default="VM Network"),
    ]),
    ("Platform: baremetal / vsphere VIPs", [
        Field("oc_api_vip", "apiVIPs", default="10.0.0.5"),
        Field("oc_ingress_vip", "ingressVIPs", default="10.0.0.6"),
        Field("oc_provisioning", "baremetal provisioningNetwork", "combo", default="Managed",
              options=["Managed", "Unmanaged", "Disabled"]),
        table("ocp_bm_hosts", "baremetal hosts", ["name", "role", "bmc_address", "boot_mac"],
              default=[{**{f.key: f.default for f in TABLE_FIELDS["ocp_bm_hosts"]}, "name": f"master-{i}",
                        "boot_mac": f"52:54:00:00:00:0{i + 1}"} for i in range(3)]),
    ]),
]
COLLAPSED = {"Networking", "Platform: aws / azure / gcp", "Platform: vsphere", "Platform: baremetal / vsphere VIPs"}

_MAC = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_BMC = re.compile(r"^(ipmi|redfish|redfish-virtualmedia|redfish-uefihttp|idrac|idrac-redfish|idrac-virtualmedia|"
                  r"ilo4|ilo4-virtualmedia|ilo5|ilo5-redfish|ilo5-virtualmedia|irmc|ibmc)(\+https?)?://\S+$")


def check_row(key, sc):
    if key != "ocp_bm_hosts":
        return
    if not _DNS1123_SUB.match(sc.s("name") or ""):
        sc.err("name must be a lowercase DNS name.")
    if not _BMC.match(sc.s("bmc_address")):
        sc.err("bmc.address must be like ipmi://10.0.0.1 or redfish-virtualmedia://host/redfish/v1/Systems/1.")
    if not _MAC.match(sc.s("boot_mac")):
        sc.err("bootMACAddress must be a MAC like 52:54:00:00:00:01.")
    if not sc.s("bmc_user"):
        sc.err("bmc.username is required.")
    if not sc.s("bmc_password"):
        sc.warn("bmc.password is empty.")


def _pull_secret(c, text):
    try:
        ps = json.loads(text)
    except ValueError as exc:
        c.err(f"pullSecret is not valid JSON ({exc}).")
        return
    if not isinstance(ps, dict) or not isinstance(ps.get("auths"), dict) or not ps["auths"]:
        c.err('pullSecret must be JSON like {"auths": {"registry": {"auth": "..."}}}.')
        return
    for reg, ent in ps["auths"].items():
        if not isinstance(ent, dict) or not (ent.get("auth") or ent.get("identitytoken")):
            c.err(f"pullSecret entry '{reg}' needs an 'auth' value.")
    if "fake.example.com" in ps["auths"]:
        c.warn("pullSecret is the fake placeholder; real installs need yours from console.redhat.com.")
    elif "cloud.openshift.com" not in ps["auths"] and "quay.io" not in ps["auths"]:
        c.warn("pullSecret has no quay.io / cloud.openshift.com entry; release images may not pull.")


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    name, bd, plat = c.s("oc_name"), c.s("oc_base_domain"), c.s("oc_platform") or "none"
    if name and not re.match(r"^[a-z0-9]([-a-z0-9]{0,52}[a-z0-9])?$", name):
        c.err("metadata.name must be a DNS label (lowercase, digits, '-'), max 54 chars.")
    if bd and (not _DNS1123_SUB.match(bd) or "." not in bd):
        c.err("baseDomain must be a DNS domain like example.com.")
    if plat not in PLATFORMS:
        c.err(f"platform must be one of {', '.join(PLATFORMS)}.")
    cp = c.int("oc_cp_replicas", "controlPlane.replicas", lo=1, hi=5, required=True)
    if cp == 2:
        c.err("controlPlane.replicas must be 1, 3, 4 or 5 (2 can't keep etcd quorum; use 2+arbiter separately).")
    workers = c.int("oc_workers", "compute replicas", lo=0, hi=500, required=True)
    if cp == 1:
        if workers:
            c.err("single-node OpenShift (1 control-plane) requires compute replicas 0.")
        if plat not in ("none", "baremetal", "aws", "gcp", "azure"):
            c.err(f"single-node OpenShift isn't supported on platform {plat}.")
    if cp and cp > 1 and workers == 1:
        c.warn("1 worker: ingress routers need 2 schedulable nodes; use 0 (compact) or 2+.")
    ht = c.s("oc_hyperthreading") or "Enabled"
    cfg: dict = {"apiVersion": "v1", "baseDomain": bd, "metadata": {"name": name}}
    compute = {"name": "worker", "architecture": "amd64", "hyperthreading": ht, "platform": {},
               "replicas": workers or 0}
    ctl = {"name": "master", "architecture": "amd64", "hyperthreading": ht, "platform": {}, "replicas": cp or 3}
    itype = c.s("oc_instance_type")
    if itype and plat in ("aws", "azure", "gcp"):
        key = {"aws": "type", "azure": "type", "gcp": "type"}[plat]
        compute["platform"] = {plat: {key: itype}}
        ctl["platform"] = {plat: {key: itype}}
    cfg["compute"] = [compute]
    cfg["controlPlane"] = ctl

    hp = c.int("oc_host_prefix", "hostPrefix", lo=1, hi=128, required=True)
    cn = _cidrs(c, c.s("oc_cluster_net"), "clusterNetwork")
    sn = _cidrs(c, c.s("oc_service_net"), "serviceNetwork")
    mn = _cidrs(c, c.s("oc_machine_net"), "machineNetwork")
    if not cn or not sn or not mn:
        c.err("clusterNetwork, serviceNetwork and machineNetwork are all required.")
    if cn and hp is not None:
        if hp <= cn[0].prefixlen or hp > (30 if cn[0].version == 4 else 126):
            c.err(f"hostPrefix {hp} must be longer than the clusterNetwork prefix /{cn[0].prefixlen} "
                  f"(and <= /30 for IPv4).")
        elif cn[0].version == 4 and 2 ** (hp - cn[0].prefixlen) < (cp or 3) + (workers or 0):
            c.err(f"clusterNetwork {cn[0]} with hostPrefix {hp} fits only {2 ** (hp - cn[0].prefixlen)} nodes.")
    if sn and sn[0].version == 4 and sn[0].prefixlen > 24:
        c.warn("serviceNetwork smaller than /24 leaves few service IPs.")
    _no_overlap(c, cn, sn, "clusterNetwork", "serviceNetwork")
    _no_overlap(c, cn, mn, "clusterNetwork", "machineNetwork")
    _no_overlap(c, sn, mn, "serviceNetwork", "machineNetwork")
    ovn_reserved = [ipaddress.ip_network("100.64.0.0/16"), ipaddress.ip_network("100.88.0.0/16")]
    for n in cn + sn + mn:
        for r in ovn_reserved:
            if n.version == 4 and n.overlaps(r):
                c.err(f"{n} overlaps {r}, which OVN-Kubernetes reserves internally.")
    cfg["networking"] = {
        "networkType": c.s("oc_network_type") or "OVNKubernetes",
        "clusterNetwork": [{"cidr": str(cn[0]), "hostPrefix": hp}] if cn and hp else [],
        "serviceNetwork": [str(n) for n in sn],
        "machineNetwork": [{"cidr": str(n)} for n in mn],
    }

    platform: dict = {}
    region = c.s("oc_region")
    if plat == "none":
        platform = {"none": {}}
        if workers:
            c.warn("platform none: worker replicas are informational - you provision and join those machines (UPI).")
    elif plat in ("aws", "azure", "gcp"):
        if not region:
            c.err(f"platform.{plat}.region is required.")
        pats = {"aws": r"^[a-z]{2}(-gov)?-[a-z]+-\d$", "azure": r"^[a-z0-9]+$", "gcp": r"^[a-z]+-[a-z]+\d+$"}
        if region and not re.match(pats[plat], region):
            ex = {"aws": "us-east-1", "azure": "eastus", "gcp": "us-central1"}[plat]
            c.err(f"'{region}' is not a {plat} region (e.g. {ex}).")
        p: dict = {"region": region}
        if plat == "azure":
            if not c.s("oc_azure_rg"):
                c.err("azure needs baseDomainResourceGroupName (the resource group holding the DNS zone).")
            else:
                p["baseDomainResourceGroupName"] = c.s("oc_azure_rg")
            p["cloudName"] = c.s("oc_azure_cloud") or "AzurePublicCloud"
        if plat == "gcp":
            proj = c.s("oc_gcp_project")
            if not proj:
                c.err("gcp needs projectID.")
            elif not re.match(r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$", proj):
                c.err("gcp projectID must be 6-30 lowercase letters, digits or '-'.")
            p = {"projectID": proj, "region": region}
        platform = {plat: p}
        c.warn(f"platform {plat} is IPI: openshift-install will create billable cloud resources with your credentials.")
    else:
        apis = [_ip(c, v, "apiVIPs") for v in _csv(c.s("oc_api_vip"))]
        ings = [_ip(c, v, "ingressVIPs") for v in _csv(c.s("oc_ingress_vip"))]
        if not apis or not ings:
            c.err(f"{plat} needs apiVIPs and ingressVIPs.")
        for v in apis + ings:
            if v is not None and mn and not any(v in n for n in mn):
                c.err(f"VIP {v} must be inside machineNetwork {', '.join(map(str, mn))}.")
        if set(map(str, apis)) & set(map(str, ings)):
            c.err("apiVIPs and ingressVIPs must differ.")
        vips = {"apiVIPs": [str(v) for v in apis if v], "ingressVIPs": [str(v) for v in ings if v]}
        if plat == "vsphere":
            vals = {k: c.s(f"oc_vc_{k}") for k in ("server", "user", "password", "datacenter", "cluster",
                                                   "datastore", "network")}
            for k, v in vals.items():
                if not v:
                    c.err(f"vsphere {k} is required.")
            dc = vals["datacenter"]

            def path(v, kind):
                return v if v.startswith("/") else f"/{dc}/{kind}/{v}"
            platform = {"vsphere": {
                **vips,
                "vcenters": [{"server": vals["server"], "user": vals["user"], "password": vals["password"],
                              "port": 443, "datacenters": [dc]}],
                "failureDomains": [{"name": "generated-failure-domain", "region": "generated-region",
                                    "zone": "generated-zone", "server": vals["server"],
                                    "topology": {"datacenter": dc, "computeCluster": path(vals["cluster"], "host"),
                                                 "datastore": path(vals["datastore"], "datastore"),
                                                 "networks": [vals["network"]]}}]}}
        else:
            hosts = []
            roles = {"master": 0, "worker": 0}
            seen_mac, seen_name = set(), set()
            for i, r in enumerate(_rows(c, "ocp_bm_hosts"), 1):
                sc = _SubCtx(c, r, f"host #{i} ({r['name']}): ")
                check_row("ocp_bm_hosts", sc)
                roles[r["role"]] = roles.get(r["role"], 0) + 1
                if r["boot_mac"].lower() in seen_mac:
                    sc.err("duplicate bootMACAddress.")
                if r["name"] in seen_name:
                    sc.err("duplicate name.")
                seen_mac.add(r["boot_mac"].lower())
                seen_name.add(r["name"])
                bmc = {"address": r["bmc_address"], "username": r["bmc_user"], "password": r["bmc_password"]}
                if r.get("insecure"):
                    bmc["disableCertificateVerification"] = True
                hosts.append({"name": r["name"], "role": r["role"], "bmc": bmc, "bootMACAddress": r["boot_mac"],
                              "hardwareProfile": "default"})
            if cp and roles["master"] != cp:
                c.err(f"baremetal hosts list {roles['master']} master(s) but controlPlane.replicas is {cp}.")
            if workers is not None and roles["worker"] < workers:
                c.err(f"baremetal hosts list {roles['worker']} worker(s) but compute replicas is {workers}.")
            platform = {"baremetal": {**vips, "provisioningNetwork": c.s("oc_provisioning") or "Managed",
                                      "hosts": hosts}}
    cfg["platform"] = platform
    if c.b("oc_fips"):
        cfg["fips"] = True
    pub = c.s("oc_publish") or "External"
    if pub == "Internal" and plat not in ("aws", "azure", "gcp"):
        c.err("publish: Internal is only supported on aws, azure and gcp.")
    if pub != "External":
        cfg["publish"] = pub
    ps = c.s("oc_pull_secret")
    if ps:
        _pull_secret(c, ps)
        try:
            ps = json.dumps(json.loads(ps), separators=(",", ":"))
        except ValueError:
            pass
    cfg["pullSecret"] = ps
    key = " ".join(c.s("oc_ssh_key").split())
    if key:
        if "PRIVATE KEY" in key:
            c.err("sshKey must be a PUBLIC key - never paste a private key.")
        elif not re.match(r"^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/=]+( .*)?$", key):
            c.err("sshKey must be an OpenSSH public key (ssh-ed25519 AAAA... comment).")
        cfg["sshKey"] = key
    else:
        c.warn("no sshKey: you won't be able to SSH to nodes for debugging.")
    header = ("# openshift-install create cluster --dir <dir>   (install-config.yaml is CONSUMED - keep a copy)\n"
              f"# platform {plat}: " + ("UPI - run `create manifests` / `create ignition-configs`\n"
                                         if plat == "none" else "installer-provisioned\n"))
    res = ConfigResult([cfg], c.errors, c.warnings, "install-config.yaml", header=header,
                       info=f"install-config v1, platform {plat}")
    res.files = [OutFile("install-config.yaml", header + to_yaml(cfg), 0o600)]
    return res
