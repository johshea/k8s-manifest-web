"""
Azure Kubernetes Service: an `az aks create` script (optionally preceded by `az group create`).
Chosen over Bicep/ARM because it's what the AKS quickstarts use and needs no template toolchain.
Flags and allowed values are checked against `az aks create --help` (azure-cli 2.8x).
SSH: never uses --generate-ssh-keys (it silently writes ~/.ssh/id_rsa); it's --ssh-key-value or --no-ssh-key.
"""
from __future__ import annotations

import re

from .common import (Field, ConfigResult, OutFile, _cidrs, _csv, _ip, _no_overlap, _parse_kv, required, sh_quote, shell_script)

TYPE = "AKS"
GROUP = "Managed cloud"

LOCATIONS = ["eastus", "eastus2", "westus2", "westus3", "centralus", "southcentralus", "northcentralus", "canadacentral",
             "brazilsouth", "northeurope", "westeurope", "uksouth", "francecentral", "germanywestcentral",
             "swedencentral", "switzerlandnorth", "norwayeast", "polandcentral", "italynorth", "uaenorth",
             "southafricanorth", "centralindia", "japaneast", "koreacentral", "southeastasia", "eastasia",
             "australiaeast"]
VERSIONS = ["", "1.34", "1.33", "1.32", "1.35"]
VM_SIZES = ["Standard_D4ds_v5", "Standard_D2ds_v5", "Standard_D4s_v5", "Standard_D8ds_v5", "Standard_B4ms",
            "Standard_D4as_v5", "Standard_E4ds_v5", "Standard_D4pds_v5", "Standard_NC4as_T4_v3"]
TIERS = ["free", "standard", "premium"]
PLUGINS = ["azure", "kubenet", "none"]
POLICIES = ["none", "azure", "cilium", "calico"]
DATAPLANES = ["azure", "cilium"]
OS_SKUS = ["Ubuntu", "AzureLinux", "AzureLinux3", "Ubuntu2204", "Ubuntu2404", "AzureContainerLinux"]
OUTBOUND = ["loadBalancer", "managedNATGateway", "userAssignedNATGateway", "userDefinedRouting", "none"]
UPGRADE = ["none", "patch", "stable", "rapid", "node-image"]
NODE_OS_UPGRADE = ["NodeImage", "SecurityPatch", "Unmanaged", "None"]
ADDONS = ["monitoring", "azure-policy", "azure-keyvault-secrets-provider", "open-service-mesh", "virtual-node",
          "confcom", "ingress-appgw", "http_application_routing", "gitops"]

SCHEMA = [
    ("Cluster", [
        Field("ak_name", "--name", default="demo-aks", required=True),
        Field("ak_rg", "--resource-group", default="demo-aks-rg", required=True),
        Field("ak_create_rg", "also run az group create", "check", default=True),
        Field("ak_location", "--location", "ecombo", default="eastus2", options=LOCATIONS, required=True),
        Field("ak_version", "--kubernetes-version", "ecombo", default="", options=VERSIONS,
              help="blank = AKS default; check `az aks get-versions -l <loc>`"),
        Field("ak_tier", "--tier", "combo", default="free", options=TIERS),
        Field("ak_tags", "--tags", default="", help="key=value, comma-separated"),
    ]),
    ("System node pool", [
        Field("ak_pool", "--nodepool-name", default="system", help="lowercase alphanumeric, max 12"),
        Field("ak_vm", "--node-vm-size", "ecombo", default="Standard_D4ds_v5", options=VM_SIZES),
        Field("ak_count", "--node-count", default="3"),
        Field("ak_zones", "--zones", default="1,2,3", help="availability zones 1-3, blank = none"),
        Field("ak_os_sku", "--os-sku", "combo", default="AzureLinux", options=OS_SKUS),
        Field("ak_autoscale", "--enable-cluster-autoscaler", "check", default=True),
        Field("ak_min", "--min-count", default="3"),
        Field("ak_max", "--max-count", default="5"),
        Field("ak_max_pods", "--max-pods", default="", help="blank = default (250 overlay / 30 kubenet)"),
        Field("ak_labels", "--nodepool-labels", default=""),
        Field("ak_taints", "--nodepool-taints", default="", help="key=value:Effect, comma-separated"),
    ]),
    ("Networking", [
        Field("ak_plugin", "--network-plugin", "combo", default="azure", options=PLUGINS),
        Field("ak_overlay", "--network-plugin-mode overlay", "check", default=True),
        Field("ak_dataplane", "--network-dataplane", "combo", default="cilium", options=DATAPLANES),
        Field("ak_policy", "--network-policy", "combo", default="cilium", options=POLICIES),
        Field("ak_pod_cidr", "--pod-cidr", default="10.244.0.0/16"),
        Field("ak_svc_cidr", "--service-cidr", default="10.0.0.0/16"),
        Field("ak_dns_ip", "--dns-service-ip", default="10.0.0.10"),
        Field("ak_outbound", "--outbound-type", "combo", default="loadBalancer", options=OUTBOUND),
        Field("ak_private", "--enable-private-cluster", "check", default=False),
        Field("ak_authorized", "--api-server-authorized-ip-ranges", default=""),
    ]),
    ("Identity, upgrades & add-ons", [
        Field("ak_oidc", "--enable-oidc-issuer", "check", default=True),
        Field("ak_wi", "--enable-workload-identity", "check", default=True),
        Field("ak_aad", "--enable-aad (Entra ID)", "check", default=True),
        Field("ak_azure_rbac", "--enable-azure-rbac", "check", default=False),
        Field("ak_upgrade", "--auto-upgrade-channel", "combo", default="patch", options=UPGRADE),
        Field("ak_node_upgrade", "--node-os-upgrade-channel", "combo", default="NodeImage", options=NODE_OS_UPGRADE),
        Field("ak_addons", "--enable-addons", default="", help="comma-separated: " + ", ".join(ADDONS)),
        Field("ak_ssh_key", "--ssh-key-value", default="",
              help="public key path/content; blank = --no-ssh-key (no node SSH)"),
    ]),
]
COLLAPSED = {"Networking", "Identity, upgrades & add-ons"}


def build(c) -> ConfigResult:
    required(c, SCHEMA)
    name, rg, loc = c.s("ak_name"), c.s("ak_rg"), c.s("ak_location")
    if name and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,61}[A-Za-z0-9]$", name):
        c.err("--name must be 2-63 letters, digits, '_' or '-', starting and ending alphanumeric.")
    if rg and (not re.match(r"^[-\w._()]{1,90}$", rg) or rg.endswith(".")):
        c.err("--resource-group must be 1-90 letters, digits, '_', '-', '.', '(' or ')' and not end with '.'.")
    if loc and not re.match(r"^[a-z0-9]+$", loc):
        c.err(f"--location '{loc}' must be an Azure region name like eastus2 (no spaces).")
    flags: list = [("--resource-group", rg), ("--name", name), ("--location", loc)]
    ver = c.s("ak_version")
    if ver:
        if not re.match(r"^\d+\.\d+(\.\d+)?$", ver):
            c.err("--kubernetes-version must look like 1.34 or 1.34.1.")
        flags.append(("--kubernetes-version", ver))
    tier = c.s("ak_tier") or "free"
    if tier not in TIERS:
        c.err(f"--tier must be one of {', '.join(TIERS)}.")
    flags.append(("--tier", tier))
    tags = _parse_kv(c, c.s("ak_tags"), "--tags")
    if tags:
        flags.append(("--tags", [f"{k}={v}" for k, v in tags.items()]))
    pool = c.s("ak_pool") or "system"
    if not re.match(r"^[a-z][a-z0-9]{0,11}$", pool):
        c.err("--nodepool-name must be lowercase alphanumeric, start with a letter, max 12 chars.")
    flags += [("--nodepool-name", pool), ("--node-vm-size", c.s("ak_vm") or "Standard_D4ds_v5")]
    if not re.match(r"^Standard_[A-Za-z0-9_]+$", c.s("ak_vm") or "Standard_D4ds_v5"):
        c.err("--node-vm-size must be an Azure VM size like Standard_D4ds_v5.")
    n = c.int("ak_count", "--node-count", lo=1, hi=1000, required=True)
    if n is not None:
        flags.append(("--node-count", str(n)))
        if n < 2:
            c.warn("a single system node has no redundancy; AKS recommends 3 for production.")
    zones = _csv(c.s("ak_zones"))
    if any(z not in ("1", "2", "3") for z in zones) or len(set(zones)) != len(zones):
        c.err("--zones must be distinct values from 1, 2, 3.")
    elif zones:
        flags.append(("--zones", zones))
    osku = c.s("ak_os_sku") or "AzureLinux"
    if osku not in OS_SKUS:
        c.err(f"--os-sku must be one of {', '.join(OS_SKUS)}.")
    flags.append(("--os-sku", osku))
    if c.b("ak_autoscale"):
        lo = c.int("ak_min", "--min-count", lo=1, hi=1000, required=True)
        hi = c.int("ak_max", "--max-count", lo=1, hi=1000, required=True)
        if lo is not None and hi is not None:
            if lo > hi:
                c.err("--min-count must be <= --max-count.")
            if n is not None and not lo <= n <= hi:
                c.err("--node-count must be within --min-count..--max-count when the autoscaler is on.")
        flags += [("--enable-cluster-autoscaler", None), ("--min-count", c.s("ak_min")), ("--max-count", c.s("ak_max"))]
    plugin = c.s("ak_plugin") or "azure"
    mp = c.int("ak_max_pods", "--max-pods", lo=10, hi=250)
    if mp is not None:
        if plugin == "kubenet" and mp > 110:
            c.err("--max-pods can't exceed 110 with kubenet.")
        flags.append(("--max-pods", str(mp)))
    lab = _parse_kv(c, c.s("ak_labels"), "--nodepool-labels", label_rules=True)
    if lab:
        flags.append(("--nodepool-labels", [f"{k}={v}" for k, v in lab.items()]))
    taints = _csv(c.s("ak_taints"))
    for t in taints:
        if not re.match(r"^[^=:\s]+=[^:\s]*:(NoSchedule|PreferNoSchedule|NoExecute)$", t):
            c.err(f"--nodepool-taints '{t}' must be key=value:Effect.")
    if taints:
        c.warn("tainting the only (system) pool can block system pods unless they tolerate it; "
               "AKS allows only CriticalAddonsOnly=true:NoSchedule here in practice.")
        flags.append(("--nodepool-taints", ",".join(taints)))

    if plugin not in PLUGINS:
        c.err(f"--network-plugin must be one of {', '.join(PLUGINS)}.")
    flags.append(("--network-plugin", plugin))
    overlay, dp, pol = c.b("ak_overlay"), c.s("ak_dataplane") or "azure", c.s("ak_policy") or "none"
    if overlay:
        if plugin != "azure":
            c.err("--network-plugin-mode overlay requires --network-plugin azure.")
        flags.append(("--network-plugin-mode", "overlay"))
    if dp not in DATAPLANES or pol not in POLICIES:
        c.err("invalid --network-dataplane / --network-policy value.")
    if dp == "cilium":
        if plugin != "azure":
            c.err("--network-dataplane cilium requires --network-plugin azure.")
        if pol not in ("cilium", "none"):
            c.err("with the Cilium dataplane, --network-policy must be cilium (or none).")
        flags.append(("--network-dataplane", "cilium"))
    elif pol == "cilium":
        c.err("--network-policy cilium requires --network-dataplane cilium.")
    if plugin == "none" and pol != "none":
        c.err("--network-plugin none (BYO CNI) can't use an AKS network policy engine.")
    if pol != "none":
        flags.append(("--network-policy", pol))
    pods = svcs = []
    if c.s("ak_pod_cidr"):
        pods = _cidrs(c, c.s("ak_pod_cidr"), "--pod-cidr")
        if plugin == "azure" and not overlay:
            c.err("--pod-cidr is only used with kubenet or Azure CNI overlay (flat Azure CNI uses subnet IPs).")
        elif pods:
            flags.append(("--pod-cidr", str(pods[0])))
    if c.s("ak_svc_cidr"):
        svcs = _cidrs(c, c.s("ak_svc_cidr"), "--service-cidr")
        if svcs:
            flags.append(("--service-cidr", str(svcs[0])))
    _no_overlap(c, pods, svcs, "--pod-cidr", "--service-cidr")
    if c.s("ak_dns_ip"):
        dns = _ip(c, c.s("ak_dns_ip"), "--dns-service-ip")
        if dns is not None:
            if svcs and dns not in svcs[0]:
                c.err(f"--dns-service-ip {dns} must be inside --service-cidr {svcs[0]}.")
            elif svcs and dns == svcs[0].network_address + 1:
                c.err("--dns-service-ip can't be the first address of the service CIDR (used by kubernetes svc).")
            flags.append(("--dns-service-ip", str(dns)))
        if not svcs:
            c.err("--dns-service-ip requires --service-cidr.")
    ob = c.s("ak_outbound") or "loadBalancer"
    if ob not in OUTBOUND:
        c.err(f"--outbound-type must be one of {', '.join(OUTBOUND)}.")
    if ob in ("userAssignedNATGateway", "userDefinedRouting"):
        c.warn(f"--outbound-type {ob} needs a pre-configured subnet: add --vnet-subnet-id when running.")
    flags.append(("--outbound-type", ob))
    auth = _csv(c.s("ak_authorized"))
    for a in auth:
        _cidrs(c, a if "/" in a else a + "/32", "--api-server-authorized-ip-ranges")
    if c.b("ak_private"):
        if auth:
            c.err("--api-server-authorized-ip-ranges can't be used with a private cluster.")
        flags.append(("--enable-private-cluster", None))
    elif auth:
        flags.append(("--api-server-authorized-ip-ranges", ",".join(auth)))
    else:
        c.warn("the public API server accepts any IP; consider --api-server-authorized-ip-ranges.")

    flags.append(("--enable-managed-identity", None))
    if c.b("ak_wi") and not c.b("ak_oidc"):
        c.err("--enable-workload-identity requires --enable-oidc-issuer.")
    if c.b("ak_oidc"):
        flags.append(("--enable-oidc-issuer", None))
    if c.b("ak_wi"):
        flags.append(("--enable-workload-identity", None))
    if c.b("ak_azure_rbac") and not c.b("ak_aad"):
        c.err("--enable-azure-rbac requires --enable-aad.")
    if c.b("ak_aad"):
        flags.append(("--enable-aad", None))
    if c.b("ak_azure_rbac"):
        flags.append(("--enable-azure-rbac", None))
    up, nup = c.s("ak_upgrade") or "none", c.s("ak_node_upgrade") or "NodeImage"
    if up not in UPGRADE or nup not in NODE_OS_UPGRADE:
        c.err("invalid upgrade channel value.")
    if up == "node-image" and nup != "NodeImage":
        c.err("--auto-upgrade-channel node-image requires --node-os-upgrade-channel NodeImage.")
    flags += [("--auto-upgrade-channel", up), ("--node-os-upgrade-channel", nup)]
    addons = _csv(c.s("ak_addons"))
    for a in addons:
        if a not in ADDONS:
            c.err(f"--enable-addons '{a}' is not an AKS add-on ({', '.join(ADDONS)}).")
    if "http_application_routing" in addons:
        c.warn("http_application_routing is retired; use the application routing add-on (az aks approuting).")
    if "virtual-node" in addons:
        c.warn("virtual-node needs --aci-subnet-name and flat Azure CNI networking.")
    if addons:
        flags.append(("--enable-addons", ",".join(addons)))
    key = c.s("ak_ssh_key")
    if key:
        if key.startswith(("ssh-", "ecdsa-")) and len(key.split()) < 2:
            c.err("--ssh-key-value looks like a truncated public key.")
        if "PRIVATE KEY" in key:
            c.err("--ssh-key-value must be a PUBLIC key, never a private key.")
        flags.append(("--ssh-key-value", key))
    else:
        flags.append(("--no-ssh-key", None))
    pre = []
    if c.b("ak_create_rg"):
        pre = [f'"${{AZ:-az}}" group create --name {sh_quote(rg)} --location {sh_quote(loc)} --output none', ""]
    post = ["", f'"${{AZ:-az}}" aks get-credentials --resource-group {sh_quote(rg)} --name {sh_quote(name)}']
    comments = ["Generated by k8s-manifest-gui. Needs `az login`; creates billable Azure resources.",
                "Extra flags are passed through, e.g.:  ./create-aks.sh --no-wait"]
    script = shell_script(f"AKS cluster '{name}' in {loc}", comments, ['"${AZ:-az}"', "aks", "create"], flags,
                          pre=pre, post=post)
    res = ConfigResult([], c.errors, c.warnings, f"create-aks-{name or 'cluster'}.sh", info="az aks create")
    res.files = [OutFile(res.filename, script, 0o755)]
    return res
