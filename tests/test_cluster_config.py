#!/usr/bin/env python3
"""
Headless tests for cluster_config_gen (headless).

  python3 tests/test_cluster_config.py           # structure / parse / base64 / negative checks
  python3 tests/test_cluster_config.py --tools   # + real tools when found (PATH, ./bin, /workspace/bin):
        kubeadm config validate --config <file>          (kubeadm init + join outputs)
        kubectl config view / get-contexts --kubeconfig  (every kubeconfig variant)
        k3s|rke2 server|agent --help                     (every generated key must be a real flag)
  python3 tests/test_cluster_config.py --tools --kind  # + `kind create cluster --config` from the generated
        kind config, generated kubeconfig auth test (`kubectl get nodes`), then `kind delete cluster`.
        Set KIND_SUDO=1 if docker needs sudo. KEEP_KIND=1 leaves the cluster running.
  python3 tests/test_cluster_config.py --tools --k3s-docker   # + boot rancher/k3s with the generated
        config.yaml mounted at /etc/rancher/k3s/config.yaml and wait for the node to be Ready.

Generated files are written to tests/output/cluster/<case>/.
"""
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

import cluster_config_gen as ccg  # noqa: E402

OUT = os.path.join(HERE, "output", "cluster")
TOKEN = "abcdef.0123456789abcdef"
HASH = "sha256:" + "a" * 64

PASS = FAIL = 0
LOG = []


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")
        LOG.append(msg)
    return cond


def gen(ctype, **over):
    vals = {**ccg.defaults(ctype), **over}
    return ccg.generate(ctype, vals)


def write(case, res):
    d = os.path.join(OUT, case)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, res.filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(ccg.render(res))
    return path


def docs_of(text):
    return [d for d in yaml.safe_load_all(text) if d is not None]


def make_pki():
    """Self-signed CA + client cert via openssl (for PEM -> base64 checks). Falls back to fake PEMs."""
    d = tempfile.mkdtemp(prefix="ccg-pki-")
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-subj", "/CN=test-ca", "-keyout", f"{d}/ca.key", "-out", f"{d}/ca.crt"],
                       check=True, capture_output=True)
        return open(f"{d}/ca.crt").read(), open(f"{d}/ca.key").read(), d
    except Exception:
        crt = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ\n-----END CERTIFICATE-----\n"
        key = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49\n-----END PRIVATE KEY-----\n"
        return crt, key, d


CA_PEM, KEY_PEM, PKI_DIR = make_pki()
B64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731

# --------------------------------------------------------------------------- positive cases
# name -> (type, overrides, assertions(docs, res))
CASES = {}


def case(case_name, ctype, **over):
    def deco(fn):
        CASES[case_name] = (ctype, over, fn)
        return fn
    return deco


def by_kind(docs):
    return {d.get("kind"): d for d in docs}


@case("kubeadm-defaults", "kubeadm")
def _(docs, res):
    k = by_kind(docs)
    check(res.filename == "kubeadm-config.yaml", "kubeadm default filename")
    check(set(k) == {"InitConfiguration", "ClusterConfiguration", "KubeletConfiguration"}, f"kubeadm kinds {set(k)}")
    check(all(d["apiVersion"] == "kubeadm.k8s.io/v1beta4" for d in docs if "Configuration" in d["kind"]
              and d["kind"] in ("InitConfiguration", "ClusterConfiguration")), "kubeadm v1beta4")
    check(k["KubeletConfiguration"]["apiVersion"] == "kubelet.config.k8s.io/v1beta1", "kubelet apiVersion")
    check(k["KubeletConfiguration"]["cgroupDriver"] == "systemd", "cgroupDriver systemd")
    check(k["InitConfiguration"]["localAPIEndpoint"] == {"advertiseAddress": "192.168.1.10", "bindPort": 6443},
          "localAPIEndpoint")


@case("kubeadm-full", "kubeadm", advertise_address="10.0.0.11", bind_port="6443", node_name="cp-1",
      cri_socket="unix:///var/run/crio/crio.sock", taints="none", kubelet_extra_args="node-ip=10.0.0.11\nv=2",
      bootstrap_token=TOKEN, token_ttl="2h0m0s", kubernetes_version="v1.37.1", cluster_name="prod",
      control_plane_endpoint="k8s-api.example.com:6443", pod_subnet="192.168.0.0/16",
      service_subnet="10.96.0.0/12", dns_domain="prod.local", cert_sans="k8s-api.example.com, 10.0.0.100",
      etcd_data_dir="/data/etcd", include_kube_proxy=True, proxy_mode="ipvs", cgroup_driver="systemd")
def _(docs, res):
    k = by_kind(docs)
    i, c = k["InitConfiguration"], k["ClusterConfiguration"]
    nr = i["nodeRegistration"]
    check(nr["name"] == "cp-1" and nr["criSocket"].endswith("crio.sock"), "nodeRegistration name/criSocket")
    check(nr["taints"] == [], "taints none -> []")
    check(nr["kubeletExtraArgs"] == [{"name": "node-ip", "value": "10.0.0.11"}, {"name": "v", "value": "2"}],
          "v1beta4 kubeletExtraArgs list form")
    check(i["bootstrapTokens"][0]["token"] == TOKEN and i["bootstrapTokens"][0]["ttl"] == "2h0m0s",
          "bootstrap token")
    check(c["controlPlaneEndpoint"] == "k8s-api.example.com:6443", "controlPlaneEndpoint")
    check(c["networking"] == {"podSubnet": "192.168.0.0/16", "serviceSubnet": "10.96.0.0/12",
                              "dnsDomain": "prod.local"}, "networking")
    check(c["apiServer"]["certSANs"] == ["k8s-api.example.com", "10.0.0.100"], "certSANs")
    check(c["etcd"]["local"]["dataDir"] == "/data/etcd", "etcd dataDir")
    check(c["clusterName"] == "prod", "clusterName")
    check(k["KubeProxyConfiguration"]["mode"] == "ipvs" and
          k["KubeProxyConfiguration"]["apiVersion"] == "kubeproxy.config.k8s.io/v1alpha1", "kube-proxy ipvs")


@case("kubeadm-taints-nftables", "kubeadm", taints="node-role.kubernetes.io/control-plane:NoSchedule\ndedicated=infra:NoExecute",
      include_kubelet=False, include_kube_proxy=True, proxy_mode="nftables", control_plane_endpoint="10.0.0.100")
def _(docs, res):
    k = by_kind(docs)
    check("KubeletConfiguration" not in k, "kubelet doc omitted")
    t = k["InitConfiguration"]["nodeRegistration"]["taints"]
    check(t == [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"},
                {"key": "dedicated", "value": "infra", "effect": "NoExecute"}], f"taints {t}")
    check(k["KubeProxyConfiguration"]["mode"] == "nftables", "nftables")


@case("kubeadm-join-worker", "kubeadm", output="join (JoinConfiguration)", join_endpoint="10.0.0.100:6443",
      join_token=TOKEN, ca_cert_hashes=HASH, node_name="worker-1")
def _(docs, res):
    check(res.filename == "kubeadm-join-config.yaml", "join filename")
    j = docs[0]
    check(j["kind"] == "JoinConfiguration" and j["apiVersion"] == "kubeadm.k8s.io/v1beta4", "JoinConfiguration v1beta4")
    bt = j["discovery"]["bootstrapToken"]
    check(bt == {"apiServerEndpoint": "10.0.0.100:6443", "token": TOKEN, "caCertHashes": [HASH]}, f"discovery {bt}")
    check("controlPlane" not in j, "worker join has no controlPlane")


@case("kubeadm-join-cp", "kubeadm", output="join (JoinConfiguration)", join_endpoint="lb.example.com:6443",
      join_token=TOKEN, unsafe_skip_ca=True, join_control_plane=True, certificate_key="b" * 64,
      advertise_address="10.0.0.12")
def _(docs, res):
    j = docs[0]
    check(j["discovery"]["bootstrapToken"].get("unsafeSkipCAVerification") is True, "unsafeSkipCAVerification")
    check(j["controlPlane"]["certificateKey"] == "b" * 64, "certificateKey")
    check(j["controlPlane"]["localAPIEndpoint"]["advertiseAddress"] == "10.0.0.12", "cp advertiseAddress")
    check(any("unsafe" in w.lower() or "skip" in w.lower() for w in res.warnings), "unsafe skip warns")


@case("kind-defaults", "kind")
def _(docs, res):
    c = docs[0]
    check(res.filename == "kind-config.yaml", "kind filename")
    check(c["kind"] == "Cluster" and c["apiVersion"] == "kind.x-k8s.io/v1alpha4", "kind v1alpha4")
    check([n["role"] for n in c["nodes"]] == ["control-plane", "worker", "worker"], "count expansion")
    check(c["name"] == "dev", "kind name")


@case("kind-full", "kind", name="lab", feature_gates="InPlacePodVerticalScaling=true\nSomeAlpha=false",
      nodes=[{"role": "control-plane", "count": "3", "image": "kindest/node:v1.37.0", "labels": "tier=cp"},
             {"role": "worker", "count": "1", "image": "", "labels": "ingress-ready=true,zone=a"}],
      port_mappings=[{"container_port": "80", "host_port": "8080", "listen_address": "127.0.0.1", "protocol": "TCP"},
                     {"container_port": "30053", "host_port": "30053", "listen_address": "", "protocol": "UDP"}],
      ip_family="dual", api_server_address="127.0.0.1", api_server_port="6443",
      pod_subnet="10.244.0.0/16,fd00:10:244::/56", service_subnet="10.96.0.0/16,fd00:10:96::/112",
      disable_default_cni=True, kube_proxy_mode="ipvs")
def _(docs, res):
    c = docs[0]
    check(len(c["nodes"]) == 4 and [n["role"] for n in c["nodes"]].count("control-plane") == 3, "3 cp + 1 worker")
    check(c["nodes"][0]["extraPortMappings"] == [
        {"containerPort": 80, "hostPort": 8080, "listenAddress": "127.0.0.1"},           # TCP = kind default
        {"containerPort": 30053, "hostPort": 30053, "protocol": "UDP"}],
          f"port mapping {c['nodes'][0].get('extraPortMappings')}")
    check(all("extraPortMappings" not in n for n in c["nodes"][1:]), "mappings only on first cp")
    check(c["nodes"][0]["image"] == "kindest/node:v1.37.0", "node image")
    check(c["nodes"][3]["labels"] == {"ingress-ready": "true", "zone": "a"}, "node labels")
    check(c["featureGates"] == {"InPlacePodVerticalScaling": True, "SomeAlpha": False}, "featureGates bools")
    n = c["networking"]
    check(n["ipFamily"] == "dual" and n["disableDefaultCNI"] is True and n["kubeProxyMode"] == "ipvs" and
          n["apiServerPort"] == 6443, f"networking {n}")


@case("kubeconfig-token-pem", "kubeconfig", cluster_name="prod", server="https://10.0.0.100:6443",
      ca=CA_PEM, user_name="ci", auth_method="token", token="eyJhbGciOi.fake.token", namespace="apps")
def _(docs, res):
    k = docs[0]
    check(res.filename == "prod.kubeconfig", "kubeconfig filename <cluster>.kubeconfig")
    check(k["apiVersion"] == "v1" and k["kind"] == "Config", "Config v1")
    cl = k["clusters"][0]["cluster"]
    check(cl["certificate-authority-data"] == B64(CA_PEM), "pasted PEM is base64-encoded")
    check(base64.b64decode(cl["certificate-authority-data"]).decode() == CA_PEM, "CA round-trips")
    check(k["users"][0]["user"] == {"token": "eyJhbGciOi.fake.token"}, "token user")
    ctx = k["contexts"][0]
    check(ctx["name"] == "ci@prod" and ctx["context"]["namespace"] == "apps", "context name/namespace")
    check(k["current-context"] == "ci@prod", "current-context")


@case("kubeconfig-cert-paths", "kubeconfig", cluster_name="lab", ca="/etc/kubernetes/pki/ca.crt",
      auth_method="client certificate", client_cert="/home/john/.kube/john.crt",
      client_key="/home/john/.kube/john.key", context_name="lab-admin", set_current=False)
def _(docs, res):
    k = docs[0]
    check(k["clusters"][0]["cluster"]["certificate-authority"] == "/etc/kubernetes/pki/ca.crt", "CA path")
    check(k["users"][0]["user"] == {"client-certificate": "/home/john/.kube/john.crt",
                                    "client-key": "/home/john/.kube/john.key"}, "cert paths")
    check("current-context" not in k or not k.get("current-context"), "current-context omitted")


@case("kubeconfig-cert-pem", "kubeconfig", cluster_name="pemtest", ca=B64(CA_PEM),
      auth_method="client certificate", client_cert=CA_PEM, client_key=KEY_PEM)
def _(docs, res):
    u = docs[0]["users"][0]["user"]
    check(docs[0]["clusters"][0]["cluster"]["certificate-authority-data"] == B64(CA_PEM), "base64 CA kept")
    check(u["client-certificate-data"] == B64(CA_PEM) and u["client-key-data"] == B64(KEY_PEM),
          "client cert/key PEM -> base64")


@case("kubeconfig-exec-insecure", "kubeconfig", cluster_name="eks", server="https://ABC.gr7.us-west-2.eks.amazonaws.com",
      ca_mode="insecure-skip-tls-verify", auth_method="exec plugin", exec_command="aws",
      exec_args="eks\nget-token\n--cluster-name\neks", exec_env="AWS_PROFILE=prod",
      exec_provide_cluster_info=True)
def _(docs, res):
    k = docs[0]
    check(k["clusters"][0]["cluster"].get("insecure-skip-tls-verify") is True, "insecure-skip-tls-verify")
    ex = k["users"][0]["user"]["exec"]
    check(ex["apiVersion"] == "client.authentication.k8s.io/v1" and ex["command"] == "aws" and
          ex["args"] == ["eks", "get-token", "--cluster-name", "eks"] and
          ex["env"] == [{"name": "AWS_PROFILE", "value": "prod"}] and ex["interactiveMode"] == "IfAvailable" and
          ex["provideClusterInfo"] is True, f"exec {ex}")
    check(any("insecure" in w.lower() for w in res.warnings), "insecure warns")


@case("kubeconfig-basic", "kubeconfig", cluster_name="old", ca_mode="none (system CAs)",
      auth_method="username/password", username="admin", password="s3cret")
def _(docs, res):
    check(docs[0]["users"][0]["user"] == {"username": "admin", "password": "s3cret"}, "basic auth")


@case("k3s-defaults", "k3s / RKE2")
def _(docs, res):
    check(res.filename == "config.yaml", "k3s filename config.yaml")
    check(docs[0] in ({}, None), f"defaults -> empty config {docs[0]}")
    check("/etc/rancher/k3s/config.yaml" in res.header, "path hint header")


@case("k3s-server-ha", "k3s / RKE2", distro="k3s", role="server", cluster_init=True, token="K10supersecret",
      node_name="cp-1", node_ip="10.0.0.11", tls_san="k3s.example.com,10.0.0.100", cluster_cidr="10.42.0.0/16",
      service_cidr="10.43.0.0/16", cluster_dns="10.43.0.10", cluster_domain="cluster.local",
      flannel_backend="wireguard-native", write_kubeconfig_mode="0644", k3s_disable_traefik=True,
      k3s_disable_servicelb=True, node_label="tier=cp\nzone=a", node_taint="CriticalAddonsOnly=true:NoExecute")
def _(docs, res):
    c = docs[0]
    check(c["cluster-init"] is True and c["token"] == "K10supersecret", "cluster-init/token")
    check(c["tls-san"] == ["k3s.example.com", "10.0.0.100"], "tls-san list")
    check(c["disable"] == ["traefik", "servicelb"], f"disable {c.get('disable')}")
    check(c["flannel-backend"] == "wireguard-native", "flannel-backend")
    check(c["node-label"] == ["tier=cp", "zone=a"] and c["node-taint"] == ["CriticalAddonsOnly=true:NoExecute"],
          "labels/taints lists")
    check(c["write-kubeconfig-mode"] == "0644", "write-kubeconfig-mode")
    check(any("world-readable" in w for w in res.warnings), "0644 warns")


@case("k3s-agent", "k3s / RKE2", distro="k3s", role="agent", token="K10supersecret",
      server_url="https://10.0.0.11:6443", node_name="worker-1", node_label="role=worker")
def _(docs, res):
    c = docs[0]
    check(c == {"server": "https://10.0.0.11:6443", "token": "K10supersecret", "node-name": "worker-1",
                "node-label": ["role=worker"]}, f"k3s agent {c}")


@case("rke2-server", "k3s / RKE2", distro="rke2", role="server", token="rke2secret", tls_san="rke2.example.com",
      cni="cilium", multus=True, ingress_controller="traefik", rke2_disable_rke2_snapshot_controller=True,
      rke2_disable_rke2_snapshot_controller_crd=True, rke2_disable_rke2_snapshot_validation_webhook=True,
      cluster_cidr="10.42.0.0/16", service_cidr="10.43.0.0/16", write_kubeconfig_mode="0600")
def _(docs, res):
    c = docs[0]
    check("/etc/rancher/rke2/config.yaml" in res.header, "rke2 path hint")
    check(c["cni"] == ["multus", "cilium"], f"rke2 cni {c.get('cni')}")
    check(c["ingress-controller"] == "traefik", "ingress-controller")
    check(c["disable"] == ["rke2-snapshot-controller", "rke2-snapshot-controller-crd",
                           "rke2-snapshot-validation-webhook"], f"rke2 disable {c.get('disable')}")
    check("cluster-init" not in c and "flannel-backend" not in c, "no k3s-only keys")


@case("rke2-join-server", "k3s / RKE2", distro="rke2", role="server", token="rke2secret",
      server_url="https://10.0.0.11:9345")
def _(docs, res):
    check(docs[0]["server"] == "https://10.0.0.11:9345", "rke2 join url")


@case("rke2-agent", "k3s / RKE2", distro="rke2", role="agent", token="rke2secret",
      server_url="https://10.0.0.11:9345", node_ip="10.0.0.21", node_taint="gpu=true:NoSchedule")
def _(docs, res):
    check(set(docs[0]) == {"server", "token", "node-ip", "node-taint"}, f"rke2 agent keys {set(docs[0])}")


# --------------------------------------------------------------------------- negative cases
# (type, overrides, substring expected in one of the errors)
NEGATIVE = [
    ("kubeadm", dict(advertise_address="10.0.0.300"), "not a valid"),
    ("kubeadm", dict(bind_port="70000"), "port"),
    ("kubeadm", dict(pod_subnet="10.244.1.0/16"), "Did you mean 10.244.0.0/16"),
    ("kubeadm", dict(pod_subnet="10.96.0.0/16", service_subnet="10.96.0.0/12"), "overlap"),
    ("kubeadm", dict(bootstrap_token="ABCDEF.0123456789abcdef"), "token"),
    ("kubeadm", dict(token_ttl="1 day"), "ttl"),
    ("kubeadm", dict(kubernetes_version="1.37"), "version"),
    ("kubeadm", dict(taints="dedicated=x:Sometimes"), "effect"),
    ("kubeadm", dict(cluster_name="Prod_Cluster"), "clusterName"),
    ("kubeadm", dict(output="join (JoinConfiguration)", join_endpoint="10.0.0.1:6443", join_token=TOKEN), "caCertHash"),
    ("kubeadm", dict(output="join (JoinConfiguration)", join_endpoint="10.0.0.1:6443", join_token=TOKEN,
                     ca_cert_hashes="sha256:xyz"), "sha256"),
    ("kubeadm", dict(output="join (JoinConfiguration)", join_endpoint="10.0.0.1:6443", join_token=TOKEN,
                     unsafe_skip_ca=True, join_control_plane=True, certificate_key="short"), "certificate"),
    ("kind", dict(name="Bad_Name"), "name"),
    ("kind", dict(nodes=[{"role": "worker", "count": "2", "image": "", "labels": ""}]), "control-plane"),
    ("kind", dict(nodes=[{"role": "control-plane", "count": "0", "image": "", "labels": ""}]), "count"),
    ("kind", dict(ip_family="ipv6", pod_subnet="10.244.0.0/16"), "ipv6"),
    ("kind", dict(feature_gates="NotABool=maybe"), "true"),
    ("kind", dict(port_mappings=[{"container_port": "80", "host_port": "99999", "listen_address": "", "protocol": "TCP"}]), "port"),
    ("kind", dict(port_mappings=[{"container_port": "80", "host_port": "8080", "listen_address": "", "protocol": "TCP"},
                                 {"container_port": "81", "host_port": "8080", "listen_address": "", "protocol": "TCP"}]), "8080"),
    ("kubeconfig", dict(), "token"),
    ("kubeconfig", dict(token="x", server="http//nope"), "server"),
    ("kubeconfig", dict(token="x", ca="-----BEGIN CERTIFICATE-----\nnot base64 !!\n-----END CERTIFICATE-----"), "PEM"),
    ("kubeconfig", dict(ca="/x/ca.crt", auth_method="client certificate", client_cert="/x/c.crt"), "key"),
    ("kubeconfig", dict(ca="/x/ca.crt", auth_method="exec plugin"), "command"),
    ("kubeconfig", dict(ca="/x/ca.crt", token="x", namespace="Bad NS"), "namespace"),
    ("k3s / RKE2", dict(role="agent"), "server"),
    ("k3s / RKE2", dict(role="agent", server_url="https://10.0.0.1:6443"), "token"),
    ("k3s / RKE2", dict(role="agent", server_url="https://10.0.0.1:6443", token="t", tls_san="x.example.com"), "agent"),
    ("k3s / RKE2", dict(server_url="10.0.0.1:6443", token="t"), "https"),
    ("k3s / RKE2", dict(service_cidr="10.43.0.0/16", cluster_dns="10.50.0.10"), "service-cidr"),
    ("k3s / RKE2", dict(cluster_cidr="10.42.0.0/16", service_cidr="10.42.128.0/17"), "overlap"),
    ("k3s / RKE2", dict(write_kubeconfig_mode="999"), "mode"),
    ("k3s / RKE2", dict(node_taint="foo=bar"), "Effect"),
    ("k3s / RKE2", dict(distro="rke2", cluster_init=True), "cluster-init"),
]

ROW_CHECKS = [
    ("nodes", {"role": "worker", "count": "x", "image": "", "labels": ""}, True),
    ("nodes", {"role": "worker", "count": "2", "image": "", "labels": "a=b"}, False),
    ("nodes", {"role": "worker", "count": "1", "image": "", "labels": "bad label"}, True),
    ("port_mappings", {"container_port": "80", "host_port": "0", "listen_address": "", "protocol": "TCP"}, True),
    ("port_mappings", {"container_port": "80", "host_port": "80", "listen_address": "1.2.3", "protocol": "TCP"}, True),
    ("port_mappings", {"container_port": "443", "host_port": "8443", "listen_address": "127.0.0.1", "protocol": "TCP"}, False),
]


# --------------------------------------------------------------------------- real tools
def _bin(name):
    for cand in (os.environ.get(name.upper()), shutil.which(name), os.path.join(ROOT, "bin", name),
                 f"/workspace/bin/{name}"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, (p.stdout + p.stderr).strip()


def tool_kubeadm(files):
    kb = _bin("kubeadm")
    if not kb:
        print("  kubeadm not found - skipped")
        return
    _, ver = _run([kb, "version", "-o", "short"])
    print(f"  kubeadm {ver}: config validate")
    for f in files:
        rc, out = _run([kb, "config", "validate", "--config", f])
        ok = check(rc == 0 and "ok" in out.lower(), f"kubeadm config validate {f}: {out[-300:]}")
        print(f"    {'OK ' if ok else 'BAD'} {os.path.relpath(f, HERE)}  ({out.splitlines()[-1] if out else ''})")


def tool_kubectl(files):
    kc = _bin("kubectl")
    if not kc:
        print("  kubectl not found - skipped")
        return
    print("  kubectl config view / get-contexts")
    env = {**os.environ, "KUBECONFIG": ""}
    for f in files:
        rc1, view = _run([kc, "config", "view", "--kubeconfig", f, "-o", "yaml"], env=env)
        rc2, ctx = _run([kc, "config", "get-contexts", "--kubeconfig", f], env=env)
        rc3, raw = _run([kc, "config", "view", "--raw", "--kubeconfig", f, "-o", "yaml"], env=env)
        src = yaml.safe_load(open(f))
        okview = rc1 == 0 and rc3 == 0 and yaml.safe_load(raw).get("clusters") == src.get("clusters")
        names = [c["name"] for c in src["contexts"]]
        okctx = rc2 == 0 and all(n in ctx for n in names)
        check(okview, f"kubectl config view {f}: {view[-300:]}")
        check(okctx, f"kubectl config get-contexts {f}: {ctx[-300:]}")
        print(f"    {'OK ' if okview and okctx else 'BAD'} {os.path.relpath(f, HERE)}  contexts={names}")


HELP_TEXT = {}


def _help_flags(binary, sub):
    rc, out = _run([binary, sub, "--help"])
    HELP_TEXT[(os.path.basename(binary), sub)] = out
    return set(re.findall(r"--([a-z0-9][a-z0-9-]*)", out))


def _help_line(distro, flag):
    for ln in HELP_TEXT.get((distro, "server"), "").splitlines():
        if ln.strip().startswith(f"--{flag} "):
            return ln
    return ""


def tool_rancher(cases):
    for distro in ("k3s", "rke2"):
        b = _bin(distro)
        if not b:
            print(f"  {distro} not found - skipped")
            continue
        _, ver = _run([b, "--version"])
        print(f"  {ver.splitlines()[0]}: generated keys vs `{distro} server|agent --help`")
        flags = {r: _help_flags(b, r) for r in ("server", "agent")}
        check(len(flags["server"]) > 50 and len(flags["agent"]) > 20, f"{distro} help parsed")
        for name, (f, role) in cases.items():
            if not name.startswith(distro):
                continue
            keys = set((yaml.safe_load(open(f)) or {}).keys())
            missing = sorted(k for k in keys if k not in flags[role])
            check(not missing, f"{name}: keys not in `{distro} {role} --help`: {missing}")
            print(f"    {'OK ' if not missing else 'BAD'} {name}: {sorted(keys) or '(empty)'}")
        # every option the GUI can produce must be a real flag, for every role it is offered in
        server_keys = {"token", "server", "node-name", "node-ip", "node-external-ip", "tls-san", "cluster-cidr",
                       "service-cidr", "cluster-dns", "cluster-domain", "write-kubeconfig-mode", "disable",
                       "node-label", "node-taint"}
        server_keys |= {"cluster-init", "flannel-backend"} if distro == "k3s" else {"cni", "ingress-controller"}
        agent_keys = {"token", "server", "node-name", "node-ip", "node-external-ip", "node-label", "node-taint"}
        check(server_keys <= flags["server"], f"{distro} server flags missing: {server_keys - flags['server']}")
        check(agent_keys <= flags["agent"], f"{distro} agent flags missing: {agent_keys - flags['agent']}")
        print(f"    all {len(server_keys)} server / {len(agent_keys)} agent keys the form can emit exist in --help")
        # dropdown / checkbox *values* must be listed in the flag's help text too
        if distro == "k3s":
            values = {"disable": ccg.K3S_DISABLE, "flannel-backend": [v for v in ccg.K3S_FLANNEL if v]}
        else:
            values = {"disable": ccg.RKE2_DISABLE, "cni": [v for v in ccg.RKE2_CNI if v] + ["multus"],
                      "ingress-controller": [v for v in ccg.RKE2_INGRESS if v]}
        for flag, vals in values.items():
            line = _help_line(distro, flag)
            missing = [v for v in vals if not re.search(rf"[\s'\"(,]{re.escape(v)}[\s'\",);]", line + " ")]
            check(line and not missing, f"{distro} --{flag} values not in help: {missing} / {line.strip()[:200]}")
            print(f"    --{flag}: {', '.join(vals)}  {'OK' if line and not missing else 'BAD'}")


def _sudo():
    return ["sudo"] if os.environ.get("KIND_SUDO") else []


def tool_kind(kind_file):
    kind, kc = _bin("kind"), _bin("kubectl")
    if not (kind and kc):
        print("  kind/kubectl not found - skipped")
        return
    cfg = yaml.safe_load(open(kind_file))
    name = cfg["name"]
    _, ver = _run([kind, "version"])
    print(f"  {ver}: kind create cluster --config {os.path.relpath(kind_file, HERE)}")
    _run(_sudo() + [kind, "delete", "cluster", "--name", name])
    t0 = time.time()
    rc, out = _run(_sudo() + [kind, "create", "cluster", "--config", kind_file, "--wait", "180s"])
    print(f"    create rc={rc} in {time.time() - t0:.0f}s")
    if not check(rc == 0, f"kind create cluster failed: {out[-800:]}"):
        print(out[-1500:])
        return
    try:
        rc, raw = _run(_sudo() + [kind, "get", "kubeconfig", "--name", name])
        kraw = yaml.safe_load(raw)
        c, u = kraw["clusters"][0]["cluster"], kraw["users"][0]["user"]
        dec = lambda s: base64.b64decode(s).decode()  # noqa: E731
        # Build a kubeconfig with the generator from *pasted PEM* and test it against the live API server
        res = gen("kubeconfig", cluster_name=f"gen-{name}", server=c["server"], ca=dec(c["certificate-authority-data"]),
                  auth_method="client certificate", client_cert=dec(u["client-certificate-data"]),
                  client_key=dec(u["client-key-data"]), user_name="kind-admin", namespace="kube-system")
        check(res.ok, f"live kubeconfig generation: {res.errors}")
        kfile = write("kind-live-kubeconfig", res)
        os.chmod(kfile, 0o600)
        env = {**os.environ, "KUBECONFIG": ""}
        rc, nodes = _run([kc, "--kubeconfig", kfile, "get", "nodes", "-o", "wide"], env=env)
        print("    generated kubeconfig -> kubectl get nodes:\n      " + "\n      ".join(nodes.splitlines()))
        check(rc == 0 and nodes.count(f"{name}-") == len(cfg["nodes"]), f"kubectl get nodes via generated kubeconfig: {nodes}")
        rc, pods = _run([kc, "--kubeconfig", kfile, "get", "pods", "--no-headers"], env=env)
        check(rc == 0 and "kube-apiserver" in pods, "namespace from context (kube-system) used")
        # reflect what kind applied
        rc, nodes_json = _run([kc, "--kubeconfig", kfile, "get", "nodes", "-o", "yaml"], env=env)
        nj = yaml.safe_load(nodes_json)
        roles = sorted("control-plane" if "node-role.kubernetes.io/control-plane" in n["metadata"]["labels"]
                       else "worker" for n in nj["items"])
        check(roles == sorted(n["role"] for n in cfg["nodes"]), f"node roles {roles}")
        want_labels = [n.get("labels", {}) for n in cfg["nodes"] if n.get("labels")]
        for wl in want_labels:
            check(any(all(n["metadata"]["labels"].get(k) == v for k, v in wl.items()) for n in nj["items"]),
                  f"node labels {wl} applied")
        if cfg.get("networking", {}).get("kubeProxyMode"):
            rc, cm = _run([kc, "--kubeconfig", kfile, "get", "cm", "kube-proxy", "-o", "jsonpath={.data.config\\.conf}"],
                          env=env)
            mode = (yaml.safe_load(cm) or {}).get("mode")
            check(mode == cfg["networking"]["kubeProxyMode"], f"kube-proxy mode {mode}")
            print(f"    kube-proxy mode in cluster: {mode}")
        if cfg.get("featureGates"):
            rc, cmd = _run([kc, "--kubeconfig", kfile, "get", "pod", "-l", "component=kube-apiserver",
                            "-o", "jsonpath={.items[0].spec.containers[0].command}"], env=env)
            fg = re.search(r"--feature-gates=([^\"]+)", cmd)
            want = ",".join(f"{k}={str(v).lower()}" for k, v in sorted(cfg["featureGates"].items()))
            check(fg and fg.group(1) == want, f"apiserver --feature-gates {fg and fg.group(1)} != {want}")
            print(f"    kube-apiserver --feature-gates={fg and fg.group(1)}")
        pm = cfg["nodes"][0].get("extraPortMappings")
        if pm:
            rc, ports = _run(_sudo() + ["docker", "port", f"{name}-control-plane"])
            print("    docker port: " + "; ".join(ports.splitlines()))
            check(all(f"{m['containerPort']}/{m.get('protocol', 'TCP').lower()}" in ports for m in pm),
                  f"extraPortMappings published: {ports}")
        with open(os.path.join(OUT, "kind-live-kubeconfig", "kubectl-get-nodes.txt"), "w") as fh:
            fh.write(nodes + "\n")
        if os.environ.get("KIND_SERVER_KUBECONFIG"):
            # hand the cluster to the manifest dry-run tests
            with open(os.environ["KIND_SERVER_KUBECONFIG"], "w") as fh:
                fh.write(raw)
    finally:
        if os.environ.get("KEEP_KIND"):
            print(f"    KEEP_KIND set - cluster {name} left running")
        else:
            rc, _ = _run(_sudo() + [kind, "delete", "cluster", "--name", name])
            check(rc == 0, "kind delete cluster")
            print(f"    deleted cluster {name}")


def tool_k3s_docker(cfg_file, image="rancher/k3s:v1.37.1-k3s1"):
    if not shutil.which("docker"):
        print("  docker not found - skipped")
        return
    name = "k8sgui-k3s-test"
    d = _sudo() + ["docker"]
    _run(d + ["rm", "-f", name])
    print(f"  docker run {image} server (config.yaml mounted at /etc/rancher/k3s/config.yaml)")
    rc, out = _run(d + ["run", "-d", "--name", name, "--privileged", "--tmpfs", "/run", "--tmpfs", "/var/run",
                        "-v", f"{os.path.abspath(cfg_file)}:/etc/rancher/k3s/config.yaml:ro", image, "server"])
    if not check(rc == 0, f"docker run k3s: {out}"):
        return
    try:
        want = yaml.safe_load(open(cfg_file)) or {}
        node = want.get("node-name", "")
        ready = False
        for _ in range(60):
            time.sleep(3)
            rc, out = _run(d + ["exec", name, "kubectl", "get", "nodes", "--no-headers"])
            if rc == 0 and " Ready" in out:
                ready = True
                break
        print(f"    kubectl get nodes (inside container): {out}")
        check(ready, f"k3s node Ready: {out}")
        if node:
            check(out.startswith(node), f"node-name {node} applied")
        rc, labels = _run(d + ["exec", name, "kubectl", "get", "node", "-o", "jsonpath={.items[0].metadata.labels}"])
        for lab in want.get("node-label", []):
            k, v = lab.split("=", 1)
            check(f'"{k}":"{v}"' in labels, f"node-label {lab} applied")
        rc, taints = _run(d + ["exec", name, "kubectl", "get", "node", "-o", "jsonpath={.items[0].spec.taints}"])
        for t in want.get("node-taint", []):
            check(t.split("=")[0] in taints, f"node-taint {t} applied: {taints}")
        if "disable" in want:
            for _ in range(20):
                rc, pods = _run(d + ["exec", name, "kubectl", "get", "pods", "-A", "--no-headers"])
                if "coredns" in pods:
                    break
                time.sleep(3)
            for comp in want["disable"]:
                check(comp not in pods, f"disabled component {comp} not running")
            print("    kube-system pods: " + ", ".join(sorted({ln.split()[1].rsplit('-', 2)[0]
                                                               for ln in pods.splitlines() if ln.strip()})))
        if want.get("cluster-cidr"):
            rc, cidr = _run(d + ["exec", name, "kubectl", "get", "node", "-o", "jsonpath={.items[0].spec.podCIDR}"])
            import ipaddress
            check(rc == 0 and ipaddress.ip_network(cidr).subnet_of(ipaddress.ip_network(want["cluster-cidr"])),
                  f"podCIDR {cidr} inside cluster-cidr")
            print(f"    node podCIDR: {cidr} (cluster-cidr {want['cluster-cidr']})")
        rc, mode = _run(d + ["exec", name, "stat", "-c", "%a", "/etc/rancher/k3s/k3s.yaml"])
        if want.get("write-kubeconfig-mode"):
            check(mode.strip() == want["write-kubeconfig-mode"].lstrip("0"), f"kubeconfig mode {mode}")
            print(f"    /etc/rancher/k3s/k3s.yaml mode: {mode}")
    finally:
        _run(d + ["rm", "-f", name])
        print(f"    removed container {name}")


# --------------------------------------------------------------------------- main
def main():
    args = set(sys.argv[1:])
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)

    print("== defaults")
    for t in ccg.CONFIG_TYPES:
        r = gen(t)
        if t == "kubeconfig":
            check(not r.ok and any("token" in e.lower() for e in r.errors) and any("ca" in e.lower() for e in r.errors),
                  "kubeconfig defaults ask for CA + token")
        else:
            check(r.ok, f"{t} defaults valid: {r.errors}")
        check(set(ccg.defaults(t)) == {f.key for _, fs in ccg.SCHEMA[t] for f in fs}, f"{t} defaults keys")
        d1 = ccg.defaults(t)
        for v in d1.values():
            if isinstance(v, list):
                v.append({})
        check(ccg.defaults(t) != d1 or not any(isinstance(v, list) for v in d1.values()), f"{t} defaults deep-copied")
    print("== positive cases")
    files = {"kubeadm": [], "kind": [], "kubeconfig": [], "rancher": {}}
    for name, (ctype, over, fn) in CASES.items():
        res = gen(ctype, **over)
        if not check(res.ok, f"{name}: unexpected errors {res.errors}"):
            continue
        path = write(name, res)
        text = open(path).read()
        try:
            docs = docs_of(text)
        except yaml.YAMLError as exc:
            check(False, f"{name}: YAML parse {exc}")
            continue
        if ctype == "k3s / RKE2" and not docs:
            docs = [{}]
        check(docs == [d or {} for d in res.docs] or (ctype == "k3s / RKE2" and docs == [{}]),
              f"{name}: rendered YAML round-trips")
        fn(docs, res)
        print(f"  ok  {name:26s} -> {os.path.relpath(path, HERE)}"
              + (f"   ({len(res.warnings)} warning(s))" if res.warnings else ""))
        if ctype == "kubeadm":
            files["kubeadm"].append(path)
        elif ctype == "kind":
            files["kind"].append(path)
        elif ctype == "kubeconfig":
            files["kubeconfig"].append(path)
        else:
            files["rancher"][name] = (path, over.get("role", "server"))
    print("== negative cases")
    for ctype, over, want in NEGATIVE:
        res = gen(ctype, **over)
        hit = any(want.lower() in e.lower() for e in res.errors)
        check(hit, f"{ctype} {over}: expected error containing {want!r}, got {res.errors}")
    print(f"  {len(NEGATIVE)} negative cases checked")
    for key, row, bad in ROW_CHECKS:
        errs = ccg.check_row(key, row)
        check(bool(errs) == bad, f"check_row {key} {row}: {errs}")
    print(f"  {len(ROW_CHECKS)} row checks")

    if "--tools" in args:
        print("== real tools")
        tool_kubeadm(files["kubeadm"])
        tool_kubectl(files["kubeconfig"])
        tool_rancher(files["rancher"])
    if "--kind" in args:
        print("== kind create cluster (real)")
        res = gen("kind", name="k8sgui-cfgtest", feature_gates="InPlacePodVerticalScaling=true",
                  nodes=[{"role": "control-plane", "count": "1", "image": os.environ.get("KIND_IMAGE", ""), "labels": "ingress-ready=true"},
                         {"role": "worker", "count": "1", "image": os.environ.get("KIND_IMAGE", ""), "labels": "tier=apps"}],
                  port_mappings=[{"container_port": "30080", "host_port": "18080", "listen_address": "127.0.0.1",
                                  "protocol": "TCP"}],
                  api_server_address="127.0.0.1", kube_proxy_mode="nftables", pod_subnet="10.244.0.0/16",
                  service_subnet="10.96.0.0/16")
        check(res.ok, f"kind live config: {res.errors}")
        tool_kind(write("kind-live", res))
    if "--k3s-docker" in args:
        print("== k3s in docker (real)")
        res = gen("k3s / RKE2", distro="k3s", role="server", token="k8sgui-test-token", node_name="k3s-gen",
                  write_kubeconfig_mode="0644", k3s_disable_traefik=True, k3s_disable_metrics_server=True,
                  node_label="tier=test", node_taint="dedicated=test:NoSchedule", cluster_cidr="10.52.0.0/16",
                  service_cidr="10.53.0.0/16", cluster_dns="10.53.0.10", tls_san="k3s.local",
                  flannel_backend=os.environ.get("K3S_FLANNEL", "host-gw"))   # vxlan needs the vxlan kernel module
        check(res.ok, f"k3s live config: {res.errors}")
        tool_k3s_docker(write("k3s-live", res))

    shutil.rmtree(PKI_DIR, ignore_errors=True)
    print(f"\nCLUSTER CONFIG RESULT: {'PASS' if not FAIL else 'FAIL'}  ({PASS} passed, {FAIL} failed)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
