#!/usr/bin/env python3
"""
Tests for the Cluster config platform modules (clusterplatforms/*): minikube, k0s, MicroK8s, Talos,
Kubespray, Cluster API, OpenShift, EKS, GKE, AKS.

  python3 tests/test_cluster_platforms.py             generator checks only (no tools needed)
  ... --tools      validate the outputs with the real CLIs (all offline; no cloud credentials):
                   minikube (config set round-trip, start --dry-run, --help, addons), k0s config validate,
                   talosctl gen config + validate --strict, eksctl JSON schema, gcloud / az --help flag+enum
                   checks, openshift-install create manifests, ansible-inventory + kubespray variable names;
                   checks whose tool or test data is not present are skipped
  ... --minikube   really boot minikube (docker driver) from a generated start script, check it, delete it
  ... --capi       kind management cluster + CAPI/CAPD v1beta2 CRDs + kubectl apply --dry-run=server, delete
Docker needs root on this box: set KIND_SUDO=1 (used for minikube / kind).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cluster_config_gen as ccg  # noqa: E402
from clusterplatforms.common import outputs, render, parse_script_flags  # noqa: E402
from clusterplatforms import minikube as MK, kubespray as KSP  # noqa: E402

OUT = os.path.join(HERE, "out-platforms")
FAILS: list[str] = []
COUNT = [0]
SUDO = ["sudo", "-E"] if os.environ.get("KIND_SUDO") else []


def check(cond, msg):
    COUNT[0] += 1
    if not cond:
        FAILS.append(msg)
        print("  FAIL", msg)
    return bool(cond)


def gen(ctype, **over):
    return ccg.generate(ctype, over)


def write(case, res) -> str:
    d = os.path.join(OUT, case)
    shutil.rmtree(d, ignore_errors=True)
    for o in outputs(res):
        p = os.path.join(d, o.name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(o.text)
        os.chmod(p, o.mode or 0o644)
    return d


def files_of(res):
    return {o.name: o.text for o in outputs(res)}


def _bin(name):
    for d in [os.path.join(ROOT, "bin"), "/workspace/bin", "/workspace/tools/google-cloud-sdk/bin",
              "/workspace/tools/azvenv/bin"]:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return shutil.which(name)


def _run(cmd, timeout=300, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


# ----------------------------------------------------------------------------------------------
# positive cases: name -> (type, overrides, assert fn(files, res))
CASES: dict = {}


def case(name, ctype, **over):
    def deco(fn):
        CASES[name] = (ctype, over, fn)
        return fn
    return deco


def _flags(text, word=None):
    return dict(parse_script_flags(text, word))


# ---- minikube
@case("minikube-defaults", "minikube")
def _(f, res):
    cfg = json.loads(f["config.json"])
    check(set(cfg) <= set(MK.CONFIG_KEYS), f"minikube config keys {set(cfg)}")
    check(all(isinstance(v, str) for v in cfg.values()), "minikube config.json values are strings")
    fl = _flags(f["start-minikube.sh"], "start")
    check(fl.get("--driver") == "docker" and fl.get("--cpus") == "2", f"minikube flags {fl}")
    check(cfg["memory"] == fl["--memory"] and cfg["driver"] == fl["--driver"], "config.json == script values")


@case("minikube-full", "minikube", mk_profile="dev", mk_cpus="4", mk_memory="6g", mk_disk_size="40g", mk_nodes="3",
      mk_ha=True, mk_k8s_version="v1.36.4", mk_runtime="containerd", mk_cni="calico",
      mk_addons="metrics-server\ningress", mk_feature_gates="InPlacePodVerticalScaling=true",
      mk_extra_config="kubelet.max-pods=150\napiserver.v=2", mk_ports="8080:80", mk_listen_address="127.0.0.1",
      mk_subnet="192.168.200.0/24", mk_insecure_registry="10.0.0.0/24", mk_wait="all", mk_keep_context=True)
def _(f, res):
    cfg = json.loads(f["config.json"])
    check(cfg.get("profile") == "dev" and cfg.get("container-runtime") == "containerd", f"minikube full cfg {cfg}")
    sh = f["start-minikube.sh"]
    fl = parse_script_flags(sh, "start")
    names = [k for k, _ in fl]
    for want in ("--ha", "--nodes", "--cni", "--addons", "--extra-config", "--ports", "--listen-address", "--subnet",
                 "--feature-gates", "--wait", "--keep-context"):
        check(want in names, f"minikube full script has {want}: {names}")
    check(names.count("--extra-config") == 2, "--extra-config repeated per component setting")
    check(dict(fl)["--addons"] == "metrics-server,ingress", "--addons comma list")


@case("minikube-kvm2-config", "minikube", mk_output="config.json", mk_driver="kvm2", mk_cpus="2", mk_memory="4g",
      mk_cni="flannel")
def _(f, res):
    check(list(f) == ["config.json"], f"config-only output {list(f)}")
    check(any("cannot hold cni" in w for w in res.warnings),
          f"config-only warns about dropped flags: {res.warnings}")


@case("minikube-static-ip", "minikube", mk_output="start script (start-minikube.sh)", mk_profile="static",
      mk_static_ip="192.168.200.200")
def _(f, res):
    check(list(f) == ["start-minikube.sh"], "script-only output")
    check(_flags(f["start-minikube.sh"], "start").get("--static-ip") == "192.168.200.200", "static ip flag")


# ---- k0s
_K0S_HOSTS = [{"role": "controller", "address": f"10.0.0.1{i}", "user": "root", "port": "22", "key_path": "~/.ssh/id_ed25519",
               "hostname": "", "install_flags": ""} for i in range(3)] + \
             [{"role": "worker", "address": "10.0.0.20", "user": "ubuntu", "port": "2222", "key_path": "", "hostname": "w1",
               "install_flags": "--labels=tier=apps"}]


@case("k0s-ha-both", "k0s", k0_output="both", k0_version="v1.36.4+k0s.1", k0s_hosts=_K0S_HOSTS, k0_provider="calico",
      k0_proxy_mode="ipvs", k0_nllb=True, k0_api_sans="k0s.example.com", k0_telemetry=False)
def _(f, res):
    k = yaml.safe_load(f["k0sctl.yaml"])
    check(k["apiVersion"] == "k0sctl.k0sproject.io/v1beta1" and len(k["spec"]["hosts"]) == 4, "k0sctl hosts")
    check(k["spec"]["k0s"]["version"] == "v1.36.4+k0s.1", "k0s version")
    c = yaml.safe_load(f["k0s.yaml"])
    check(c["kind"] == "ClusterConfig" and c["spec"]["network"]["provider"] == "calico", "k0s.yaml ClusterConfig")


@case("k0s-single-kine", "k0s", k0_output="k0s.yaml (ClusterConfig only)", k0_storage="kine",
      k0_kine_ds="sqlite:///var/lib/k0s/db/state.db?mode=rwc&_journal=WAL",
      k0s_hosts=[{"role": "single", "address": "10.0.0.5", "user": "root", "port": "22", "key_path": "", "hostname": "",
                  "install_flags": ""}], k0_proxy_disabled=True)
def _(f, res):
    c = yaml.safe_load(f["k0s.yaml"])
    check(c["spec"]["storage"]["type"] == "kine", "kine storage")


# ---- MicroK8s
@case("microk8s-defaults", "MicroK8s")
def _(f, res):
    d = yaml.safe_load(f["microk8s-config.yaml"])
    check(d["version"] == "0.1.0" and d["addons"], f"microk8s {d}")


@case("microk8s-full", "MicroK8s", m8_version="0.2.0", m8_addons="dns\nmetallb:10.64.140.43-10.64.140.49\ningress",
      m8_disable="ha-cluster", m8_apiserver_args="--event-ttl=2h\n--audit-log-maxage=30",
      m8_kubelet_args="--max-pods=150", m8_sans="10.10.10.10,k8s.example.com",
      m8_registry_mirrors="docker.io=https://mirror.example.com", m8_cluster_token="a" * 32)
def _(f, res):
    d = yaml.safe_load(f["microk8s-config.yaml"])
    check(d["version"] == "0.2.0" and "persistentClusterToken" in d, "microk8s full")
    check(any(a.get("name") == "metallb" and a.get("args") for a in d["addons"]), "metallb args")


# ---- Talos
@case("talos-defaults", "Talos")
def _(f, res):
    fl = _flags(f["talos-gen-config.sh"], "gen config")
    check(fl.get("--kubernetes-version") and fl.get("--with-docs") == "false" and fl.get("--output") == "_out",
          f"talos flags {fl}")


@case("talos-patched", "Talos", tl_cluster_name="lab2", tl_endpoint="https://10.5.0.2:6443",
      tl_sans="10.5.0.2,talos.example.com", tl_pod_subnets="10.200.0.0/16", tl_service_subnets="10.201.0.0/16",
      tl_cni="none (bring your own: Cilium, Calico...)", tl_proxy_disabled=True, tl_nameservers="1.1.1.1,9.9.9.9",
      tl_ntp="time.cloudflare.com", tl_schedule_cp=True, tl_node_labels="tier=apps",
      tl_node_taints="dedicated=gpu:NoSchedule", tl_max_pods="200", tl_kubelet_args="rotate-server-certificates=true",
      tl_kubespan=True, tl_registry_mirrors="docker.io=https://mirror.gcr.io", tl_dns_domain="lab.local")
def _(f, res):
    check({"talos-patch.yaml", "talos-patch-controlplane.yaml", "talos-patch-worker.yaml"} <= set(f), f"talos files {list(f)}")
    docs = list(yaml.safe_load_all(f["talos-patch.yaml"]))
    kinds = {d.get("kind") for d in docs if d}
    check({"KubeNetworkConfig", "ResolverConfig", "TimeSyncConfig"} <= kinds, f"talos kinds {kinds}")
    cpk = {d.get("kind") for d in yaml.safe_load_all(f["talos-patch-controlplane.yaml"]) if d}
    check("KubeProxyConfig" in cpk, f"KubeProxyConfig is control-plane only: {cpk}")


# ---- Kubespray
@case("kubespray-defaults", "Kubespray")
def _(f, res):
    check("[kube_control_plane]" in f["inventory.ini"], "inventory groups")
    k = yaml.safe_load(f["group_vars/k8s_cluster/k8s-cluster.yml"])
    check(k["kube_network_plugin"] == "calico" and k["kube_version"] == "1.35.8", "k8s-cluster vars")


@case("kubespray-cilium-metallb", "Kubespray", ks_plugin="cilium", ks_proxy="ipvs", ks_strict_arp=True,
      ks_addons="helm,metrics_server,metallb,cert_manager", ks_metallb_range="192.168.10.200-192.168.10.220",
      ks_sans="192.168.10.100,k8s.example.com", ks_version="1.36.4", ks_runtime="crio", ks_etcd="kubeadm",
      ks_hosts=[{"name": "cp1", "ansible_host": "192.168.10.11", "ip": "10.0.0.11", "roles": "control-plane+etcd+node",
                 "ansible_user": ""}])
def _(f, res):
    a = yaml.safe_load(f["group_vars/k8s_cluster/addons.yml"])
    check(a.get("metallb_enabled") and a["metallb_config"]["address_pools"]["primary"]["ip_range"], "metallb config")


# ---- Cluster API
@case("capi-defaults", "Cluster API")
def _(f, res):
    docs = [d for d in yaml.safe_load_all(next(iter(f.values()))) if d]
    kinds = [d["kind"] for d in docs]
    check(kinds == ["Cluster", "DockerCluster", "KubeadmControlPlane", "DockerMachineTemplate", "MachineDeployment",
                    "DockerMachineTemplate", "KubeadmConfigTemplate"], f"capi kinds {kinds}")
    check(all(d["apiVersion"].endswith("/v1beta2") for d in docs), "capi v1beta2")


@case("capi-ha", "Cluster API", ca_name="prod", ca_namespace="capi", ca_cp_replicas="3", ca_workers="3",
      ca_sans="prod.example.com", ca_worker_labels="tier=apps", ca_labels="cni=calico")
def _(f, res):
    docs = [d for d in yaml.safe_load_all(next(iter(f.values()))) if d]
    kcp = [d for d in docs if d["kind"] == "KubeadmControlPlane"][0]
    check(kcp["spec"]["replicas"] == 3, "kcp replicas")


# ---- OpenShift
@case("openshift-none", "OpenShift")
def _(f, res):
    d = yaml.safe_load(f["install-config.yaml"])
    check(d["platform"] == {"none": {}} and d["controlPlane"]["replicas"] == 3, "ocp none")


@case("openshift-sno", "OpenShift", oc_name="sno", oc_cp_replicas="1", oc_workers="0", oc_machine_net="192.168.10.0/24",
      oc_ssh_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ4bqS0tMR0Eadk1q7Bf6FMu8Yg1e3mE0wJpm2R9n2Ru test@example")
def _(f, res):
    d = yaml.safe_load(f["install-config.yaml"])
    check(d["controlPlane"]["replicas"] == 1 and d["compute"][0]["replicas"] == 0 and d["sshKey"], "ocp sno")


@case("openshift-aws", "OpenShift", oc_platform="aws", oc_region="eu-west-1", oc_instance_type="m6i.xlarge",
      oc_publish="Internal", oc_fips=True)
def _(f, res):
    d = yaml.safe_load(f["install-config.yaml"])
    check(d["platform"]["aws"]["region"] == "eu-west-1" and d["publish"] == "Internal", "ocp aws")


@case("openshift-azure", "OpenShift", oc_platform="azure", oc_region="eastus", oc_azure_rg="dns-rg")
def _(f, res):
    check(yaml.safe_load(f["install-config.yaml"])["platform"]["azure"]["baseDomainResourceGroupName"] == "dns-rg",
          "ocp azure")


@case("openshift-gcp", "OpenShift", oc_platform="gcp", oc_region="us-central1", oc_gcp_project="my-ocp-project")
def _(f, res):
    check(yaml.safe_load(f["install-config.yaml"])["platform"]["gcp"]["projectID"] == "my-ocp-project", "ocp gcp")


@case("openshift-vsphere", "OpenShift", oc_platform="vsphere", oc_vc_password="secret", oc_workers="2")
def _(f, res):
    v = yaml.safe_load(f["install-config.yaml"])["platform"]["vsphere"]
    check(v["failureDomains"][0]["topology"]["computeCluster"] == "/dc1/host/cluster1", "ocp vsphere paths")


@case("openshift-baremetal", "OpenShift", oc_platform="baremetal", oc_workers="0",
      ocp_bm_hosts=[{"name": f"master-{i}", "role": "master", "bmc_address": f"ipmi://10.0.1.{i + 1}",
                     "bmc_user": "admin", "bmc_password": "pw", "boot_mac": f"52:54:00:00:00:0{i + 1}",
                     "insecure": True} for i in range(3)])
def _(f, res):
    b = yaml.safe_load(f["install-config.yaml"])["platform"]["baremetal"]
    check(len(b["hosts"]) == 3 and b["apiVIPs"] == ["10.0.0.5"], "ocp baremetal")


# ---- EKS
@case("eks-defaults", "EKS")
def _(f, res):
    d = yaml.safe_load(next(iter(f.values())))
    check(d["apiVersion"] == "eksctl.io/v1alpha5" and d["managedNodeGroups"][0]["instanceType"] == "m7i.large", "eks")


@case("eks-spot-multi", "EKS", ek_name="prod", ek_region="eu-central-1", ek_version="1.33", ek_tags="owner=john",
      ek_azs="eu-central-1a,eu-central-1b,eu-central-1c", ek_nat="HighlyAvailable", ek_private=True,
      ek_public_cidrs="203.0.113.0/24", ek_logging="api,audit", ek_addons="vpc-cni,coredns,kube-proxy,aws-ebs-csi-driver",
      ek_service_cidr="172.20.0.0/16",
      eks_nodegroups=[{"name": "system", "instance_types": "m7i.large", "min": "2", "desired": "2", "max": "4",
                       "volume_size": "50", "volume_type": "gp3", "ami_family": "AmazonLinux2023", "spot": False,
                       "private": True, "labels": "role=system", "taints": "CriticalAddonsOnly=true:NoSchedule"},
                      {"name": "spot", "instance_types": "m7i.large,m6i.large,m5.large", "min": "0", "desired": "1",
                       "max": "10", "volume_size": "80", "volume_type": "gp3", "ami_family": "Bottlerocket",
                       "spot": True, "private": True, "labels": "", "taints": ""}])
def _(f, res):
    d = yaml.safe_load(next(iter(f.values())))
    ng = d["managedNodeGroups"]
    check(ng[1]["instanceTypes"] == ["m7i.large", "m6i.large", "m5.large"] and ng[1]["spot"], "eks spot")
    check(ng[0]["taints"][0] == {"key": "CriticalAddonsOnly", "value": "true", "effect": "NoSchedule"}, "eks taints")


@case("eks-auto", "EKS", ek_auto_mode=True, eks_nodegroups=[], ek_addons="")
def _(f, res):
    d = yaml.safe_load(next(iter(f.values())))
    check(d["autoModeConfig"] == {"enabled": True} and "managedNodeGroups" not in d, "eks auto mode")


# ---- GKE
@case("gke-defaults", "GKE")
def _(f, res):
    fl = _flags(next(iter(f.values())), "clusters create")
    check(fl.get("--release-channel") == "regular" and "--enable-ip-alias" in fl, f"gke flags {fl}")


@case("gke-private", "GKE", gk_name="priv", gk_project="john-lab-123", gk_location="europe-west1-b",
      gk_node_locations="europe-west1-b,europe-west1-c", gk_private=True, gk_authorized="203.0.113.0/24",
      gk_pod_cidr="10.4.0.0/14", gk_svc_cidr="10.8.0.0/20", gk_spot=True, gk_node_labels="tier=apps",
      gk_node_taints="dedicated=batch:NoSchedule", gk_addons="HttpLoadBalancing,GcsFuseCsiDriver", gk_labels="env=dev",
      gk_channel="stable", gk_disk_type="pd-ssd")
def _(f, res):
    fl = _flags(next(iter(f.values())), "clusters create")
    check(fl.get("--enable-private-nodes", 1) is None and fl.get("--master-ipv4-cidr") == "172.16.0.0/28", "gke private")


@case("gke-autopilot", "GKE", gk_mode="Autopilot", gk_project="john-lab-123", gk_location="us-east1")
def _(f, res):
    t = next(iter(f.values()))
    check("clusters create-auto" in t and "--machine-type" not in t, "gke autopilot")


# ---- AKS
@case("aks-defaults", "AKS")
def _(f, res):
    fl = _flags(next(iter(f.values())), "aks create")
    check(fl.get("--network-dataplane") == "cilium" and "--no-ssh-key" in fl and "--generate-ssh-keys" not in fl,
          f"aks flags {fl}")


@case("aks-kubenet", "AKS", ak_name="kn", ak_rg="rg-kn", ak_location="westeurope", ak_plugin="kubenet",
      ak_overlay=False, ak_dataplane="azure", ak_policy="calico", ak_zones="", ak_autoscale=False, ak_count="2",
      ak_tier="standard", ak_azure_rbac=True, ak_authorized="203.0.113.10", ak_addons="monitoring,azure-policy",
      ak_ssh_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ4bqS0tMR0Eadk1q7Bf6FMu8Yg1e3mE0wJpm2R9n2Ru test",
      ak_tags="env=dev,owner=john", ak_max_pods="110", ak_create_rg=False)
def _(f, res):
    t = next(iter(f.values()))
    fl = _flags(t, "aks create")
    check(fl.get("--network-plugin") == "kubenet" and fl.get("--tags") == ["env=dev", "owner=john"], f"aks kubenet {fl}")
    check("group create" not in t, "no group create")


# ----------------------------------------------------------------------------------------------
NEGATIVE = [
    # minikube
    ("minikube", dict(mk_cpus="1"), "cpus"),
    ("minikube", dict(mk_memory="1000"), "memory"),
    ("minikube", dict(mk_disk_size="1g"), "disk"),
    ("minikube", dict(mk_ha=True, mk_nodes="2"), "ha"),
    ("minikube", dict(mk_driver="none", mk_nodes="2"), "none"),
    ("minikube", dict(mk_addons="metrics-servr"), "metrics-servr"),
    ("minikube", dict(mk_driver="kvm2", mk_ports="8080:80"), "ports"),
    ("minikube", dict(mk_driver="kvm2", mk_static_ip="192.168.200.200"), "static"),
    ("minikube", dict(mk_static_ip="8.8.8.8"), "private"),
    ("minikube", dict(mk_static_ip="192.168.200.1"), "static"),
    ("minikube", dict(mk_k8s_version="1.36"), "kubernetes-version"),
    ("minikube", dict(mk_profile="bad name"), "profile"),
    ("minikube", dict(mk_extra_config="foo.bar=1"), "extra-config"),
    ("minikube", dict(mk_cni="weave"), "cni"),
    ("minikube", dict(mk_driver="virtualbox", mk_cpus="no-limit"), "no-limit"),
    # k0s
    ("k0s", dict(k0s_hosts=[{"role": "worker", "address": "10.0.0.1"}]), "controller"),
    ("k0s", dict(k0_version="1.36.4"), "version"),
    ("k0s", dict(k0_pod_cidr="10.96.0.0/16"), "overlap"),
    ("k0s", dict(k0_storage="kine", k0_kine_ds=""), "datasource"),
    ("k0s", dict(k0s_hosts=[{"role": "controller", "address": "10.0.0.1"}, {"role": "worker", "address": "10.0.0.1"}]),
     "duplicate"),
    # MicroK8s
    ("MicroK8s", dict(m8_cluster_token="short"), "token"),
    ("MicroK8s", dict(m8_apiserver_args="event-ttl=2h"), "--"),
    ("MicroK8s", dict(m8_version="0.9.0"), "version"),
    # Talos
    ("Talos", dict(tl_endpoint="192.168.1.100"), "https"),
    ("Talos", dict(tl_pod_subnets="10.96.0.0/16", tl_service_subnets="10.96.0.0/12"), "overlap"),
    ("Talos", dict(tl_node_taints="bad taint"), "taint"),
    ("Talos", dict(tl_k8s_version="latest"), "version"),
    # Kubespray
    ("Kubespray", dict(ks_hosts=[{"name": "n1", "ansible_host": "10.0.0.1", "roles": "node"}]), "control-plane"),
    ("Kubespray", dict(ks_hosts=[{"name": f"n{i}", "ansible_host": f"10.0.0.{i}", "roles": "control-plane+etcd"}
                                 for i in range(2)]), "odd"),
    ("Kubespray", dict(ks_version="1.30.1"), "supported"),
    ("Kubespray", dict(ks_version="1.36.9"), "checksums"),
    ("Kubespray", dict(ks_pods="10.233.0.0/18"), "overlap"),
    ("Kubespray", dict(ks_node_prefix="26"), "110"),
    ("Kubespray", dict(ks_addons="metallb", ks_proxy="ipvs"), "strict_arp"),
    ("Kubespray", dict(ks_etcd="docker"), "docker"),
    ("Kubespray", dict(ks_addons="istio"), "istio"),
    # Cluster API
    ("Cluster API", dict(ca_cp_replicas="2"), "odd"),
    ("Cluster API", dict(ca_version="1.34.0"), "semver"),
    ("Cluster API", dict(ca_pods="10.128.0.0/12"), "overlap"),
    ("Cluster API", dict(ca_name="Bad_Name"), "dns"),
    ("Cluster API", dict(ca_worker_labels="node-role.kubernetes.io/worker="), "noderestriction"),
    # OpenShift
    ("OpenShift", dict(oc_cp_replicas="1", oc_workers="2"), "single-node"),
    ("OpenShift", dict(oc_pull_secret="{not json"), "json"),
    ("OpenShift", dict(oc_pull_secret='{"foo": 1}'), "auths"),
    ("OpenShift", dict(oc_platform="aws", oc_region=""), "region"),
    ("OpenShift", dict(oc_platform="gcp", oc_region="us-central1", oc_gcp_project=""), "projectid"),
    ("OpenShift", dict(oc_platform="azure", oc_region="eastus", oc_azure_rg=""), "basedomainresourcegroupname"),
    ("OpenShift", dict(oc_host_prefix="12"), "hostprefix"),
    ("OpenShift", dict(oc_service_net="10.128.0.0/16"), "overlap"),
    ("OpenShift", dict(oc_machine_net="100.64.0.0/16"), "ovn"),
    ("OpenShift", dict(oc_platform="baremetal", oc_api_vip="192.168.1.5"), "machinenetwork"),
    ("OpenShift", dict(oc_ssh_key="-----BEGIN OPENSSH PRIVATE KEY-----"), "public"),
    ("OpenShift", dict(oc_publish="Internal"), "internal"),
    # EKS
    ("EKS", dict(ek_region="mars-1"), "region"),
    ("EKS", dict(ek_version="1.34.2"), "minor"),
    ("EKS", dict(ek_azs="us-east-1a,us-east-1b"), "not in region"),
    ("EKS", dict(ek_public=False, ek_private=False), "publicaccess"),
    ("EKS", dict(ek_nat="Disable"), "nat"),
    ("EKS", dict(ek_logging="api,bogus"), "clusterlogging"),
    ("EKS", dict(eks_nodegroups=[{"name": "a", "min": "3", "desired": "1", "max": "2"}]), "minsize"),
    ("EKS", dict(ek_ip_family="IPv6", ek_oidc=False), "oidc"),
    # GKE
    ("GKE", dict(gk_name="Bad_Name"), "cluster name"),
    ("GKE", dict(gk_mode="Autopilot", gk_location="us-central1-a"), "regional"),
    ("GKE", dict(gk_min="5", gk_max="2"), "min-nodes"),
    ("GKE", dict(gk_addons="Istio"), "add-on"),
    ("GKE", dict(gk_addons="NetworkPolicy"), "dataplane"),
    ("GKE", dict(gk_private=True, gk_master_cidr="172.16.0.0/24"), "/28"),
    ("GKE", dict(gk_pod_cidr="10.0.0.0/24"), "/21"),
    ("GKE", dict(gk_node_locations="europe-west1-b"), "not a zone"),
    # AKS
    ("AKS", dict(ak_pool="System-Pool"), "nodepool-name"),
    ("AKS", dict(ak_zones="1,4"), "zones"),
    ("AKS", dict(ak_min="4", ak_max="5", ak_count="3"), "node-count"),
    ("AKS", dict(ak_plugin="kubenet"), "overlay"),
    ("AKS", dict(ak_dataplane="azure", ak_policy="cilium"), "cilium"),
    ("AKS", dict(ak_dns_ip="10.1.0.10"), "inside"),
    ("AKS", dict(ak_wi=True, ak_oidc=False), "oidc"),
    ("AKS", dict(ak_private=True, ak_authorized="1.2.3.4"), "private"),
    ("AKS", dict(ak_ssh_key="-----BEGIN OPENSSH PRIVATE KEY-----"), "public"),
    ("AKS", dict(ak_addons="istio"), "add-on"),
]

ROW_CHECKS = [
    ("k0s_hosts", {"role": "controller", "address": "10.0.0.1"}, False),
    ("k0s_hosts", {"role": "boss", "address": "10.0.0.1"}, True),
    ("k0s_hosts", {"role": "worker", "address": "10.0.0.1", "port": "99999"}, True),
    ("ks_hosts", {"name": "node1", "ansible_host": "10.0.0.1", "roles": "node"}, False),
    ("ks_hosts", {"name": "Node_1", "ansible_host": "10.0.0.1"}, True),
    ("ks_hosts", {"name": "n1", "ansible_host": "10.0.0.1", "ip": "300.1.1.1"}, True),
    ("eks_nodegroups", {"name": "ng", "instance_types": "m7i.large"}, False),
    ("eks_nodegroups", {"name": "ng", "instance_types": "large"}, True),
    ("eks_nodegroups", {"name": "ng", "taints": "a=b:Never"}, True),
    ("ocp_bm_hosts", {"name": "m0", "bmc_address": "ipmi://10.0.0.1", "boot_mac": "52:54:00:00:00:01", "bmc_password": "x"}, False),
    ("ocp_bm_hosts", {"name": "m0", "bmc_address": "http://x", "boot_mac": "52:54:00"}, True),
]


# ----------------------------------------------------------------------------------------------
# real tools
def tool_minikube(dirs):
    mk = _bin("minikube")
    if not check(mk, "minikube binary found"):
        return
    helptext = _run([mk, "start", "--help"]).stdout
    known = set(re.findall(r"^\s+(?:-\w, )?(--[a-z0-9-]+)[=:\s]", helptext, re.M))
    known |= set(re.findall(r"^\s+(?:-\w, )?(--[a-z0-9-]+)[=:\s]", _run([mk, "options"]).stdout, re.M))  # global flags
    check(len(known) > 100 and "--driver" in known, f"parsed `minikube start --help` ({len(known)} flags)")
    cfg_help = _run([mk, "config", "--help"]).stdout
    cfg_keys = set(re.findall(r"^\s+\*\s+([a-z0-9-]+)", cfg_help, re.M))
    check({"driver", "cpus", "memory", "disk-size", "kubernetes-version", "container-runtime"} <= cfg_keys,
          f"minikube config --help lists the keys ({len(cfg_keys)} found)")
    check(set(MK.CONFIG_KEYS) <= cfg_keys, f"CONFIG_KEYS are real config keys: {set(MK.CONFIG_KEYS) - cfg_keys}")
    defaults = _run([mk, "config", "defaults", "driver"]).stdout.split()
    check({"docker", "kvm2", "qemu2", "podman", "none", "ssh"} <= {d.lstrip("* ") for d in defaults} | set(defaults),
          f"`minikube config defaults driver` includes the dropdown drivers: {defaults}")
    lst = _run([mk, "addons", "list", "-o", "json"])
    if lst.returncode == 0 and lst.stdout.strip().startswith("{"):
        real = set(json.loads(lst.stdout))
        check(set(MK.ADDONS) <= real, f"minikube addons known: {set(MK.ADDONS) - real}")
    for d in dirs:
        name = os.path.basename(d)
        sh = os.path.join(d, "start-minikube.sh")
        cj = os.path.join(d, "config.json")
        if os.path.exists(sh):
            used = {k for k, _ in parse_script_flags(open(sh).read(), "start")}
            check(used <= known, f"{name}: flags exist in `minikube start --help`: {used - known}")
            check(_run(["bash", "-n", sh]).returncode == 0, f"{name}: bash -n")
        home = tempfile.mkdtemp(prefix="mkhome-")
        try:
            env = {**os.environ, "MINIKUBE_HOME": home, "MINIKUBE_IN_STYLE": "false", "KUBECONFIG": home + "/kc"}
            if os.path.exists(cj):
                want = json.load(open(cj))
                ok = True
                for k, v in want.items():
                    r = _run([mk, "config", "set", k, v], env=env)
                    ok &= check(r.returncode == 0, f"{name}: minikube config set {k} {v}: {r.stderr.strip()[-200:]}")
                got = json.load(open(os.path.join(home, ".minikube", "config", "config.json")))
                check(ok and got == want, f"{name}: config.json == what `minikube config set` writes ({got} vs {want})")
                print(f"  ok  minikube config set round-trip  {name} ({len(want)} keys)")
            if not SUDO or (os.path.exists(cj) and json.load(open(cj)).get("driver") not in ("docker", None)):
                continue
            if os.path.exists(cj):
                r = _run(SUDO + ["env", f"MINIKUBE_HOME={home}", f"KUBECONFIG={home}/kc", "MINIKUBE_IN_STYLE=false",
                                 mk, "start", "--dry-run", "--force"], env=env)
                check(r.returncode == 0 and "dry-run validation complete" in r.stdout,
                      f"{name}: minikube start --dry-run (config.json): {(r.stdout + r.stderr)[-400:]}")
                _run(SUDO + ["rm", "-f", os.path.join(home, ".minikube", "config", "config.json")])
            if os.path.exists(sh):
                r = _run(SUDO + ["env", f"MINIKUBE_HOME={home}", f"KUBECONFIG={home}/kc", "MINIKUBE_IN_STYLE=false",
                                 f"MINIKUBE={mk}", sh, "--dry-run", "--force"], env=env)
                check(r.returncode == 0 and "dry-run validation complete" in r.stdout,
                      f"{name}: start-minikube.sh --dry-run: {(r.stdout + r.stderr)[-400:]}")
            print(f"  ok  minikube start --dry-run         {name}")
        finally:
            _run(SUDO + ["rm", "-rf", home])
    # negative: minikube itself rejects what our validator rejects
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "MINIKUBE_HOME": home}
        r = _run([mk, "config", "set", "cpus", "abc"], env=env)
        check(r.returncode != 0, "minikube config set cpus abc is rejected (sanity)")


def tool_k0s(dirs):
    k0s = _bin("k0s")
    if not check(k0s, "k0s binary"):
        return
    for d in dirs:
        for fn in ("k0s.yaml", "k0sctl.yaml"):
            p = os.path.join(d, fn)
            if not os.path.exists(p):
                continue
            if fn == "k0sctl.yaml":
                doc = yaml.safe_load(open(p))
                check(doc["kind"] == "Cluster" and all(h.get("role") and h.get("ssh", {}).get("address")
                                                      for h in doc["spec"]["hosts"]), "k0sctl hosts shape")
                emb = doc["spec"]["k0s"].get("config")
                if not emb:
                    continue
                p = os.path.join(d, ".embedded-k0s.yaml")
                yaml.safe_dump(emb, open(p, "w"))
            r = _run([k0s, "config", "validate", "--config", p])
            check(r.returncode == 0, f"k0s config validate {os.path.basename(d)}/{fn}: {r.stderr.strip()[-300:]}")
            print(f"  ok  k0s config validate   {os.path.basename(d)}/{fn}")
    bad = os.path.join(OUT, "k0s-bad.yaml")
    yaml.safe_dump({"apiVersion": "k0s.k0sproject.io/v1beta1", "kind": "ClusterConfig", "metadata": {"name": "x"},
                    "spec": {"network": {"provider": "weave"}}}, open(bad, "w"))
    check(_run([k0s, "config", "validate", "--config", bad]).returncode != 0, "k0s rejects a bad provider (sanity)")


def tool_microk8s(dirs):
    chk = _bin("microk8s-launchcfg-check")
    if not chk:
        print("  skip microk8s launch-config parser check (no microk8s-launchcfg-check binary on PATH)")
        return
    for d in dirs:
        r = _run([chk, os.path.join(d, "microk8s-config.yaml")])
        check(r.returncode == 0, f"microk8s launch config {d}: {r.stdout}{r.stderr}")
        print(f"  ok  microk8s ParseConfiguration  {os.path.basename(d)}: {r.stdout.strip().split(': ', 1)[-1]}")
    bad = os.path.join(OUT, "microk8s-bad.yaml")
    open(bad, "w").write("version: 0.1.0\naddonz: [dns]\n")
    check(_run([chk, bad]).returncode != 0, "microk8s parser rejects an unknown key (sanity)")


def tool_talos(dirs):
    tc = _bin("talosctl")
    if not check(tc, "talosctl binary"):
        return
    for d in dirs:
        shutil.rmtree(os.path.join(d, "_out"), ignore_errors=True)
        r = _run([os.path.join(d, "talos-gen-config.sh")], env={**os.environ, "TALOSCTL": tc})
        ok = check(r.returncode == 0 and r.stdout.count("is valid for") == 2,
              f"talos gen config + validate --strict {d}: {(r.stdout + r.stderr)[-600:]}")
        if os.path.exists(os.path.join(d, "talos-patch.yaml")):
            cp = list(yaml.safe_load_all(open(os.path.join(d, "_out", "controlplane.yaml"))))
            kinds = {x.get("kind") for x in cp if x}
            check("ResolverConfig" in kinds or "KubeNetworkConfig" in kinds, f"talos patches merged: {kinds}")
        if ok:
            print(f"  ok  talosctl gen config + validate --strict  {os.path.basename(d)}")


def tool_eks(dirs):
    try:
        import jsonschema
    except ImportError:
        print("  skip eksctl ClusterConfig schema check (pip install jsonschema to enable)")
        return
    ek = _bin("eksctl")
    if ek:
        schema = json.loads(_run([ek, "utils", "schema"]).stdout)
    else:
        schema = json.load(open("/tmp/eksctl-schema.json"))
    v = jsonschema.Draft7Validator(schema)
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.yaml")):
            doc = yaml.safe_load(open(p))
            errs = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in v.iter_errors(doc)]
            if check(not errs, f"eksctl schema {p}: {errs[:5]}"):
                print(f"  ok  eksctl utils schema (JSON Schema)  {os.path.basename(d)}")
    bad = {"apiVersion": "eksctl.io/v1alpha5", "kind": "ClusterConfig", "metadata": {"name": "x", "region": "us-east-1"},
           "managedNodeGroups": [{"name": "a", "instanceTyp": "m5.large"}]}
    check(list(v.iter_errors(bad)), "eksctl schema rejects a typo'd key (sanity)")


def _help_choices(text):
    """flag -> allowed values, from gcloud ('X must be one of: a, b' or an indented list) / az ('Allowed values:')."""
    out = {}
    blocks = re.split(r"\n(?=\s{4,5}--[a-z])", text)
    for b in blocks:
        m = re.match(r"\s+(--[a-z0-9-]+)", b)
        if not m:
            continue
        lines = b.splitlines()
        for i, ln in enumerate(lines):
            if re.search(r"must be one of:\s*$", ln):             # gcloud list form
                vals, ind = set(), None
                for nxt in lines[i + 1:]:
                    if not nxt.strip():
                        continue
                    lead = len(nxt) - len(nxt.lstrip())
                    if ind is None:
                        ind = lead
                    if lead == ind and re.fullmatch(r"\s*[A-Za-z0-9_-]+", nxt):
                        vals.add(nxt.strip())
                    elif lead < ind:
                        break
                out[m.group(1)] = vals
                break
        else:
            flat = " ".join(b.split())
            mm = re.search(r"(?:must be one of:|Allowed values:)\s*(.+?)(?:\.\s|\.$|$)", flat)
            if mm:
                out[m.group(1)] = {v.strip(" .") for v in re.split(r"[,\s]+", mm.group(1)) if v.strip(" .")}
    return out


def _cloud_help(cmd, env):
    r = _run(cmd, env=env, timeout=180)
    return r.stdout


def tool_gke(dirs):
    gc = _bin("gcloud")
    if not check(gc, "gcloud installed"):
        return
    cfgdir = tempfile.mkdtemp(prefix="gcfg-")
    env = {**os.environ, "CLOUDSDK_CONFIG": cfgdir, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
           "CLOUDSDK_CORE_CHECK_GCE_METADATA": "false"}
    helps = {sub: _cloud_help([gc, "container", "clusters", sub, "--help"], env) for sub in ("create", "create-auto")}
    shutil.rmtree(cfgdir, ignore_errors=True)
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.sh")):
            t = open(p).read()
            sub = "create-auto" if "clusters create-auto" in t else "create"
            h = helps[sub]
            flags = set(re.findall(r"^\s+(--[a-z0-9-]+)", h, re.M))
            wide = re.search(r"GCLOUD WIDE FLAGS\s+These flags are available to all commands:(.+?)\n\s*\n", h, re.S)
            flags |= set(re.findall(r"--[a-z0-9-]+", wide.group(1))) if wide else set()
            choices = _help_choices(h)
            used = parse_script_flags(t, f"clusters {sub}")
            check(used, f"{p}: parsed flags")
            for k, v in used:
                check(k in flags, f"gcloud container clusters {sub}: unknown flag {k}")
                if k in choices and isinstance(v, str):
                    vals = [x.split("=")[0] for x in v.split(",")] if k == "--addons" else [v]
                    for x in vals:
                        check(x in choices[k], f"{k}={x} not in gcloud's allowed values {sorted(choices[k])}")
            check(_run(["bash", "-n", p]).returncode == 0, f"bash -n {p}")
            print(f"  ok  gcloud container clusters {sub} --help flags/values  {os.path.basename(d)} ({len(used)} flags)")
    ch = _help_choices(helps["create"])
    check({"--release-channel", "--disk-type", "--addons"} <= set(ch), "help parser found the enum flags (sanity)")
    check(ch.get("--release-channel") == {"None", "extended", "rapid", "regular", "stable"},
          f"release channels parsed exactly: {ch.get('--release-channel')}")


def tool_aks(dirs):
    az = _bin("az")
    if not check(az, "az installed"):
        return
    cfgdir = tempfile.mkdtemp(prefix="azcfg-")
    env = {**os.environ, "AZURE_CONFIG_DIR": cfgdir, "AZURE_CORE_COLLECT_TELEMETRY": "false"}
    h = _cloud_help([az, "aks", "create", "--help"], env)
    flags = set(re.findall(r"^\s+(--[a-z0-9-]+)", h, re.M))
    choices = _help_choices(h)
    check({"--network-plugin", "--os-sku", "--outbound-type", "--tier"} <= set(choices), f"az enum parse {sorted(choices)[:8]}")
    gh = _cloud_help([az, "group", "create", "--help"], env)
    shutil.rmtree(cfgdir, ignore_errors=True)
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.sh")):
            t = open(p).read()
            used = parse_script_flags(t, "aks create")
            for k, v in used:
                check(k in flags, f"az aks create: unknown flag {k}")
                if k in choices and isinstance(v, str):
                    check(v in choices[k], f"{k}={v} not in az allowed values {sorted(choices[k])}")
            check("--generate-ssh-keys" not in t, "never --generate-ssh-keys")
            for k, _ in parse_script_flags(t, "group create"):
                check(k in gh, f"az group create flag {k}")
            check(_run(["bash", "-n", p]).returncode == 0, f"bash -n {p}")
            print(f"  ok  az aks create --help flags/values  {os.path.basename(d)} ({len(used)} flags)")


def _crd_validator(crd_path):
    try:
        import jsonschema
    except ImportError:
        print("  skip InstallConfig CRD schema check (pip install jsonschema to enable)")
        return None
    d = yaml.safe_load(open(crd_path))
    s = d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    return jsonschema.Draft4Validator(s)


def tool_openshift(dirs):
    oi = _bin("openshift-install")
    if not check(oi, "openshift-install binary"):
        return
    crd = os.path.join(ROOT, "tests", "data", "installconfig-crd.yaml")
    if os.path.exists(crd):
        v = _crd_validator(crd)
    else:
        v = None
        print("  skip InstallConfig CRD schema check (tests/data/installconfig-crd.yaml not present)")
    for d in dirs:
        p = os.path.join(d, "install-config.yaml")
        doc = yaml.safe_load(open(p))
        if v:
            errs = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in v.iter_errors(doc)]
            if check(not errs, f"InstallConfig CRD schema {d}: {errs[:4]}"):
                print(f"  ok  InstallConfig CRD schema  {os.path.basename(d)} (platform {list(doc['platform'])[0]})")
        if "none" in doc["platform"]:
            with tempfile.TemporaryDirectory() as w:
                shutil.copy(p, w)
                r = _run([oi, "create", "manifests", "--dir", w], timeout=300)
                ok = r.returncode == 0 and os.path.isdir(os.path.join(w, "manifests"))
                check(ok, f"openshift-install create manifests {d}: {r.stderr[-600:]}")
                if ok:
                    n = len(os.listdir(os.path.join(w, "manifests"))) + len(os.listdir(os.path.join(w, "openshift")))
                    print(f"  ok  openshift-install create manifests  {os.path.basename(d)} ({n} manifests)")
    with tempfile.TemporaryDirectory() as w:
        bad = yaml.safe_load(open(os.path.join(dirs[0], "install-config.yaml")))
        bad["networking"]["clusterNetwork"][0]["hostPrefix"] = 8
        yaml.safe_dump(bad, open(os.path.join(w, "install-config.yaml"), "w"))
        check(_run([oi, "create", "manifests", "--dir", w]).returncode != 0, "openshift-install rejects bad hostPrefix (sanity)")


KS_TREE = "/workspace/tools/kubespray/kubespray-2.32.0"


def tool_kubespray(dirs):
    inv_bin, pb = _bin("ansible-inventory"), _bin("ansible-playbook")
    if not check(inv_bin and pb and os.path.isdir(KS_TREE), "ansible + kubespray 2.32.0 tree"):
        return
    defined = set()
    for p in glob.glob(f"{KS_TREE}/roles/**/defaults/**/*.yml", recursive=True) + \
            glob.glob(f"{KS_TREE}/inventory/sample/group_vars/**/*.yml", recursive=True):
        defined |= set(re.findall(r"^#?\s?([a-z_0-9]+):", open(p).read(), re.M))
    env = {**os.environ, "ANSIBLE_ROLES_PATH": f"{KS_TREE}/roles",
           "ANSIBLE_COLLECTIONS_PATH": "/workspace/tools/ansible-collections",
           "PYTHONPATH": "/workspace/tools/pylib", "ANSIBLE_HOST_KEY_CHECKING": "False", "ANSIBLE_NOCOLOR": "1"}
    play = os.path.join(ROOT, "tests", "data", "kubespray-validate.yml")
    if not os.path.exists(play):
        print("  skip kubespray validate_inventory role (tests/data/kubespray-validate.yml not present)")
        play = None
    for d in dirs:
        inv = os.path.join(d, "inventory.ini")
        r = _run([inv_bin, "-i", inv, "--list"], env=env)
        data = json.loads(r.stdout) if r.returncode == 0 else {}
        check(data.get("kube_control_plane", {}).get("hosts"), f"ansible-inventory {d}: {r.stderr[-300:]}")
        hv = data.get("_meta", {}).get("hostvars", {})
        some = next(iter(hv.values()), {})
        check("kube_network_plugin" in some, "group_vars/k8s_cluster applied to hosts")
        for f in glob.glob(os.path.join(d, "group_vars", "**", "*.yml"), recursive=True):
            keys = set((yaml.safe_load(open(f)) or {}).keys())
            unknown = keys - defined - {"ansible_user"}
            check(not unknown, f"kubespray vars exist in the 2.32 tree: {unknown}")
        if not play:
            print(f"  ok  ansible-inventory + kubespray variable names  {os.path.basename(d)} ({len(hv)} hosts)")
            continue
        r = _run([pb, "-i", inv, play], env=env, timeout=600)
        check(r.returncode == 0 and "failed=0" in r.stdout, f"kubespray validate_inventory {d}: {r.stdout[-1500:]}")
        print(f"  ok  ansible-inventory + kubespray validate_inventory  {os.path.basename(d)} ({len(hv)} hosts)")
    if not play:
        return
    # sanity: the role really catches mistakes
    with tempfile.TemporaryDirectory() as w:
        shutil.copytree(dirs[0], w, dirs_exist_ok=True)
        k = os.path.join(w, "group_vars", "k8s_cluster", "k8s-cluster.yml")
        open(k, "a").write("kube_proxy_mode: userspace\n")
        r = _run([pb, "-i", os.path.join(w, "inventory.ini"), play], env=env, timeout=600)
        check(r.returncode != 0, "kubespray validate_inventory rejects kube_proxy_mode userspace (sanity)")


# ---- live: minikube boot
def live_minikube():
    mk = _bin("minikube")
    res = gen("minikube", mk_profile="k8sgui-mk", mk_output="both (config.json + start-minikube.sh)", mk_cpus="2",
              mk_memory="2200", mk_disk_size="20000mb", mk_k8s_version="stable", mk_addons="metrics-server",
              mk_wait="apiserver,system_pods", mk_runtime="containerd")
    check(res.ok, f"live minikube config {res.errors}")
    d = write("minikube-live", res)
    home = tempfile.mkdtemp(prefix="mkhome-")
    envs = [f"MINIKUBE_HOME={home}", f"KUBECONFIG={home}/kubeconfig", "MINIKUBE_IN_STYLE=false", f"MINIKUBE={mk}"]
    t0 = time.time()
    try:
        r = _run(SUDO + ["env"] + envs + [os.path.join(d, "start-minikube.sh"), "--force"], timeout=900)
        check(r.returncode == 0, f"minikube start (live): {(r.stdout + r.stderr)[-1500:]}")
        print(f"  minikube start finished rc={r.returncode} in {time.time() - t0:.0f}s")
        if r.returncode == 0:
            st = _run(SUDO + ["env"] + envs + [mk, "status", "-p", "k8sgui-mk", "-o", "json"])
            j = json.loads(st.stdout) if st.stdout.strip().startswith("{") else {}
            check(j.get("Host") == "Running" and j.get("APIServer") == "Running", f"minikube status {st.stdout}")
            kc = _run(SUDO + ["env"] + envs + [mk, "kubectl", "-p", "k8sgui-mk", "--", "get", "nodes", "-o", "json"])
            nodes = json.loads(kc.stdout)["items"] if kc.returncode == 0 else []
            check(len(nodes) == 1, f"1 node: {kc.stderr[-300:]}")
            if nodes:
                info = nodes[0]["status"]["nodeInfo"]
                check(info["containerRuntimeVersion"].startswith("containerd://"), f"runtime {info}")
                print(f"  ok  node {nodes[0]['metadata']['name']}: {info['kubeletVersion']} {info['containerRuntimeVersion']}")
            ad = _run(SUDO + ["env"] + envs + [mk, "addons", "list", "-p", "k8sgui-mk", "-o", "json"])
            on = {k for k, v in json.loads(ad.stdout).items() if v.get("Status") == "enabled"} if ad.returncode == 0 else set()
            check("metrics-server" in on, f"metrics-server enabled: {on}")
            prof = _run(SUDO + ["env"] + envs + [mk, "profile", "list", "-o", "json"])
            pj = json.loads(prof.stdout) if prof.returncode == 0 else {}
            cfgd = [p for p in pj.get("valid", []) if p["Name"] == "k8sgui-mk"]
            if cfgd:
                cc = cfgd[0]["Config"]
                check(cc["CPUs"] == 2 and cc["Memory"] == 2200 and cc["Driver"] == "docker", f"profile config {cc['CPUs']} {cc['Memory']}")
                print(f"  ok  profile k8sgui-mk: driver={cc['Driver']} cpus={cc['CPUs']} memory={cc['Memory']}MB "
                      f"k8s={cc['KubernetesConfig']['KubernetesVersion']}")
    finally:
        r = _run(SUDO + ["env"] + envs + [mk, "delete", "-p", "k8sgui-mk"], timeout=300)
        check(r.returncode == 0, f"minikube delete: {r.stderr[-300:]}")
        left = _run(SUDO + ["docker", "ps", "-a", "--filter", "name=k8sgui-mk", "-q"]).stdout.strip()
        check(not left, f"no minikube containers left: {left}")
        _run(SUDO + ["rm", "-rf", home])
        print("  minikube profile deleted, MINIKUBE_HOME removed")


# ---- live: Cluster API server-side dry run
CAPI_DIR = "/workspace/tools/capi"


def live_capi(capi_file_dirs):
    kind, kubectl = _bin("kind"), _bin("kubectl")
    if not check(kind and kubectl and os.path.isdir(CAPI_DIR), "kind + kubectl + CAPI components"):
        return
    name = "k8sgui-capi"
    kc = tempfile.mktemp(prefix="capi-kc-")
    try:
        r = _run(SUDO + [kind, "create", "cluster", "--name", name, "--kubeconfig", kc, "--wait", "120s"], timeout=600)
        if not check(r.returncode == 0, f"kind create {r.stderr[-500:]}"):
            return
        _run(SUDO + ["chmod", "644", kc])
        crds = []
        for f in glob.glob(os.path.join(CAPI_DIR, "*.yaml")):
            for doc in yaml.safe_load_all(open(f)):
                if doc and doc.get("kind") == "CustomResourceDefinition":
                    doc["spec"].pop("conversion", None)   # no webhook server in this test
                    doc["metadata"].get("annotations", {}).pop("cert-manager.io/inject-ca-from", None)
                    crds.append(doc)
        crdf = os.path.join(OUT, "capi-crds.yaml")
        with open(crdf, "w") as fh:
            yaml.safe_dump_all(crds, fh)
        r = _run([kubectl, "--kubeconfig", kc, "apply", "--server-side", "-f", crdf])
        check(r.returncode == 0, f"apply CAPI CRDs {r.stderr[-500:]}")
        _run([kubectl, "--kubeconfig", kc, "wait", "--for=condition=Established", "crd", "--all", "--timeout=120s"])
        print(f"  {len(crds)} CAPI v1.14 + CAPD CRDs installed (conversion webhooks stripped)")
        for d in capi_file_dirs:
            for p in glob.glob(os.path.join(d, "*.yaml")):
                ns = [x for x in yaml.safe_load_all(open(p)) if x][0]["metadata"]["namespace"]
                _run([kubectl, "--kubeconfig", kc, "create", "namespace", ns])
                r = _run([kubectl, "--kubeconfig", kc, "apply", "--dry-run=server", "-f", p])
                check(r.returncode == 0 and r.stdout.count("(server dry run)") == 7,
                      f"kubectl apply --dry-run=server {p}: {r.stdout}{r.stderr[-800:]}")
                print(f"  ok  CAPI server dry-run {os.path.basename(d)}: {r.stdout.count('(server dry run)')} objects")
        bad = os.path.join(OUT, "capi-bad.yaml")
        doc = [x for x in yaml.safe_load_all(open(glob.glob(os.path.join(capi_file_dirs[0], '*.yaml'))[0])) if x][2]
        doc["spec"]["machineTemplate"]["spec"]["infrastructureRef"] = {"kind": "DockerMachineTemplate", "name": "x"}
        yaml.safe_dump(doc, open(bad, "w"))
        r = _run([kubectl, "--kubeconfig", kc, "apply", "--dry-run=server", "-f", bad])
        check(r.returncode != 0 and "apiGroup" in r.stderr, "server rejects a ref without apiGroup (sanity)")
    finally:
        _run(SUDO + [kind, "delete", "cluster", "--name", name], timeout=300)
        left = _run(SUDO + ["docker", "ps", "-a", "--filter", f"name={name}", "-q"]).stdout.strip()
        check(not left, f"no kind containers left: {left}")
        if os.path.exists(kc):
            _run(SUDO + ["rm", "-f", kc])
        print("  kind cluster deleted")


def main():
    args = set(sys.argv[1:])
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    print("== registration / grouping")
    check([t for g in ccg.GROUPS.values() for t in g] == ccg.CONFIG_TYPES, "CONFIG_TYPES follows GROUPS")
    check(ccg.GROUPS["Local"] == ["kind", "minikube", "k3s / RKE2", "k0s", "MicroK8s"], "Local group order")
    check(ccg.GROUPS["Self-managed"] == ["kubeadm", "Talos", "Kubespray", "Cluster API", "OpenShift"], "Self-managed order")
    check(ccg.GROUPS["Managed cloud"] == ["EKS", "GKE", "AKS"] and ccg.GROUPS["Access"] == ["kubeconfig"], "cloud/access")
    for m in ccg.PLATFORM_MODULES:
        check(ccg.GROUP_OF[m.TYPE] == m.GROUP, f"{m.TYPE} group")
        r = gen(m.TYPE)
        check(r.ok, f"{m.TYPE} defaults valid: {r.errors}")
        check(outputs(r) and render(r).strip(), f"{m.TYPE} renders")
        keys = [f.key for _, fs in ccg.SCHEMA[m.TYPE] for f in fs]
        check(len(keys) == len(set(keys)), f"{m.TYPE} unique field keys")
        for _, fs in ccg.SCHEMA[m.TYPE]:
            for f in fs:
                if f.kind == "combo":
                    check(f.default in f.options, f"{m.TYPE}.{f.key} default {f.default!r} in options")
    allkeys = [f.key for t in ccg.CONFIG_TYPES for _, fs in ccg.SCHEMA[t] for f in fs]
    print(f"  {len(ccg.PLATFORM_MODULES)} platform modules, {len(ccg.CONFIG_TYPES)} types, {len(allkeys)} fields")
    print("== positive cases")
    dirs: dict = {}
    for name, (ctype, over, fn) in CASES.items():
        res = gen(ctype, **over)
        if not check(res.ok, f"{name}: unexpected errors {res.errors}"):
            continue
        d = write(name, res)
        f = files_of(res)
        for o in outputs(res):
            if o.name.endswith((".yaml", ".yml")):
                try:
                    list(yaml.safe_load_all(o.text))
                except yaml.YAMLError as exc:
                    check(False, f"{name}/{o.name}: YAML {exc}")
            if o.name.endswith(".json"):
                json.loads(o.text)
            if o.name.endswith(".sh"):
                check(o.mode == 0o755 and o.text.startswith("#!/usr/bin/env bash"), f"{name}/{o.name} executable script")
                check(_run(["bash", "-n", os.path.join(d, o.name)]).returncode == 0, f"{name}/{o.name}: bash -n")
        fn(f, res)
        dirs.setdefault(ctype, []).append(d)
        print(f"  ok  {name:26s} -> {', '.join(os.path.relpath(os.path.join(d, o.name), HERE) for o in outputs(res))}"
              + (f"   ({len(res.warnings)} warning(s))" if res.warnings else ""))
    check(oct(outputs(gen("OpenShift"))[0].mode) == "0o600", "install-config.yaml (pull secret) is 0600")
    print("== negative cases")
    for ctype, over, want in NEGATIVE:
        res = gen(ctype, **over)
        check(any(want.lower() in e.lower() for e in res.errors),
              f"{ctype} {over}: expected error containing {want!r}, got {res.errors}")
    print(f"  {len(NEGATIVE)} negative cases checked")
    for key, row, bad in ROW_CHECKS:
        errs = ccg.check_row(key, row)
        check(bool(errs) == bad, f"check_row {key} {row}: {errs}")
    print(f"  {len(ROW_CHECKS)} row checks")
    if "--tools" in args:
        print("== real tools (offline)")
        tool_minikube(dirs["minikube"])
        tool_k0s(dirs["k0s"])
        tool_microk8s(dirs["MicroK8s"])
        tool_talos(dirs["Talos"])
        tool_eks(dirs["EKS"])
        tool_gke(dirs["GKE"])
        tool_aks(dirs["AKS"])
        tool_openshift(dirs["OpenShift"])
        tool_kubespray(dirs["Kubespray"])
    if "--minikube" in args:
        print("== minikube start (real, docker driver)")
        live_minikube()
    if "--capi" in args:
        print("== Cluster API CRDs on kind + server dry-run (real)")
        live_capi(dirs["Cluster API"])
    print(f"\n{COUNT[0] - len(FAILS)}/{COUNT[0]} platform checks passed")
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
