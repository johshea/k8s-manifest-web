# Testing

```bash
pip install -r requirements.txt playwright      # in your venv
python -m playwright install chromium            # once; or set CHROME=/path/to/chrome
./run_tests.sh              # manifest + cluster-config + help-coverage tests, kubectl client
                            # dry-run (skipped when no API server is reachable - kubectl needs discovery),
                            # real-tool checks for whatever is installed, web API + browser e2e tests
                            # (use PYTHON=.venv/bin/python if the venv isn't active)
./run_tests.sh --server     # also kubectl --dry-run=server (needs a cluster; creates ns "demo")
KIND_SUDO=1 ./run_tests.sh --kind --k3s-docker   # + real kind cluster and k3s-in-docker boots
python3 tests/test_generator.py            # headless only
python3 tests/test_field_docs.py [-v]      # hover-help coverage (-v prints every tooltip)
python tests/test_web.py                   # web API tests (Flask test client, no browser)
python tests/web_e2e_test.py --screenshots docs/images   # browser end-to-end test + docs/images/web-*.png
python3 tests/test_app_templates.py        # application templates, headless (2,000+ checks)
KIND_SUDO=1 ./run_tests.sh --templates-kind          # + temporary kind cluster: dry-run client+server
KIND_SUDO=1 ./run_tests.sh --templates-kind --deploy # + deploy every template, wait for Ready, client checks
KIND_SUDO=1 python3 tests/test_app_templates.py --kind --deploy="PostgreSQL,nginx"   # just these
python3 tests/test_cluster_config.py --tools   # kubeadm config validate, kubectl config view/get-contexts,
                                               # k3s/rke2 --help flag checks
python3 tests/test_cluster_platforms.py --tools    # all platform tools, offline (see the platform table)
                                                   # (EKS / OpenShift schema checks need `pip install jsonschema`)
KIND_SUDO=1 ./run_tests.sh --minikube --capi       # + real minikube boot/delete and CAPI on a temporary kind cluster
```

Platform tools are looked up in `./bin`, `/workspace/bin`, then `PATH` (gcloud and az also in
`/workspace/tools/google-cloud-sdk/bin` and `/workspace/tools/azvenv/bin`). The gcloud / az
checks only read `--help` (with a throw-away `CLOUDSDK_CONFIG` / `AZURE_CONFIG_DIR` and GCE
metadata lookup disabled) - they never log in or call a cloud API. The Kubespray check needs a
kubespray 2.32.0 checkout at `/workspace/tools/kubespray/kubespray-2.32.0`, the
`ansible.utils` collection and `netaddr`. `--minikube` boots profile `k8sgui-mk` in a temporary
`MINIKUBE_HOME` and deletes it; `--capi` creates/deletes kind cluster `k8sgui-capi`.

Tools are found on `PATH`, then `./bin`, then `/workspace/bin` (override with
`KUBEADM=`, `KIND=`, `KUBECTL=`, `K3S=`, `RKE2=`); missing tools are reported as skipped.
`--kind` creates the cluster `k8sgui-cfgtest` (1 control-plane + 1 worker, port mapping,
featureGates, nftables kube-proxy), checks nodes/labels/kube-proxy mode/feature gates/ports
through a **generated kubeconfig built from pasted PEMs**, then deletes it (`KEEP_KIND=1`
keeps it). `--k3s-docker` runs `rancher/k3s:v1.37.1-k3s1` with the generated config mounted
and uses `flannel-backend: host-gw` because many CI kernels/containers lack the vxlan module
(set `K3S_FLANNEL=vxlan` to override).

`kubectl apply --dry-run=client` still needs API discovery (and the OpenAPI schema
for validation) from a cluster. With no cluster it fails with
`unable to recognize ... connection refused` even with `--validate=false`; use a
local cluster (kind, minikube, Docker Desktop, k3d) for real validation.

The template deploy test (`--templates-kind --deploy`) creates kind cluster `tpl-test` with
kube-proxy in **nftables** mode (`KIND_PROXY_MODE` overrides it; on the test box iptables mode
couldn't program Services), pulls each image on the host and `kind load`s it into the node
(the kind node's own registry DNS didn't work there; `PRELOAD=0` turns this off), deploys the
templates in batches into `t-<template>` namespaces, waits for `kubectl rollout status`, runs
the client checks from a busybox debug pod or the app's own CLI, deletes the namespaces, and
finally deletes the cluster and the images it pulled (`KEEP_KIND=1` / `KEEP_IMAGES=1` keep
them). The full run takes about 25 minutes and needs about 4 GB of free RAM (Elasticsearch,
OpenSearch, Cassandra and Jenkins run one at a time).

## Networking & Storage tests

```bash
python3 tests/test_addons.py            # every CNI / CSI type: rich + default cases, 30+ negative cases,
                                        # cross-checks, session StorageClass options (output: tests/output-addons/)
python3 tests/test_addons.py --tools    # + `helm template` of every generated values.yaml against the pinned charts
python3 tests/test_addons.py --server   # + kubectl --dry-run=server of every YAML (needs a cluster with the CRDs)
```

Cloud CSI classes (EBS, EFS, GCE PD, Azure, vSphere, Nutanix) are only generator / server-dry-run
checked - no cloud credentials are used.

## Screenshots

`./run_tests.sh` regenerates `docs/images/web-*.png` through the browser end-to-end test (`--screenshots docs/images`). The fresh captures are not optimised. Before committing them, compress them losslessly, for example
`oxipng -o 4 --strip safe docs/images/*.png` (or `pip install pyoxipng`), which saves about 25 %.
