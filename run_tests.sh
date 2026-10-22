#!/usr/bin/env bash
# Run the web (Flask) project tests; use the Python that has Flask (+ Playwright for e2e), e.g. PYTHON=.venv/bin/python. Usage: ./run_tests.sh [--server] [--kind] [--k3s-docker] [--minikube] [--capi] [--templates-kind] [--deploy]
#   --server      also run `kubectl apply --dry-run=server` for the manifests (needs a reachable cluster)
#   --kind        create a real kind cluster from a generated kind-config.yaml, test a generated kubeconfig
#                 against it, then delete it (set KIND_SUDO=1 if docker needs sudo)
#   --k3s-docker  boot rancher/k3s in docker with a generated config.yaml and wait for Ready
#   --minikube    boot minikube (docker driver) from a generated start-minikube.sh, check it, delete it
#   --capi        kind management cluster + Cluster API v1beta2 CRDs + server dry-run of the CAPD template
#   --templates-kind  temporary kind cluster: client+server dry-run of every application template
#   --deploy      with --templates-kind: also deploy the templates for real, wait for Ready, run client checks
# Platform tools (minikube, k0s, talosctl, eksctl, gcloud, az, openshift-install, ansible + kubespray) are used
# offline when found; nothing ever talks to a cloud API or uses cloud credentials.
# Real tools are looked up on PATH, then ./bin and /workspace/bin (kubectl, kubeadm, kind, k3s, rke2).
set -u
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
export PATH="$PATH:$PWD/bin:/workspace/bin"
rc=0
gen_args=() ; cc_args=(--tools) ; pf_args=(--tools) ; tp_args=(--kubectl) ; tp_extra=()
for a in "$@"; do
  case "$a" in
    --server) gen_args+=(--server) ;;
    --kind|--k3s-docker) cc_args+=("$a") ;;
    --minikube|--capi) pf_args+=("$a") ;;
    --templates-kind) tp_args=(--kind) ;;
    --deploy|--deploy=core) tp_extra+=("$a") ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done
echo "### 1. Manifest generator tests (+ kubectl dry-run if kubectl is on PATH)"
$PY tests/test_generator.py --kubectl "${gen_args[@]}" || rc=1
echo
echo "### 2. Cluster config generator tests (+ kubeadm / kubectl config / k3s / rke2 when found)"
$PY tests/test_cluster_config.py "${cc_args[@]}" || rc=1
echo
echo "### 3. Cluster platform tests (minikube, k0s, MicroK8s, Talos, Kubespray, Cluster API, OpenShift, EKS, GKE, AKS)"
$PY tests/test_cluster_platforms.py "${pf_args[@]}" || rc=1
echo
echo "### 4. Application template tests (+ kubectl dry-run / temporary kind cluster)"
$PY tests/test_app_templates.py "${tp_args[@]}" "${tp_extra[@]}" || rc=1
echo
echo "### 5a. Networking & Storage add-ons (CNI / CSI generators, cross-checks; helm template when helm is found)"
$PY tests/test_addons.py --tools || rc=1
echo
echo "### 5. Hover-help coverage (every field / column / section / template parameter has real help text)"
$PY tests/test_field_docs.py || rc=1
echo
echo "### 6. Web UI (Flask) API tests + browser end-to-end test (skipped when Flask / Playwright are missing)"
$PY tests/test_web.py || rc=1
$PY tests/web_e2e_test.py --screenshots docs/images || rc=1   # CHROME=/path/to/chrome if not /usr/bin/google-chrome
exit $rc
