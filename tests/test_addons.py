#!/usr/bin/env python3
"""
Tests for the Networking & Storage mode (addons_gen: CNI + CSI add-ons) and its cross-links.

  python3 tests/test_addons.py              # every type: defaults + a rich case, file syntax, negative cases,
                                            # cross-check warnings, session StorageClass options
  python3 tests/test_addons.py --tools      # + helm template of every generated values.yaml against the pinned
                                            #   chart (needs helm + network), bash -n of install.sh
  python3 tests/test_addons.py --server     # + kubectl apply --dry-run=server of every generated YAML against the
                                            #   current cluster (CRDs for Tigera / Cilium / snapshots / Multus NAD /
                                            #   Ceph must exist in the cluster)

Generated files are written to tests/output-addons/<type>/.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

import addons_gen as agen  # noqa: E402
import providers as P  # noqa: E402
from addons import csi  # noqa: E402

OUT = os.path.join(HERE, "output-addons")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL " + msg)


RICH = {
    "Calico": {"cal_provider": "Kind", "cal_bgp": "Disabled", "cal_mtu": "1440", "cal_dataplane": "Nftables",
               "cal_autodetect": "interface", "cal_autodetect_value": "eth.*", "cal_goldmane": True, "cal_whisker": True,
               "cal_pools": [{"name": "pool-a", "cidr": "10.244.0.0/17", "encapsulation": "VXLAN",
                              "nat_outgoing": "Enabled", "block_size": "26", "node_selector": "all()"},
                             {"name": "pool-b", "cidr": "10.244.128.0/17", "encapsulation": "VXLANCrossSubnet",
                              "nat_outgoing": "Disabled", "block_size": "24", "node_selector": "zone == 'b'"}]},
    "Cilium": {"cil_ipam": "cluster-pool", "cil_pool_cidrs": "10.244.0.0/16", "cil_kpr": "true",
               "cil_api_host": "172.18.0.2", "cil_api_port": "6443", "cil_routing": "tunnel", "cil_tunnel": "geneve",
               "cil_hubble": True, "cil_relay": True, "cil_ui": True, "cil_encryption": "wireguard",
               "cil_node_encryption": True, "cil_bgp": True, "cil_gateway": True, "cil_l2": True,
               "cil_bpf_masq": True, "cil_operator_replicas": "1", "cil_method": "cilium CLI"},
    "Flannel": {"fl_network": "10.42.0.0/16", "fl_subnet_len": "24", "fl_backend": "wireguard",
                "fl_wg_keepalive": "25", "fl_iface": "eth1", "fl_nftables": True},
    "Canal": {"cn_network": "10.244.0.0/16", "cn_backend": "host-gw", "cn_iface": "eth0", "cn_mtu": "1450"},
    "Antrea": {"an_encap": "encap", "an_tunnel": "vxlan", "an_encryption": "ipsec", "an_proxy_all": True,
               "an_api_override": "172.18.0.2:6443", "an_node_ipam": True, "an_cluster_cidrs": "10.244.0.0/16",
               "an_feature_gates": "Multicast=true"},
    "Multus": {"mu_type": "ipvlan", "mu_ipvlan_mode": "l3", "mu_ipam": "whereabouts", "mu_whereabouts": True,
               "mu_subnet": "10.10.0.0/24", "mu_range_start": "", "mu_range_end": "", "mu_gateway": "",
               "mu_exclude": "10.10.0.0/30", "mu_routes": "10.20.0.0/16"},
    "AWS EBS": {"ebs_type": "io2", "ebs_iops": "8000", "ebs_kms": "arn:aws:kms:us-east-1:111122223333:key/abcd",
                "ebs_fstype": "xfs", "snap_create": True, "install_driver": True,
                "ebs_role_arn": "arn:aws:iam::111122223333:role/ebs-csi",
                "allowed_topologies": [{"key": "topology.ebs.csi.aws.com/zone", "values": "us-east-1a"}]},
    "AWS EFS": {"efs_fs_id": "fs-0abc1234def567890", "efs_uid": "1000", "efs_gid": "1000",
                "efs_sub_path": "${.PVC.namespace}/${.PVC.name}", "efs_unique": False, "install_driver": True},
    "GCE PD": {"gce_type": "pd-ssd", "gce_replication": "regional-pd", "snap_create": True,
               "allowed_topologies": [{"key": "topology.gke.io/zone", "values": "us-central1-a, us-central1-b"}]},
    "Azure Disk": {"azd_sku": "PremiumV2_LRS", "azd_caching": "None", "azd_iops": "5000", "azd_mbps": "200",
                   "snap_create": True},
    "Azure File": {"azf_sku": "Premium_LRS", "azf_protocol": "nfs", "mount_options": "nconnect=4"},
    "vSphere": {"vs_datastore": "ds:///vmfs/volumes/vsan:52c1/", "snap_create": True},
    "Nutanix": {"nx_create_secret": True, "nx_prism": "10.0.0.50", "nx_user": "k8s", "nx_password": "pw",
                "nx_flash": True, "snap_create": True, "install_driver": True},
    "NFS CSI": {"nfs_demo_server": True, "nfs_subdir": "${pvc.metadata.namespace}/${pvc.metadata.name}",
                "nfs_on_delete": "retain", "snap_create": True, "snap_install_crds": True},
    "SMB CSI": {"smb_create_secret": True, "smb_user": "k8s", "smb_password": "pw", "smb_domain": "CORP"},
    "Longhorn": {"lh_replicas": "1", "lh_locality": "strict-local", "snap_create": True, "lh_snap_type": "bak"},
    "Rook-Ceph RBD": {"rk_features": "layering,exclusive-lock,object-map,fast-diff", "rk_replicas": "1",
                      "rk_failure_domain": "osd", "snap_create": True},
    "Rook-Ceph CephFS": {"rk_fs_name": "data", "rk_fs_pool": "data-replicated", "snap_create": True},
    "OpenEBS LocalPV": {"oe_base_path": "/mnt/openebs", "default_class": True},
    "TopoLVM": {"install_driver": True, "tv_cert_manager": True, "tv_vg": "vg-ssd"},
    "local-path": {"name": "local-path", "lp_path_pattern": "{{ .PVC.Namespace }}/{{ .PVC.Name }}",
                   "lp_volume_type": "local"},
}

NEGATIVE = [
    ("Calico", {"cal_bgp": "Disabled", "cal_pools": [{"name": "p", "cidr": "10.0.0.0/16", "encapsulation": "IPIP"}]}, "BGP"),
    ("Calico", {"cal_pools": [{"name": "p", "cidr": "10.0.0.1/16"}]}, "not a valid CIDR"),
    ("Calico", {"cal_pools": [{"name": "a", "cidr": "10.0.0.0/16"}, {"name": "b", "cidr": "10.0.128.0/17"}]}, "overlaps"),
    ("Calico", {"cal_version": "3.33.0"}, "leading 'v'"),
    ("Cilium", {"cil_kpr": "true", "cil_api_host": ""}, "k8sServiceHost"),
    ("Cilium", {"cil_routing": "native", "cil_native_cidr": ""}, "ipv4NativeRoutingCIDR"),
    ("Cilium", {"cil_version": "v1.20.3"}, "drop the leading"),
    ("Flannel", {"fl_network": "10.244.0.0/33"}, "CIDR"),
    ("Antrea", {"an_proxy_all": True, "an_api_override": ""}, "kubeAPIServerOverride"),
    ("Multus", {"mu_range_start": "10.9.9.9"}, ""),
    ("AWS EBS", {"ebs_type": "gp2", "ebs_throughput": "300"}, "throughput only applies to gp3"),
    ("AWS EBS", {"ebs_type": "io1"}, "need iops"),
    ("AWS EBS", {"ebs_encrypted": False, "ebs_kms": "arn:aws:kms:x"}, "encrypted"),
    ("AWS EFS", {"efs_fs_id": "bogus"}, "fileSystemId"),
    ("GCE PD", {"gce_type": "pd-extreme", "gce_replication": "regional-pd"}, "regional-pd"),
    ("Azure Disk", {"azd_sku": "UltraSSD_LRS", "azd_caching": "ReadOnly"}, "cachingMode None"),
    ("Azure File", {"azf_protocol": "nfs", "azf_sku": "Standard_LRS"}, "Premium"),
    ("vSphere", {"vs_datastore": "/vmfs/x"}, "ds:///"),
    ("Nutanix", {"nx_container": ""}, "storageContainer"),
    ("Nutanix", {"nx_storage_type": "NutanixFiles"}, "nfsServer"),
    ("Nutanix", {"nx_create_secret": True, "nx_prism": ""}, "Prism"),
    ("NFS CSI", {"nfs_server": ""}, "server is required"),
    ("NFS CSI", {"nfs_share": "exports"}, "absolute"),
    ("SMB CSI", {"smb_source": "smb.example.com/share"}, "//server/share"),
    ("Longhorn", {"lh_replicas": "2", "lh_locality": "strict-local"}, "strict-local"),
    ("Rook-Ceph RBD", {"rk_features": "object-map"}, "exclusive-lock"),
    ("Rook-Ceph CephFS", {"rk_fs_pool": "other"}, "<fsName>-<pool name>"),
    ("OpenEBS LocalPV", {"binding_mode": "Immediate"}, "WaitForFirstConsumer"),
    ("local-path", {"binding_mode": "Immediate"}, "WaitForFirstConsumer"),
    ("TopoLVM", {"binding_mode": "Immediate"}, "WaitForFirstConsumer"),
    ("NFS CSI", {"name": "Bad_Name"}, "DNS-1123"),
    ("NFS CSI", {"install_driver": True, "drv_version": "v4.13.5"}, "leading 'v'"),
    ("AWS EBS", {"extra_params": "novalue"}, "key=value"),
]


def write_files(atype, tag, res):
    d = os.path.join(OUT, P.type_folder(atype) + ("-" + tag if tag else ""))
    os.makedirs(d, exist_ok=True)
    paths = []
    for name, text, mode in P.output_files(atype, res):
        p = os.path.join(d, name)
        with open(p, "w") as fh:
            fh.write(text)
        if mode:
            os.chmod(p, mode)
        paths.append(p)
    return paths


def syntax_ok(path):
    text = open(path).read()
    if path.endswith((".yaml", ".yml")):
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        if not docs:
            return "empty YAML"
        for d in docs:
            if path.endswith("values.yaml"):
                continue
            if not isinstance(d, dict) or not d.get("apiVersion") or not d.get("kind"):
                return f"document without apiVersion/kind: {str(d)[:80]}"
            if d["kind"] == "NetworkAttachmentDefinition":
                json.loads(d["spec"]["config"])
            if d["kind"] == "ConfigMap":
                for k, v in (d.get("data") or {}).items():
                    if k.endswith(".json") or k == "cni_network_config":
                        json.loads(v.replace("__CNI_MTU__", "1500"))
    elif path.endswith(".sh"):
        p = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        if p.returncode:
            return p.stderr.strip()
        if not os.access(path, os.X_OK):
            return "install.sh is not executable"
        if "set -euo pipefail" not in text:
            return "install.sh without set -euo pipefail"
    return None


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    all_files = {}
    print(f"== {len(agen.ADDON_TYPES)} add-on types: defaults + rich case ==")
    check(set(RICH) == set(agen.ADDON_TYPES), f"RICH covers every type ({set(agen.ADDON_TYPES) ^ set(RICH)})")
    check(P.PROVIDERS["Networking & Storage"].kinds == agen.ADDON_TYPES, "provider lists every add-on type")
    for atype in agen.ADDON_TYPES:
        for tag, vals in (("default", {}), ("", RICH.get(atype, {}))):
            res = agen.generate(atype, vals)
            check(res.ok, f"{atype} {tag or 'rich'} should validate: {res.errors}")
            if not res.ok:
                continue
            check(any(o.name == "install.sh" for o in agen.outputs(res)), f"{atype}: install.sh emitted")
            paths = write_files(atype, tag, res)
            for p in paths:
                why = syntax_ok(p)
                check(why is None, f"{atype} {os.path.basename(p)}: {why}")
            if not tag:
                all_files[atype] = paths
        names = [os.path.basename(p) for p in all_files.get(atype, [])]
        print(f"  {atype:18s} OK  {', '.join(names)}")

    print("== Specific assertions ==")
    cal = [d for d in yaml.safe_load_all(open(os.path.join(OUT, "calico", "custom-resources.yaml"))) if d][0]
    check(cal["kind"] == "Installation" and cal["spec"]["calicoNetwork"]["ipPools"][1]["blockSize"] == 24
          and cal["spec"]["calicoNetwork"]["bgp"] == "Disabled", "Calico Installation pools / bgp")
    cil = yaml.safe_load(open(os.path.join(OUT, "cilium", "values.yaml")))
    check(cil["kubeProxyReplacement"] is True and cil["encryption"] == {"enabled": True, "type": "wireguard",
          "nodeEncryption": True} and cil["ipam"]["operator"]["clusterPoolIPv4PodCIDRList"] == ["10.244.0.0/16"],
          f"Cilium values: {cil.get('encryption')} {cil.get('ipam')}")
    sh = open(os.path.join(OUT, "cilium", "install.sh")).read()
    check("gateway-api" in sh and "cilium install" in sh, "Cilium install.sh: Gateway API CRDs + cilium CLI")
    ebs = yaml.safe_load(open(os.path.join(OUT, "aws-ebs", "storageclass.yaml")))
    check(ebs["provisioner"] == "ebs.csi.aws.com" and ebs["parameters"]["type"] == "io2"
          and ebs["parameters"]["iops"] == "8000" and ebs["parameters"]["csi.storage.k8s.io/fstype"] == "xfs",
          f"EBS parameters {ebs['parameters']}")
    nx = os.path.join(OUT, "nutanix", "secret.yaml")
    check(oct(os.stat(nx).st_mode & 0o777) == "0o600", "Nutanix secret.yaml is 0600")
    import base64
    key = base64.b64decode(yaml.safe_load(open(nx))["data"]["key"]).decode()
    check(key == "10.0.0.50:9440:k8s:pw", f"Nutanix key format prism:port:user:pass ({key})")
    rbd = yaml.safe_load(open(os.path.join(OUT, "rook-ceph-rbd-default", "storageclass.yaml")))
    check(rbd["provisioner"] == "rook-ceph.rbd.csi.ceph.com" and
          rbd["parameters"]["csi.storage.k8s.io/node-stage-secret-name"] == "rook-csi-rbd-node", "Rook RBD secrets")
    lp = open(os.path.join(OUT, "local-path", "install.sh")).read()
    check("delete storageclass local-path" in lp and "v0.0.37" in lp, "local-path replaces upstream SC, pinned version")
    nfs = yaml.safe_load(open(os.path.join(OUT, "nfs-csi", "storageclass.yaml")))
    check(nfs["parameters"]["server"] == "nfs-server.nfs-demo.svc.cluster.local" and nfs["parameters"]["onDelete"] == "retain",
          "NFS demo server wiring")
    demo = list(yaml.safe_load_all(open(os.path.join(OUT, "nfs-csi", "nfs-server.yaml"))))
    cm = [x for x in demo if x and x["kind"] == "ConfigMap"]
    dep = [x for x in demo if x and x["kind"] == "Deployment"][0]["spec"]["template"]["spec"]
    check(cm and "Path = /export/share;" in cm[0]["data"]["vfs.conf"] and "Access_Type = RW;" in cm[0]["data"]["vfs.conf"]
          and dep["initContainers"][0]["name"] == "init-export"
          and not os.path.exists(os.path.join(OUT, "nfs-csi", "ceph-pool.yaml")),
          "NFS demo server: RW export /export/share as :/export via init container (ConfigMap in nfs-server.yaml)")
    oe = yaml.safe_load(open(os.path.join(OUT, "openebs-localpv", "storageclass.yaml")))
    check("BasePath" in oe["metadata"]["annotations"]["cas.openebs.io/config"] and "parameters" not in oe,
          "OpenEBS config annotation")

    print("== Negative cases ==")
    for atype, ov, needle in NEGATIVE:
        res = agen.generate(atype, ov)
        hit = bool(res.errors) and any(needle.lower() in e.lower() for e in res.errors)
        check(hit, f"{atype} {ov} expected error '{needle}', got {res.errors}")
    print(f"  {len(NEGATIVE)} invalid inputs rejected, e.g. {agen.generate(*NEGATIVE[0][:2]).errors[0]}")
    check(agen.generate("Nope").errors, "unknown type rejected")
    check(agen.check_row("cal_pools", {"name": "p", "cidr": "10.0.0.1/16"}), "check_row(cal_pools) flags bad CIDR")
    check(agen.check_row("cal_pools", {"name": "p", "cidr": "10.0.0.0/16"}) == [], "check_row(cal_pools) ok row")
    check(P.check_row_any("cal_pools", {"name": "p", "cidr": "bad"}), "providers.check_row_any routes add-on tables")
    check(P.check_row_any("allowed_topologies", {"key": "", "values": ""}) is not None, "allowed_topologies row check")

    print("== Cross-checks (soft warnings) ==")
    cache = {"kind": {"disable_default_cni": True, "pod_subnet": "10.244.0.0/16"}, "Calico": {}}
    w = P.cross_warnings("Calico", {}, cache)
    check(any("192.168.0.0/16" in x and "10.244.0.0/16" in x for x in w), f"Calico default pool vs kind podSubnet: {w}")
    w = P.cross_warnings("Calico", RICH["Calico"], cache)
    check(w == [], f"matching pools -> no warning: {w}")
    w = P.cross_warnings("kind", {"disable_default_cni": False}, {"Cilium": {"cil_pool_cidrs": "10.244.0.0/16"}})
    check(any("kindnet" in x for x in w) and not any("does not match" in x for x in w), f"kind default CNI warning: {w}")
    w = P.cross_warnings("k3s / RKE2", {"flannel_backend": "none", "cluster_cidr": "10.42.0.0/16"},
                         {"Flannel": {"fl_network": "10.42.0.0/16"}})
    check(w == [], f"k3s flannel-backend none + matching flannel: {w}")
    w = P.cross_warnings("k3s / RKE2", {"distro": "rke2", "cni": "canal"}, {"Calico": {"cal_mtu": "1440"}})
    check(any("RKE2" in x for x in w), f"RKE2 cni warning: {w}")
    for ctype, vals, needle in (("kubeadm", {"pod_subnet": "10.244.0.0/16"}, "does not match"),
                                ("minikube", {"mk_cni": ""}, "--cni=false"), ("EKS", {}, "vpc-cni"),
                                ("AKS", {}, "network plugin none"), ("GKE", {}, "GKE manages"),
                                ("k0s", {}, "custom"), ("Talos", {}, "flannel"), ("Kubespray", {}, "kube_network_plugin")):
        w = P.cross_warnings(ctype, vals, {"Calico": {"cal_mtu": "1440"}})
        check(any(needle in x for x in w), f"{ctype} cross-check '{needle}': {w}")
    check(P.cross_warnings("kind", {}, {"Calico": {}}) == [], "forms still at their defaults are ignored")
    check(P.cross_warnings("Multus", {}, cache) == [], "Multus is secondary: no cross-check")
    check(P.cross_warnings("Deployment", {}, cache) == [], "manifest kinds: no cross-check")
    for atype in agen.PRIMARY_CNI:
        check(agen.pod_cidrs(atype, {}) or atype == "Antrea", f"{atype} reports its pod CIDR")

    print("== Session StorageClass names in PVC / StatefulSet dropdowns ==")
    sess = {"StorageClass": {"name": "fast"}, "NFS CSI": {}, "Longhorn": {"name": "lh"}, "Calico": {}}
    check(P.session_storage_classes(sess) == ["fast", "nfs-csi", "lh"], f"names: {P.session_storage_classes(sess)}")
    o = P.dynamic_options("PersistentVolumeClaim", sess)["storage_class"]
    check(o[-3:] == ["fast", "nfs-csi", "lh"] and "" in o, "PVC storage_class options extended")
    check("vct_storage_class" in P.dynamic_options("StatefulSet", sess), "StatefulSet vct_storage_class extended")
    check(P.dynamic_options("Deployment", sess) == {}, "other kinds unchanged")

    print("== Save-all plan ==")
    results = {t: agen.generate(t, RICH[t]) for t in ("Calico", "NFS CSI", "SMB CSI")}
    files, bad = P.save_all_plan("Networking & Storage", results)
    names = [f[0] for f in files]
    check(not bad and "calico/install.sh" in names and "nfs-csi/storageclass.yaml" in names
          and "all-in-one.yaml" not in names, f"per-type folders: {names}")
    check(dict((n, m) for n, _, m in files)["smb-csi/secret.yaml"] == 0o600, "SMB secret 0600 in save-all")
    check(dict((n, m) for n, _, m in files)["calico/install.sh"] == 0o755, "install.sh 0755")

    rc = 0
    if "--tools" in sys.argv:
        rc |= run_helm_template()
    if "--server" in sys.argv:
        rc |= run_server_dry_run(all_files)
    print(f"\nADDONS RESULT: {passed} checks passed, {failed} failed")
    return 1 if failed or rc else 0


# --------------------------------------------------------------------------- optional real tools
def _which(name):
    for cand in (os.environ.get(name.upper()), shutil.which(name), os.path.join(ROOT, "bin", name),
                 f"/workspace/bin/{name}"):
        if cand and os.path.exists(cand):
            return cand
    return None


CHARTS = {  # type -> (repo name, chart, release name for template)
    "Cilium": ("cilium", "cilium"), "Antrea": ("antrea", "antrea"), "AWS EBS": ("aws-ebs-csi-driver", "aws-ebs-csi-driver"),
    "AWS EFS": ("aws-efs-csi-driver", "aws-efs-csi-driver"), "Nutanix": ("nutanix", "nutanix-csi-storage"),
    "NFS CSI": ("csi-driver-nfs", "csi-driver-nfs"), "Longhorn": ("longhorn", "longhorn"),
    "Rook-Ceph RBD": ("rook-release", "rook-ceph"), "OpenEBS LocalPV": ("openebs-localpv", "localpv-provisioner"),
    "TopoLVM": ("topolvm", "topolvm"),
}


def run_helm_template():
    helm = _which("helm")
    if not helm:
        print("== helm not found; skipping helm template ==")
        return 0
    from addons.common import HELM_REPOS
    print(f"== helm template of every generated values.yaml ({helm}) ==")
    bad = 0
    for atype, (repo, chart) in CHARTS.items():
        vals = dict(RICH[atype])
        if atype in csi.DRIVERS:
            vals["install_driver"] = True
        res = agen.generate(atype, vals)
        paths = write_files(atype, "helm", res)
        vf = [p for p in paths if p.endswith("values.yaml")]
        sh = open([p for p in paths if p.endswith("install.sh")][0]).read()
        ver = next((ln.split('"')[1] for ln in sh.splitlines() if ln.startswith("VERSION=")), None) or \
            agen.defaults(atype).get({"Cilium": "cil_version", "Antrea": "an_version"}.get(atype, "drv_version"))
        subprocess.run([helm, "repo", "add", repo, HELM_REPOS[repo], "--force-update"], capture_output=True)
        args = [helm, "template", "t", f"{repo}/{chart}", "--version", ver, "--namespace", "kube-system"]
        if vf:
            args += ["-f", vf[0]]
        if atype == "Antrea" and vals.get("an_encryption") == "ipsec":
            args += ["--set", "ipsec.psk=test"]
        p = subprocess.run(args, capture_output=True, text=True)
        ok = p.returncode == 0 and "kind:" in p.stdout
        bad += not ok
        n = p.stdout.count("\nkind:")
        print(f"  [{'OK ' if ok else 'ERR'}] {atype:16s} {chart} {ver}: {n} objects {p.stderr.strip()[-200:] if not ok else ''}")
        check(ok, f"helm template {atype}")
    return 1 if bad else 0


def run_server_dry_run(all_files):
    kubectl = _which("kubectl")
    if not kubectl:
        print("== kubectl not found; skipping server dry-run ==")
        return 0
    print("== kubectl apply --dry-run=server of every generated YAML ==")
    bad = 0
    for atype, paths in all_files.items():
        for p in paths:
            if not p.endswith(".yaml") or p.endswith("values.yaml"):
                continue
            r = subprocess.run([kubectl, "apply", "--dry-run=server", "-f", p], capture_output=True, text=True)
            msg = (r.stdout + r.stderr).strip().replace("\n", " | ")
            if r.returncode and "namespaces" in msg and "not found" in msg:
                ns = [d["metadata"].get("namespace") for d in yaml.safe_load_all(open(p)) if d and d.get("metadata")]
                for n in {x for x in ns if x}:
                    subprocess.run([kubectl, "create", "namespace", n], capture_output=True)
                r = subprocess.run([kubectl, "apply", "--dry-run=server", "-f", p], capture_output=True, text=True)
                msg = (r.stdout + r.stderr).strip().replace("\n", " | ")
            bad += r.returncode != 0
            print(f"  [{'OK ' if r.returncode == 0 else 'ERR'}] {atype:16s} {os.path.basename(p):36s} {msg[:160]}")
    print(f"  => {bad} failures")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
