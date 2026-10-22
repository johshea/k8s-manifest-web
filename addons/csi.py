"""
addons.csi - CSI StorageClass builders, one type per driver, with driver-specific parameters and optional
VolumeSnapshotClass, driver Secret and driver install (Helm values / manifests + install.sh).
No GUI dependency. Parameter names follow each driver's documentation (checked Oct 2026).
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import manifest_gen as gen
from .common import (VERSIONS, ConfigResult, Field, OutFile, dns, helm_install, kv, script, snapshot_install_lines,
                     to_yaml, version, yaml_file, _csv)

FSTYPES = ["ext4", "xfs"]
NFS_DEMO_IMAGE = "registry.k8s.io/sig-storage/nfs-provisioner:v4.0.8"
NFS_DEMO_GANESHA_CONF = """EXPORT {
    Export_Id = 0;
    Path = /;
    Pseudo = /;
    Protocols = 4;
    Access_Type = MDONLY_RO;
    Filesystem_Id = 152.152;
    FSAL { Name = PSEUDO; }
}

EXPORT {
    Export_Id = 1;
    Path = /export/share;
    Pseudo = /export;
    Protocols = 4;
    Transports = TCP;
    Access_Type = RW;
    Squash = No_Root_Squash;
    SecType = sys;
    Filesystem_Id = 1.1;
    FSAL { Name = VFS; }
}

NFS_Core_Param {
    MNT_Port = 20048;
    NLM_Port = 32803;
    fsid_device = true;
    allow_set_io_flusher_fail = true;
}

NFSV4 {
    Grace_Period = 10;
}
"""


@dataclass
class Param:
    key: str            # form field key
    param: str          # StorageClass parameter name
    label: str
    kind: str = "entry"
    default: Any = ""
    options: list = field(default_factory=list)
    help: str = ""
    emit: str = "str"   # str (omit blank) | true (emit "true" when ticked) | bool (always "true"/"false")
                        # | enabled (ENABLED when ticked) | false (emit "false" when unticked)
    required: bool = False

    def field(self) -> Field:
        return Field(self.key, self.label, self.kind, default=self.default, options=list(self.options),
                     help=self.help, required=self.required)


@dataclass
class Driver:
    type: str
    provisioner: str
    sc_name: str
    params: list
    binding: str = "WaitForFirstConsumer"
    expansion: bool = True
    mount_options: str = ""
    snapshot: bool = False
    snap_params: list = field(default_factory=list)
    secret: list = field(default_factory=list)          # Fields of the driver Secret section
    install: list = field(default_factory=list)         # Fields of the driver install section
    install_default: bool = False
    validate: Callable | None = None
    extra_docs: Callable | None = None                  # (c) -> [docs] e.g. CephBlockPool
    install_body: Callable | None = None                # (c, ns) -> (lines, helm?, values dict|None)
    notes: list = field(default_factory=list)
    provisioner_fn: Callable | None = None              # (c) -> provisioner (Rook: <ns>.rbd.csi.ceph.com)
    secret_doc: Callable | None = None                  # (c) -> Secret dict or None
    access_note: str = ""


def P(*a, **k):
    return Param(*a, **k)


def _params(c, d: Driver) -> dict:
    out: dict[str, str] = {}
    for p in d.params:
        if p.emit == "str":
            val = c.s(p.key)
            if val:
                out[p.param] = val
            elif p.required:
                c.err(f"{p.label} is required.")
        elif p.emit == "true":
            if c.b(p.key):
                out[p.param] = "true"
        elif p.emit == "false":
            if not c.b(p.key):
                out[p.param] = "false"
        elif p.emit == "bool":
            out[p.param] = "true" if c.b(p.key) else "false"
        elif p.emit == "enabled":
            if c.b(p.key):
                out[p.param] = "ENABLED"
    return out


def _int_param(c, key, label, lo, hi):
    return c.int(key, label, lo=lo, hi=hi)


# --------------------------------------------------------------------------- driver validation / extras
def _v_ebs(c, params):
    t = params.get("type", "gp3")
    iops = _int_param(c, "ebs_iops", "iops", 100, 256000)
    thr = _int_param(c, "ebs_throughput", "throughput", 125, 2000)
    ipg = _int_param(c, "ebs_iops_per_gb", "iopsPerGB", 1, 1000)
    if iops and t not in ("io1", "io2", "gp3"):
        c.err(f"iops only applies to io1 / io2 / gp3 (type is {t}).")
    if t == "gp3" and iops and not 3000 <= iops <= 80000:
        c.err("gp3 iops must be 3000-80000.")
    if t in ("io1", "io2") and not iops and not ipg:
        c.err(f"{t} volumes need iops or iopsPerGB.")
    if iops and ipg:
        c.warn("iops and iopsPerGB are both set; the driver uses iopsPerGB x size and caps it with iops.")
    if thr and t != "gp3":
        c.err("throughput only applies to gp3.")
    if params.get("kmsKeyId") and params.get("encrypted") != "true":
        c.err("kmsKeyId needs encrypted ticked.")
    if params.get("kmsKeyId") and not params["kmsKeyId"].startswith("arn:"):
        c.warn("kmsKeyId should be the full key ARN (arn:aws:kms:<region>:<account>:key/<id>).")
    if t in ("st1", "sc1"):
        c.warn(f"{t} (HDD) volumes must be at least 125 GiB.")


def _v_efs(c, params):
    fs = params.get("fileSystemId", "")
    if fs == "fs-0123456789abcdef0":
        c.warn("fileSystemId is the example value: replace it with your EFS file system id.")
    if fs and not re.match(r"^fs-[0-9a-f]{8,17}$", fs):
        c.err("fileSystemId must look like fs-0123456789abcdef0.")
    a, b = c.int("efs_gid_start", "gidRangeStart", lo=1), c.int("efs_gid_end", "gidRangeEnd", lo=1)
    if (a is None) != (b is None):
        c.err("Set both gidRangeStart and gidRangeEnd (or neither).")
    elif a is not None and b is not None and a > b:
        c.err("gidRangeStart must be <= gidRangeEnd.")
    dp = params.get("directoryPerms", "")
    if dp and not re.match(r"^[0-7]{3,4}$", dp):
        c.err("directoryPerms must be octal, e.g. 700.")
    if c.b("allow_expansion"):
        c.warn("EFS is elastic: allowVolumeExpansion has no effect (requested sizes are ignored).")


def _v_gce(c, params):
    t = params.get("type", "pd-balanced")
    if params.get("provisioned-iops-on-create") and not (t == "pd-extreme" or t.startswith("hyperdisk")):
        c.err("provisioned-iops-on-create only applies to pd-extreme / hyperdisk types.")
    if params.get("provisioned-throughput-on-create") and not t.startswith("hyperdisk"):
        c.err("provisioned-throughput-on-create only applies to hyperdisk types.")
    for k in ("provisioned-iops-on-create", "provisioned-throughput-on-create"):
        if params.get(k) and not params[k].isdigit():
            c.err(f"{k} must be a number.")
    if params.get("replication-type") == "regional-pd":
        if t not in ("pd-standard", "pd-balanced", "pd-ssd"):
            c.err("regional-pd works with pd-standard / pd-balanced / pd-ssd only.")
        if not c.v.get("allowed_topologies"):
            c.warn("Regional PDs replicate across two zones: list them under Allowed topologies "
                   "(topology.gke.io/zone).")


def _v_azdisk(c, params):
    sku = params.get("skuName", "")
    if sku in ("PremiumV2_LRS", "UltraSSD_LRS"):
        if params.get("cachingMode", "None") != "None":
            c.err(f"{sku} supports only cachingMode None.")
    elif params.get("DiskIOPSReadWrite") or params.get("DiskMBpsReadWrite"):
        c.err("DiskIOPSReadWrite / DiskMBpsReadWrite only apply to UltraSSD_LRS / PremiumV2_LRS.")
    for k in ("DiskIOPSReadWrite", "DiskMBpsReadWrite"):
        if params.get(k) and not params[k].isdigit():
            c.err(f"{k} must be a number.")


def _v_azfile(c, params):
    if params.get("protocol") == "nfs":
        if not params.get("skuName", "").startswith("Premium"):
            c.err("NFS Azure file shares need a Premium_LRS / Premium_ZRS skuName.")
        if any(o.startswith(("dir_mode", "file_mode", "uid=", "gid=", "mfsymlinks")) for o in _csv(c.raw("mount_options").replace("\n", ","))):
            c.warn("dir_mode / file_mode / uid / gid / mfsymlinks are SMB mount options; NFS shares ignore them.")


def _v_vsphere(c, params):
    if params.get("datastoreurl") and not params["datastoreurl"].startswith("ds:///"):
        c.err("datastoreurl must look like ds:///vmfs/volumes/<uuid>/.")
    if params.get("datastoreurl") and params.get("storagepolicyname"):
        c.warn("Both datastoreurl and storagepolicyname are set: the datastore must be compatible with the policy.")


def _v_nutanix(c, params):
    st = params.get("storageType", "NutanixVolumes")
    if st == "NutanixVolumes":
        if not params.get("storageContainer"):
            c.err("storageContainer is required for NutanixVolumes.")
        for k in ("nfsServer", "nfsPath", "nfsServerName", "dynamicProv", "squashType"):
            params.pop(k, None)
    else:
        for k in ("storageContainer", "isSegmentedIscsiNetwork", "flashMode", "csi.storage.k8s.io/fstype", "chapAuth"):
            params.pop(k, None)
        if c.b("nx_dynamic"):
            params["dynamicProv"] = "ENABLED"
            params.pop("nfsServer", None)
            params.pop("nfsPath", None)
            if not params.get("nfsServerName"):
                c.err("Dynamic NutanixFiles shares need nfsServerName (the Files server name in Prism).")
        else:
            params.pop("nfsServerName", None)
            params.pop("squashType", None)
            if not params.get("nfsServer") or not params.get("nfsPath"):
                c.err("NutanixFiles (existing share) needs nfsServer and nfsPath.")
    secret_ns = c.s("nx_secret_namespace") or "ntnx-system"
    sname = c.s("nx_secret_name") or "ntnx-secret"
    dns(c, sname, "Secret name")
    if st == "NutanixVolumes" or c.b("nx_dynamic"):
        for op in ("provisioner", "node-publish", "controller-expand"):
            params[f"csi.storage.k8s.io/{op}-secret-name"] = sname
            params[f"csi.storage.k8s.io/{op}-secret-namespace"] = secret_ns


def _nutanix_secret(c):
    if not c.b("nx_create_secret"):
        c.warn(f"The StorageClass references Secret {c.s('nx_secret_namespace') or 'ntnx-system'}/"
               f"{c.s('nx_secret_name') or 'ntnx-secret'}: create it (tick 'Create the Secret') or make sure it exists.")
        return None
    ep, port, user, pw = c.s("nx_prism"), c.s("nx_prism_port") or "9440", c.s("nx_user"), c.raw("nx_password")
    if not (ep and user and pw):
        c.err("The Nutanix Secret needs the Prism Element endpoint, username and password.")
    if not port.isdigit():
        c.err("Prism port must be a number.")
    key = f"{ep}:{port}:{user}:{pw}"
    return {"apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": c.s("nx_secret_name") or "ntnx-secret", "namespace": c.s("nx_secret_namespace") or "ntnx-system"},
            "type": "Opaque", "data": {"key": base64.b64encode(key.encode()).decode()}}


def _v_longhorn(c, params):
    n = c.int("lh_replicas", "numberOfReplicas", lo=1, hi=20)
    if n and n > 3:
        c.warn("More than 3 replicas multiplies disk usage; 3 is the usual maximum.")
    c.int("lh_stale", "staleReplicaTimeout", lo=1)
    if params.get("dataLocality") == "strict-local" and n and n != 1:
        c.err("dataLocality strict-local needs numberOfReplicas 1.")
    c.warn("Longhorn needs open-iscsi (iscsiadm) and nfs-common on every node.")


def _rook_ns(c):
    return c.s("drv_namespace") or "rook-ceph"


def _v_rook(c, params, fs=False):
    dns(c, _rook_ns(c), "Operator namespace", "label")
    cid = params.get("clusterID", "")
    dns(c, cid, "clusterID (CephCluster namespace)", "label")
    if fs:
        dns(c, params.get("fsName", ""), "fsName")
        prov, node = "rook-csi-cephfs-provisioner", "rook-csi-cephfs-node"
    else:
        dns(c, params.get("pool", ""), "pool")
        feats = [f.strip() for f in params.get("imageFeatures", "").split(",") if f.strip()]
        bad = [f for f in feats if f not in ("layering", "exclusive-lock", "object-map", "fast-diff", "deep-flatten")]
        if bad:
            c.err(f"imageFeatures {bad} unknown (layering, exclusive-lock, object-map, fast-diff, deep-flatten).")
        if ("object-map" in feats or "fast-diff" in feats) and "exclusive-lock" not in feats:
            c.err("object-map / fast-diff need exclusive-lock.")
        prov, node = "rook-csi-rbd-provisioner", "rook-csi-rbd-node"
    for op, s in (("provisioner", prov), ("controller-expand", prov), ("node-stage", node)):
        params[f"csi.storage.k8s.io/{op}-secret-name"] = s
        params[f"csi.storage.k8s.io/{op}-secret-namespace"] = cid
    c.warn(f"Needs a running CephCluster in namespace {cid} (the Rook operator alone provides no storage).")


def _rook_pool(c):
    if not c.b("rk_create_pool"):
        return []
    size = c.int("rk_replicas", "replicated.size", lo=1, hi=7) or 3
    fd = c.s("rk_failure_domain") or "host"
    cid = c.s("rk_cluster_id") or "rook-ceph"
    return [{"apiVersion": "ceph.rook.io/v1", "kind": "CephBlockPool",
             "metadata": {"name": c.s("rk_pool") or "replicapool", "namespace": cid},
             "spec": {"failureDomain": fd, "replicated": {"size": size, "requireSafeReplicaSize": size > 1}}}]


def _rook_fs(c):
    if not c.b("rk_create_pool"):
        return []
    size = c.int("rk_replicas", "replicated.size", lo=1, hi=7) or 3
    cid = c.s("rk_cluster_id") or "rook-ceph"
    name = c.s("rk_fs_name") or "myfs"
    pool = c.s("rk_fs_pool") or f"{name}-replicated"
    if not pool.startswith(name + "-"):
        c.err(f"With a generated CephFilesystem the data pool is <fsName>-<pool name>, e.g. {name}-replicated.")
    return [{"apiVersion": "ceph.rook.io/v1", "kind": "CephFilesystem", "metadata": {"name": name, "namespace": cid},
             "spec": {"metadataPool": {"replicated": {"size": size}},
                      "dataPools": [{"name": pool[len(name) + 1:] or "replicated", "replicated": {"size": size}}],
                      "preserveFilesystemOnDelete": True,
                      "metadataServer": {"activeCount": 1, "activeStandby": True}}}]


def _v_openebs(c, params):
    bp = c.s("oe_base_path") or "/var/openebs/local"
    if not bp.startswith("/"):
        c.err("BasePath must be an absolute path.")
    if c.s("binding_mode") != "WaitForFirstConsumer":
        c.err("Local volumes need volumeBindingMode WaitForFirstConsumer.")
    params.clear()


def _openebs_annotations(c):
    bp = c.s("oe_base_path") or "/var/openebs/local"
    return {"openebs.io/cas-type": "local",
            "cas.openebs.io/config": f"- name: StorageType\n  value: hostpath\n- name: BasePath\n  value: {bp}\n"}


def _v_nfs(c, params):
    if c.b("nfs_demo_server"):
        params["server"] = f"nfs-server.{c.s('nfs_demo_namespace') or 'nfs-demo'}.svc.cluster.local"
        params["share"] = "/export"
    if not params.get("server"):
        c.err("NFS server is required (DNS name or IP).")
    sh = params.get("share", "")
    if not sh.startswith("/"):
        c.err("share must be an absolute export path, e.g. / or /exports.")
    mp = params.get("mountPermissions", "")
    if mp and not re.match(r"^0?[0-7]{3,4}$", mp):
        c.err("mountPermissions must be octal, e.g. 0777.")
    if params.get("onDelete", "delete") not in ("delete", "retain", "archive"):
        c.err("onDelete must be delete, retain or archive.")
    if params.get("onDelete") == "delete":
        params.pop("onDelete")


def _nfs_demo_docs(c):
    if not c.b("nfs_demo_server"):
        return []
    ns = c.s("nfs_demo_namespace") or "nfs-demo"
    dns(c, ns, "demo NFS server namespace", "label")
    lab = {"app": "nfs-server"}
    # The image's default ganesha config only exports a read-only pseudo root (the provisioner adds exports per
    # PV it provisions). The demo needs one plain read-write share, so an init container writes vfs.conf first
    # (the server keeps an existing /export/vfs.conf) and creates /export/share, exported as "<server>:/export".
    vfs = NFS_DEMO_GANESHA_CONF
    init = {"name": "init-export", "image": NFS_DEMO_IMAGE, "command": ["sh", "-c",
            "mkdir -p /export/share && chmod 0777 /export/share && cp /config/vfs.conf /export/vfs.conf"],
            "volumeMounts": [{"name": "export", "mountPath": "/export"}, {"name": "config", "mountPath": "/config"}]}
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "nfs-server-ganesha", "namespace": ns, "labels": lab},
         "data": {"vfs.conf": vfs}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "nfs-server", "namespace": ns, "labels": lab},
         "spec": {"selector": lab, "ports": [
             {"name": "nfs", "port": 2049, "protocol": "TCP"}, {"name": "nfs-udp", "port": 2049, "protocol": "UDP"},
             {"name": "nlockmgr", "port": 32803, "protocol": "TCP"}, {"name": "mountd", "port": 20048, "protocol": "TCP"},
             {"name": "rpcbind", "port": 111, "protocol": "TCP"}, {"name": "rpcbind-udp", "port": 111, "protocol": "UDP"},
             {"name": "statd", "port": 662, "protocol": "TCP"}]}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "nfs-server", "namespace": ns, "labels": lab},
         "spec": {"replicas": 1, "selector": {"matchLabels": lab}, "strategy": {"type": "Recreate"},
                  "template": {"metadata": {"labels": lab}, "spec": {"initContainers": [init], "containers": [{
                      "name": "nfs-ganesha", "image": NFS_DEMO_IMAGE,
                      "args": ["-provisioner=k8s-manifest/demo-nfs-unused", "-enable-xfs-quota=false", "-grace-period=10"],
                      "env": [{"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
                              {"name": "SERVICE_NAME", "value": "nfs-server"},
                              {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}],
                      "ports": [{"name": "nfs", "containerPort": 2049}, {"name": "mountd", "containerPort": 20048},
                                {"name": "rpcbind", "containerPort": 111}],
                      "securityContext": {"capabilities": {"add": ["DAC_READ_SEARCH", "SYS_RESOURCE"]}},
                      "volumeMounts": [{"name": "export", "mountPath": "/export"}]}],
                      "volumes": [{"name": "export", "emptyDir": {}},
                                  {"name": "config", "configMap": {"name": "nfs-server-ganesha"}}]}}}},
    ]


def _v_smb(c, params):
    src = params.get("source", "")
    if not re.match(r"^//[^/\s]+/\S+$", src):
        c.err("source must look like //server/share.")
    if params.get("onDelete") == "delete":
        params.pop("onDelete")
    ns = c.s("smb_secret_namespace") or "default"
    name = c.s("smb_secret_name") or "smbcreds"
    dns(c, name, "Secret name")
    for op in ("provisioner", "node-stage"):
        params[f"csi.storage.k8s.io/{op}-secret-name"] = name
        params[f"csi.storage.k8s.io/{op}-secret-namespace"] = ns


def _smb_secret(c):
    if not c.b("smb_create_secret"):
        c.warn(f"The StorageClass references Secret {c.s('smb_secret_namespace') or 'default'}/"
               f"{c.s('smb_secret_name') or 'smbcreds'}: create it (tick 'Create the Secret') or make sure it exists.")
        return None
    user, pw = c.s("smb_user"), c.raw("smb_password")
    if not user or not pw:
        c.err("The SMB Secret needs a username and password.")
    data = {"username": user, "password": pw}
    if c.s("smb_domain"):
        data["domain"] = c.s("smb_domain")
    return {"apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": c.s("smb_secret_name") or "smbcreds", "namespace": c.s("smb_secret_namespace") or "default"},
            "type": "Opaque", "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}


def _v_localpath(c, params):
    if c.s("binding_mode") != "WaitForFirstConsumer":
        c.err("local-path volumes need volumeBindingMode WaitForFirstConsumer.")
    np_ = params.get("nodePath", "")
    if np_ and not np_.startswith("/"):
        c.err("nodePath must be an absolute path that is listed in the provisioner's nodePathMap.")
    pp = params.get("pathPattern", "")
    if pp and not pp.startswith("{{ .PVC.Namespace }}/{{ .PVC.Name }}/"):
        c.warn("pathPattern must start with '{{ .PVC.Namespace }}/{{ .PVC.Name }}/' (or set allowUnsafePathPattern).")


def _localpath_annotations(c):
    vt = c.s("lp_volume_type")
    return {"defaultVolumeType": vt} if vt else {}


def _v_topolvm(c, params):
    if c.s("binding_mode") != "WaitForFirstConsumer":
        c.err("TopoLVM needs volumeBindingMode WaitForFirstConsumer (capacity-aware scheduling).")
    dns(c, params.get("topolvm.io/device-class", ""), "device-class", "label")


# --------------------------------------------------------------------------- installs
def _ebs_install(c, ns):
    vals: dict[str, Any] = {}
    arn = c.s("ebs_role_arn")
    if arn:
        if not arn.startswith("arn:aws:iam::"):
            c.err("IAM role ARN must look like arn:aws:iam::<account>:role/<name>.")
        vals["controller"] = {"serviceAccount": {"annotations": {"eks.amazonaws.com/role-arn": arn}}}
    else:
        c.warn("No IAM role: the controller needs EBS permissions (IRSA role, Pod Identity or the node role).")
    return helm_install("aws-ebs-csi-driver", "aws-ebs-csi-driver", "aws-ebs-csi-driver", "$VERSION", ns,
                        "values.yaml"), True, vals


def _efs_install(c, ns):
    vals: dict[str, Any] = {}
    arn = c.s("efs_role_arn")
    if arn:
        vals["controller"] = {"serviceAccount": {"annotations": {"eks.amazonaws.com/role-arn": arn}}}
    return helm_install("aws-efs-csi-driver", "aws-efs-csi-driver", "aws-efs-csi-driver", "$VERSION", ns,
                        "values.yaml"), True, vals


def _nutanix_install(c, ns):
    vals = {"createSecret": False, "secretName": c.s("nx_secret_name") or "ntnx-secret",
            "volumeClass": False, "fileClass": False, "dynamicFileClass": False}
    return helm_install("nutanix-csi", "nutanix", "nutanix-csi-storage", "$VERSION", ns, "values.yaml"), True, vals


def _longhorn_install(c, ns):
    vals = {"persistence": {"defaultClass": False}}
    n = c.int("lh_replicas", "numberOfReplicas", lo=1, hi=20)
    if n:
        vals["defaultSettings"] = {"defaultReplicaCount": n}
    return helm_install("longhorn", "longhorn", "longhorn", "$VERSION", ns, "values.yaml", timeout="15m"), True, vals


def _rook_install(c, ns):
    vals = {"crds": {"enabled": True}}
    return helm_install("rook-ceph", "rook-release", "rook-ceph", "$VERSION", ns, "values.yaml"), True, vals


def _openebs_install(c, ns):
    vals = {"hostpathClass": {"enabled": False}}
    return helm_install("openebs-localpv", "openebs-localpv", "localpv-provisioner", "$VERSION", ns,
                        "values.yaml"), True, vals


def _nfs_install(c, ns):
    vals: dict[str, Any] = {"externalSnapshotter": {"enabled": False}}
    return helm_install("csi-driver-nfs", "csi-driver-nfs", "csi-driver-nfs", "$VERSION", ns, "values.yaml"), True, vals


def _smb_install(c, ns):
    return helm_install("csi-driver-smb", "csi-driver-smb", "csi-driver-smb", "$VERSION", ns, None), True, None


def _localpath_install(c, ns):
    lines = ['"$KUBECTL" apply -f "https://raw.githubusercontent.com/rancher/local-path-provisioner/$VERSION/deploy/local-path-storage.yaml"',
             '"$KUBECTL" -n local-path-storage rollout status deployment/local-path-provisioner --timeout=5m']
    if (c.s("name") or "") == "local-path":
        lines.append("# the upstream manifest also creates a 'local-path' class: replace it with this one")
        lines.append('"$KUBECTL" delete storageclass local-path --ignore-not-found')
    return lines, False, None


def _topolvm_install(c, ns):
    dc = c.s("tv_device_class") or "ssd"
    vals = {"lvmd": {"deviceClasses": [{"name": dc, "volume-group": c.s("tv_vg") or "myvg1", "default": True,
                                        "spare-gb": c.int("tv_spare", "spare-gb", lo=0) or 10}]},
            "storageClasses": [], "cert-manager": {"enabled": c.b("tv_cert_manager")}}
    if not c.b("tv_cert_manager"):
        c.warn("TopoLVM's webhook needs cert-manager: install it first or tick 'Install cert-manager subchart'.")
    return helm_install("topolvm", "topolvm", "topolvm", "$VERSION", ns, "values.yaml"), True, vals


def _install_fields(ns, ver_key, extra=()):
    return [Field("install_driver", "Install / upgrade the driver (install.sh)", "check", default=None),
            Field("drv_namespace", "Driver namespace", default=ns),
            Field("drv_version", "Chart / release version", "ecombo", default=VERSIONS[ver_key],
                  options=[VERSIONS[ver_key]])] + list(extra)


def _secret_fields(prefix, name, ns, extra):
    return [Field(f"{prefix}_create_secret", "Create the Secret (secret.yaml, 0600)", "check", default=False),
            Field(f"{prefix}_secret_name", "Secret name", default=name),
            Field(f"{prefix}_secret_namespace", "Secret namespace", default=ns)] + list(extra)


# --------------------------------------------------------------------------- the drivers
DRIVERS: dict[str, Driver] = {}


def _d(d: Driver, ns=None, ver=None, install_extra=()):
    if d.install_body:
        d.install = _install_fields(ns, ver, install_extra)
        for f in d.install:
            if f.key == "install_driver":
                f.default = d.install_default
    DRIVERS[d.type] = d


_d(Driver("AWS EBS", "ebs.csi.aws.com", "ebs-gp3", [
    P("ebs_type", "type", "type", "combo", "gp3", ["gp3", "gp2", "io1", "io2", "st1", "sc1", "standard"]),
    P("ebs_iops", "iops", "iops", help="io1/io2/gp3; gp3 3000-80000"),
    P("ebs_iops_per_gb", "iopsPerGB", "iopsPerGB", help="io1/io2/gp3"),
    P("ebs_throughput", "throughput", "throughput (MiB/s)", help="gp3 only, 125-2000"),
    P("ebs_encrypted", "encrypted", "encrypted", "check", True, emit="true"),
    P("ebs_kms", "kmsKeyId", "kmsKeyId", help="full KMS key ARN; blank = aws/ebs default key"),
    P("ebs_fstype", "csi.storage.k8s.io/fstype", "fsType", "combo", "ext4", ["ext4", "xfs", "ext3"]),
    P("ebs_auto_iops", "allowAutoIOPSPerGBIncrease", "allowAutoIOPSPerGBIncrease", "check", False, emit="true"),
], snapshot=True, validate=_v_ebs, install_body=_ebs_install,
    notes=["EKS: the aws-ebs-csi-driver EKS add-on is the easiest install (eksctl create addon)."]),
    "kube-system", "aws-ebs-csi-driver",
    [Field("ebs_role_arn", "Controller IAM role ARN (IRSA)", default="", help="arn:aws:iam::<account>:role/<name>")])

_d(Driver("AWS EFS", "efs.csi.aws.com", "efs-sc", [
    P("efs_mode", "provisioningMode", "provisioningMode", "combo", "efs-ap", ["efs-ap"]),
    P("efs_fs_id", "fileSystemId", "fileSystemId", required=True, default="fs-0123456789abcdef0", help="fs-0123456789abcdef0"),
    P("efs_perms", "directoryPerms", "directoryPerms", default="700"),
    P("efs_uid", "uid", "uid", help="POSIX uid of the access point root; blank = from gidRange"),
    P("efs_gid", "gid", "gid"),
    P("efs_gid_start", "gidRangeStart", "gidRangeStart", help="default 50000"),
    P("efs_gid_end", "gidRangeEnd", "gidRangeEnd", help="default 7000000"),
    P("efs_base_path", "basePath", "basePath", default="/dynamic_provisioning"),
    P("efs_sub_path", "subPathPattern", "subPathPattern", help="e.g. ${.PVC.namespace}/${.PVC.name}"),
    P("efs_unique", "ensureUniqueDirectory", "ensureUniqueDirectory", "check", True, emit="false"),
    P("efs_reuse_ap", "reuseAccessPoint", "reuseAccessPoint", "check", False, emit="true"),
    P("efs_az", "az", "az (One Zone)", help="One Zone file systems: the AZ name, e.g. us-east-1a"),
], binding="Immediate", expansion=False, validate=_v_efs, install_body=_efs_install,
    access_note="ReadWriteMany"), "kube-system", "aws-efs-csi-driver",
    [Field("efs_role_arn", "Controller IAM role ARN (IRSA)", default="", help="arn:aws:iam::<account>:role/<name>")])

_d(Driver("GCE PD", "pd.csi.storage.gke.io", "pd-balanced", [
    P("gce_type", "type", "type", "combo", "pd-balanced",
      ["pd-balanced", "pd-standard", "pd-ssd", "pd-extreme", "hyperdisk-balanced", "hyperdisk-throughput",
       "hyperdisk-extreme"]),
    P("gce_replication", "replication-type", "replication-type", "combo", "none", ["none", "regional-pd"]),
    P("gce_iops", "provisioned-iops-on-create", "provisioned-iops-on-create", help="pd-extreme / hyperdisk"),
    P("gce_throughput", "provisioned-throughput-on-create", "provisioned-throughput-on-create", help="hyperdisk, MiB/s"),
    P("gce_kms", "disk-encryption-kms-key", "disk-encryption-kms-key",
      help="projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>"),
    P("gce_labels", "labels", "labels", help="key1=value1,key2=value2"),
    P("gce_fstype", "csi.storage.k8s.io/fstype", "fsType", "combo", "ext4", FSTYPES),
], snapshot=True, validate=_v_gce, notes=["GKE installs this driver; nothing to install."]))

_d(Driver("Azure Disk", "disk.csi.azure.com", "managed-premium-zrs", [
    P("azd_sku", "skuName", "skuName", "combo", "Premium_ZRS",
      ["Premium_ZRS", "StandardSSD_ZRS", "Premium_LRS", "StandardSSD_LRS", "Standard_LRS", "PremiumV2_LRS", "UltraSSD_LRS"]),
    P("azd_caching", "cachingMode", "cachingMode", "combo", "", ["", "None", "ReadOnly"]),
    P("azd_fstype", "fsType", "fsType", "combo", "ext4", ["ext4", "xfs"]),
    P("azd_iops", "DiskIOPSReadWrite", "DiskIOPSReadWrite", help="UltraSSD_LRS / PremiumV2_LRS"),
    P("azd_mbps", "DiskMBpsReadWrite", "DiskMBpsReadWrite", help="UltraSSD_LRS / PremiumV2_LRS"),
    P("azd_network", "networkAccessPolicy", "networkAccessPolicy", "combo", "", ["", "AllowAll", "DenyAll", "AllowPrivate"]),
    P("azd_des", "diskEncryptionSetID", "diskEncryptionSetID", help="/subscriptions/.../diskEncryptionSets/<name>"),
    P("azd_rg", "resourceGroup", "resourceGroup", help="blank = the node resource group"),
    P("azd_tags", "tags", "tags", help="key1=val1,key2=val2"),
    P("azd_bursting", "enableBursting", "enableBursting", "check", False, emit="true"),
], snapshot=True, validate=_v_azdisk, snap_params=[("incremental", "true")],
    notes=["AKS installs this driver; nothing to install."]))

_d(Driver("Azure File", "file.csi.azure.com", "azurefile-csi", [
    P("azf_sku", "skuName", "skuName", "combo", "Standard_LRS",
      ["Standard_LRS", "Standard_ZRS", "Standard_GRS", "Standard_RAGRS", "Premium_LRS", "Premium_ZRS"]),
    P("azf_protocol", "protocol", "protocol", "combo", "smb", ["smb", "nfs"]),
    P("azf_share", "shareName", "shareName", help="blank = a new share per volume"),
    P("azf_account", "storageAccount", "storageAccount", help="blank = the driver picks / creates one"),
    P("azf_rg", "resourceGroup", "resourceGroup"),
    P("azf_endpoint", "networkEndpointType", "networkEndpointType", "combo", "", ["", "privateEndpoint", "serviceEndpoint"]),
    P("azf_large", "enableLargeFileShares", "enableLargeFileShares", "check", False, emit="true"),
], binding="Immediate",
    mount_options="dir_mode=0777,file_mode=0777,uid=0,gid=0,mfsymlinks,cache=strict,actimeo=30,nobrl",
    snapshot=True, validate=_v_azfile, notes=["AKS installs this driver; nothing to install."],
    access_note="ReadWriteMany"))

_d(Driver("vSphere", "csi.vsphere.vmware.com", "vsphere-sc", [
    P("vs_policy", "storagepolicyname", "storagepolicyname", default="vSAN Default Storage Policy"),
    P("vs_datastore", "datastoreurl", "datastoreurl", help="ds:///vmfs/volumes/<uuid>/"),
    P("vs_fstype", "csi.storage.k8s.io/fstype", "fsType", "combo", "ext4", FSTYPES),
], snapshot=True, validate=_v_vsphere,
    notes=["Install the vSphere CSI driver with your vCenter credentials (csi-vsphere.conf) per VMware's docs."]))

_d(Driver("Nutanix", "csi.nutanix.com", "nutanix-volume", [
    P("nx_storage_type", "storageType", "storageType", "combo", "NutanixVolumes", ["NutanixVolumes", "NutanixFiles"]),
    P("nx_container", "storageContainer", "storageContainer (Volumes)", default="default-container"),
    P("nx_fstype", "csi.storage.k8s.io/fstype", "fsType (Volumes)", "combo", "xfs", FSTYPES),
    P("nx_segmented", "isSegmentedIscsiNetwork", "isSegmentedIscsiNetwork (Volumes)", "check", False, emit="bool"),
    P("nx_flash", "flashMode", "flashMode (Volumes)", "check", False, emit="enabled"),
    P("nx_chap", "chapAuth", "chapAuth (Volumes)", "check", False, emit="enabled"),
    P("nx_description", "description", "description", default=""),
    P("nx_nfs_server", "nfsServer", "nfsServer (Files, existing share)"),
    P("nx_nfs_path", "nfsPath", "nfsPath (Files, existing share)"),
    P("nx_files_server", "nfsServerName", "nfsServerName (Files, dynamic)"),
    P("nx_squash", "squashType", "squashType (Files, dynamic)", "combo", "root-squash", ["root-squash", "none", "all-squash"]),
], snapshot=True, validate=_v_nutanix, secret_doc=_nutanix_secret, install_body=_nutanix_install,
    install_default=False, secret=_secret_fields("nx", "ntnx-secret", "ntnx-system", [
        Field("nx_prism", "Prism Element endpoint", default="", help="FQDN or cluster virtual IP"),
        Field("nx_prism_port", "Prism port", default="9440"),
        Field("nx_user", "Username", default="admin"),
        Field("nx_password", "Password", default=""),
        Field("nx_dynamic", "Files: dynamic shares (dynamicProv)", "check", default=False),
    ])), "ntnx-system", "nutanix-csi-storage")

_d(Driver("Longhorn", "driver.longhorn.io", "longhorn-replicated", [
    P("lh_replicas", "numberOfReplicas", "numberOfReplicas", default="3"),
    P("lh_stale", "staleReplicaTimeout", "staleReplicaTimeout (minutes)", default="2880"),
    P("lh_locality", "dataLocality", "dataLocality", "combo", "disabled", ["disabled", "best-effort", "strict-local"]),
    P("lh_fstype", "fsType", "fsType", "combo", "ext4", FSTYPES),
    P("lh_engine", "dataEngine", "dataEngine", "combo", "v1", ["v1", "v2"]),
    P("lh_disk_selector", "diskSelector", "diskSelector", help="disk tags, comma-separated"),
    P("lh_node_selector", "nodeSelector", "nodeSelector", help="node tags, comma-separated"),
], binding="Immediate", snapshot=True, validate=_v_longhorn, install_body=_longhorn_install, install_default=True),
    "longhorn-system", "longhorn")

_d(Driver("Rook-Ceph RBD", "rook-ceph.rbd.csi.ceph.com", "rook-ceph-block", [
    P("rk_cluster_id", "clusterID", "clusterID (CephCluster namespace)", default="rook-ceph"),
    P("rk_pool", "pool", "pool", default="replicapool"),
    P("rk_image_format", "imageFormat", "imageFormat", "combo", "2", ["2"]),
    P("rk_features", "imageFeatures", "imageFeatures", default="layering", help="layering,exclusive-lock,object-map,fast-diff,deep-flatten"),
    P("rk_fstype", "csi.storage.k8s.io/fstype", "fsType", "combo", "ext4", FSTYPES),
], binding="Immediate", snapshot=True, validate=_v_rook, extra_docs=_rook_pool, install_body=_rook_install,
    install_default=True, provisioner_fn=lambda c: f"{_rook_ns(c)}.rbd.csi.ceph.com"),
    "rook-ceph", "rook-ceph", [
        Field("rk_create_pool", "Create the CephBlockPool (ceph-pool.yaml)", "check", default=True),
        Field("rk_replicas", "Pool replicas (replicated.size)", default="3"),
        Field("rk_failure_domain", "failureDomain", "combo", default="host", options=["host", "osd", "zone", "rack"])])

_d(Driver("Rook-Ceph CephFS", "rook-ceph.cephfs.csi.ceph.com", "rook-cephfs", [
    P("rk_cluster_id", "clusterID", "clusterID (CephCluster namespace)", default="rook-ceph"),
    P("rk_fs_name", "fsName", "fsName", default="myfs"),
    P("rk_fs_pool", "pool", "data pool", default="myfs-replicated"),
], binding="Immediate", snapshot=True, validate=lambda c, p: _v_rook(c, p, fs=True), extra_docs=_rook_fs,
    install_body=_rook_install, install_default=True,
    provisioner_fn=lambda c: f"{_rook_ns(c)}.cephfs.csi.ceph.com", access_note="ReadWriteMany"),
    "rook-ceph", "rook-ceph", [
        Field("rk_create_pool", "Create the CephFilesystem (ceph-pool.yaml)", "check", default=True),
        Field("rk_replicas", "Pool replicas (replicated.size)", default="3")])

_d(Driver("OpenEBS LocalPV", "openebs.io/local", "openebs-hostpath", [
    P("oe_base_path", "BasePath", "BasePath", default="/var/openebs/local", emit="none"),
], validate=_v_openebs, install_body=_openebs_install, install_default=True, expansion=False),
    "openebs", "openebs-localpv")

_d(Driver("NFS CSI", "nfs.csi.k8s.io", "nfs-csi", [
    P("nfs_server", "server", "server", default="nfs-server.example.com", help="DNS name or IP of the NFS server"),
    P("nfs_share", "share", "share", default="/exports"),
    P("nfs_subdir", "subDir", "subDir", help="e.g. ${pvc.metadata.namespace}/${pvc.metadata.name}; blank = one dir per PV"),
    P("nfs_perms", "mountPermissions", "mountPermissions", help="octal, e.g. 0777; blank = 0 (no chmod)"),
    P("nfs_on_delete", "onDelete", "onDelete", "combo", "delete", ["delete", "retain", "archive"]),
], binding="Immediate", mount_options="nfsvers=4.1", snapshot=True, validate=_v_nfs, extra_docs=_nfs_demo_docs,
    install_body=_nfs_install, install_default=True, access_note="ReadWriteMany"),
    "kube-system", "csi-driver-nfs",
    [Field("nfs_demo_server", "Deploy a demo NFS server (NFS-Ganesha, testing only)", "check", default=False),
     Field("nfs_demo_namespace", "Demo server namespace", default="nfs-demo")])

_d(Driver("SMB CSI", "smb.csi.k8s.io", "smb", [
    P("smb_source", "source", "source", default="//smb-server.default.svc.cluster.local/share", required=True),
    P("smb_subdir", "subDir", "subDir"),
    P("smb_on_delete", "onDelete", "onDelete", "combo", "delete", ["delete", "retain", "archive"]),
], binding="Immediate",
    mount_options="dir_mode=0777,file_mode=0777,uid=1001,gid=1001,noperm,mfsymlinks,cache=strict,noserverino",
    validate=_v_smb, secret_doc=_smb_secret, install_body=_smb_install, install_default=True,
    secret=_secret_fields("smb", "smbcreds", "default", [
        Field("smb_user", "Username", default=""), Field("smb_password", "Password", default=""),
        Field("smb_domain", "Domain", default="")]), access_note="ReadWriteMany"),
    "kube-system", "csi-driver-smb")

_d(Driver("local-path", "rancher.io/local-path", "local-path-wffc", [
    P("lp_node_path", "nodePath", "nodePath", help="one of the provisioner's nodePathMap paths; blank = any"),
    P("lp_path_pattern", "pathPattern", "pathPattern", help="{{ .PVC.Namespace }}/{{ .PVC.Name }}/... (Go template)"),
    P("lp_volume_type", "defaultVolumeType", "defaultVolumeType", "combo", "", ["", "hostPath", "local"], emit="none"),
], validate=_v_localpath, install_body=_localpath_install, install_default=True, expansion=False),
    "local-path-storage", "local-path")

_d(Driver("TopoLVM", "topolvm.io", "topolvm-provisioner", [
    P("tv_device_class", "topolvm.io/device-class", "device-class", default="ssd"),
    P("tv_fstype", "csi.storage.k8s.io/fstype", "fsType", "combo", "xfs", ["xfs", "ext4", "btrfs"]),
], validate=_v_topolvm, install_body=_topolvm_install, install_default=False),
    "topolvm-system", "topolvm",
    [Field("tv_vg", "LVM volume group (lvmd)", default="myvg1"),
     Field("tv_spare", "spare-gb", default="10"),
     Field("tv_cert_manager", "Install cert-manager subchart", "check", default=False)])

VER_KEYS = {"AWS EBS": "aws-ebs-csi-driver", "AWS EFS": "aws-efs-csi-driver", "Nutanix": "nutanix-csi-storage",
            "Longhorn": "longhorn", "Rook-Ceph RBD": "rook-ceph", "Rook-Ceph CephFS": "rook-ceph",
            "OpenEBS LocalPV": "openebs-localpv", "NFS CSI": "csi-driver-nfs", "SMB CSI": "csi-driver-smb",
            "local-path": "local-path", "TopoLVM": "topolvm"}
SNAP_EXTRA_FIELDS = {
    "Longhorn": [Field("lh_snap_type", "Snapshot type", "combo", default="snap", options=["snap", "bak"])],
}


def schema_for(d: Driver) -> list:
    sc = [
        Field("name", "StorageClass name", default=d.sc_name, required=True),
        Field("default_class", "Default StorageClass", "check", default=False),
        Field("reclaim_policy", "reclaimPolicy", "combo", default="Delete", options=gen.RECLAIM_POLICIES),
        Field("binding_mode", "volumeBindingMode", "combo", default=d.binding, options=gen.BINDING_MODES),
        Field("allow_expansion", "allowVolumeExpansion", "check", default=d.expansion),
        Field("mount_options", "mountOptions", "text", default=d.mount_options.replace(",", "\n"), height=2),
        Field("labels", "Labels", "text", default="", height=2),
    ]
    out = [("StorageClass", sc), (f"{d.type} parameters", [p.field() for p in d.params])]
    if d.secret:
        out.append(("Driver secret", d.secret))
    if d.snapshot:
        out.append(("VolumeSnapshotClass", [
            Field("snap_create", "Create a VolumeSnapshotClass", "check", default=False),
            Field("snap_name", "VolumeSnapshotClass name", default=f"{d.sc_name}-snap"),
            Field("snap_deletion", "deletionPolicy", "combo", default="Delete", options=gen.DELETION_POLICIES),
            Field("snap_default", "Default VolumeSnapshotClass", "check", default=False),
            Field("snap_install_crds", "Install snapshot CRDs + controller", "check", default=False),
        ] + SNAP_EXTRA_FIELDS.get(d.type, [])))
    if d.install:
        out.append(("Driver install", d.install))
    out.append(("Allowed topologies", [gen._table("allowed_topologies", "allowedTopologies", ["key", "values"],
                                                  "restrict where volumes can be provisioned (zones / nodes)")]))
    out.append(("Extra parameters", [Field("extra_params", "Additional parameters", "text", default="", height=3,
                                           help="key=value per line; added to (and override) the form parameters")]))
    return out


def _snap_params(c, d: Driver, params: dict) -> dict:
    sp = dict(d.snap_params)
    if d.type == "Nutanix":
        sp["storageType"] = "NutanixVolumes"
        sp["csi.storage.k8s.io/snapshotter-secret-name"] = c.s("nx_secret_name") or "ntnx-secret"
        sp["csi.storage.k8s.io/snapshotter-secret-namespace"] = c.s("nx_secret_namespace") or "ntnx-system"
    elif d.type.startswith("Rook"):
        cid = params.get("clusterID", "rook-ceph")
        sec = "rook-csi-cephfs-provisioner" if "CephFS" in d.type else "rook-csi-rbd-provisioner"
        sp.update({"clusterID": cid, "csi.storage.k8s.io/snapshotter-secret-name": sec,
                   "csi.storage.k8s.io/snapshotter-secret-namespace": cid})
    elif d.type == "Longhorn":
        sp["type"] = c.s("lh_snap_type") or "snap"
    return sp


def build(ctype: str, c) -> ConfigResult:
    d = DRIVERS[ctype]
    params = _params(c, d)
    if d.validate:
        d.validate(c, params)
    prov = d.provisioner_fn(c) if d.provisioner_fn else d.provisioner
    sc = gen.storage_class_doc(c, prov, params)
    ann = {}
    if d.type == "OpenEBS LocalPV":
        ann = _openebs_annotations(c)
    elif d.type == "local-path":
        ann = _localpath_annotations(c)
    if ann:
        sc["metadata"].setdefault("annotations", {}).update(ann)
    if c.b("default_class"):
        c.warn("Default StorageClass: make sure no other class is also marked default "
               "(kubectl get sc; e.g. kind's 'standard', k3s' 'local-path').")
    files: list[OutFile] = []
    extra = d.extra_docs(c) if d.extra_docs else []
    crs = [x for x in extra if x["kind"].startswith("Ceph")]
    demo = [x for x in extra if x not in crs]
    if demo:
        files.append(yaml_file("nfs-server.yaml", demo, header="# In-cluster demo NFS server (NFS-Ganesha in "
                               "userspace, emptyDir storage) - for testing only\n"))
    secret = d.secret_doc(c) if d.secret_doc else None
    if secret:
        files.append(yaml_file("secret.yaml", [secret], mode=0o600))
    if crs:
        files.append(yaml_file("ceph-pool.yaml", crs, header="# Ceph pool / filesystem for the StorageClass "
                               "(needs the CephCluster in the same namespace)\n"))
    files.append(yaml_file("storageclass.yaml", [sc]))
    snap = None
    if d.snapshot and c.b("snap_create"):
        snap = gen.snapshot_class_doc(c, c.s("snap_name"), prov, c.s("snap_deletion") or "Delete",
                                      c.b("snap_default"), _snap_params(c, d, params))
        files.append(yaml_file("volumesnapshotclass.yaml", [snap]))
    if d.snapshot and c.b("snap_install_crds") and not c.b("snap_create"):
        c.warn("Snapshot CRDs are installed but no VolumeSnapshotClass is created.")
    # install.sh
    body: list[str] = []
    helm = False
    if c.b("snap_install_crds") and d.snapshot:
        body += snapshot_install_lines(VERSIONS["snapshotter"])
    if d.install and c.b("install_driver"):
        ns = c.s("drv_namespace")
        dns(c, ns, "Driver namespace", "label")
        ver_key = VER_KEYS[d.type]
        v = version(c, "drv_version", "Chart / release version", v_prefix=(True if ver_key == "local-path" else False))
        lines, helm, vals = d.install_body(c, ns)
        if vals is not None:
            files.append(OutFile("values.yaml", f"# {d.type} driver Helm values (chart version {v})\n" + to_yaml(vals)))
        body += [f'VERSION="{v}"', f"# {d.type} driver"] + lines + [""]
    if demo:
        ns = c.s("nfs_demo_namespace") or "nfs-demo"
        body += ['"$KUBECTL" apply -f nfs-server.yaml',
                 f'"$KUBECTL" -n {ns} rollout status deployment/nfs-server --timeout=5m', ""]
    if secret:
        sns = secret["metadata"]["namespace"]
        body.append(f'"$KUBECTL" create namespace {sns} --dry-run=client -o yaml | "$KUBECTL" apply -f -')
        body.append('"$KUBECTL" apply -f secret.yaml')
    if crs:
        body.append('"$KUBECTL" apply -f ceph-pool.yaml')
    body.append('"$KUBECTL" apply -f storageclass.yaml')
    if snap:
        body.append('"$KUBECTL" apply -f volumesnapshotclass.yaml')
    if c.b("default_class"):
        body.append(f'"$KUBECTL" get storageclass  # check that {c.s("name")} is the only (default) class')
    comments = list(d.notes) + [f"provisioner: {prov}"]
    if d.access_note:
        comments.append(f"supports {d.access_note} volumes")
    files.append(script(f"{d.type}: StorageClass {c.s('name')}", comments, body, helm=helm))
    return ConfigResult(None, c.errors, c.warnings, files=files, info=f"StorageClass {c.s('name')} ({prov})")


TYPES = list(DRIVERS)
COLLAPSED = {"Allowed topologies", "Extra parameters", "VolumeSnapshotClass", "Driver secret"}
