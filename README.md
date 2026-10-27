# Kubernetes Manifest Generator (web)

A local web app (Python + Flask, vanilla JS, no CDN) that builds Kubernetes YAML, cluster configs and CNI/CSI add-on configs from forms in your browser.

[![CI status](https://github.com/johshea/k8s-manifest-web/actions/workflows/ci.yml/badge.svg)](https://github.com/johshea/k8s-manifest-web/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![Web UI in Manifests mode: form on the left, live YAML preview and validation on the right](docs/images/web-manifests.png)

You fill in a form and get valid YAML, so you never hand-type indentation. Every field has an ⓘ tooltip
that says what it does, the format it takes and the YAML key it sets. All generation runs
server-side in a headless core that you can also use from scripts. The server stores nothing.
Your values stay in your browser's localStorage.

## Features

- **Three modes**, switched with the **Mode** dropdown:
  - **Manifests**: 14 kinds (Deployment, StatefulSet, DaemonSet, Job, CronJob, Service, Ingress,
    ConfigMap, Secret, PersistentVolumeClaim, Namespace, NetworkPolicy, StorageClass,
    VolumeSnapshotClass). Includes multi-container pods (init, sidecar and native sidecar),
    tolerations, affinity, topology spread and probes.
  - **Cluster config**: kubeadm, kind, kubeconfig, k3s / RKE2, minikube, k0s, MicroK8s, Talos,
    Kubespray, Cluster API, OpenShift, EKS, GKE and AKS.
  - **Networking & Storage**: CNI add-ons (Calico, Cilium, Flannel, Canal, Antrea, Multus) and CSI
    StorageClasses for 15 drivers (AWS EBS / EFS, GCE PD, Azure Disk / File, vSphere, Nutanix,
    NFS, SMB, Longhorn, Rook-Ceph RBD / CephFS, OpenEBS, TopoLVM, local-path). Each comes with an
    `install.sh` that pins verified versions.
- **25 application templates** (PostgreSQL, MySQL, Redis, Kafka, nginx, WordPress, ...). Each one
  fills several related kinds at once.
- **Live YAML preview** and **validation**, plus optional cross-checks that warn when a CNI's
  pod CIDR doesn't match the cluster's.
- **Download, Download all edited (.zip), bundles**: zip downloads keep the Unix modes (0600 for
  Secrets and kubeconfigs, 0755 for scripts).
- **Local-first security**: binds 127.0.0.1 by default. Strict CSP, Host-header check,
  JSON-only POSTs and no request logging. See [docs/security.md](docs/security.md).
- **Keyboard shortcuts**: Ctrl/Cmd+K opens the type picker, Ctrl/Cmd+S downloads and
  Ctrl/Cmd+Enter refreshes the preview.

## Quick start

Python **3.10+** and any modern browser are required.

### macOS

```bash
git clone https://github.com/johshea/k8s-manifest-web.git && cd k8s-manifest-web
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # PyYAML + Flask
python k8s_manifest_web.py --open        # http://127.0.0.1:8080
```

### Ubuntu / Debian

```bash
sudo apt install -y python3 python3-venv
git clone https://github.com/johshea/k8s-manifest-web.git && cd k8s-manifest-web
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python k8s_manifest_web.py --open
```

Useful options are `--port 9000`, `--check` (dependency check only) and `--no-install`. To run it in
a container:

```bash
docker build -t k8s-manifest-web . && docker run --rm -p 127.0.0.1:8080:8080 k8s-manifest-web
```

See [docs/installation.md](docs/installation.md) and [docs/docker.md](docs/docker.md).

## Usage overview

1. Pick a **Mode**, then a **Resource type**, a cluster **Config type** or an **Add-on type**. In
   Manifests mode you can also start from a **Template**.
2. Fill in the form. Hover over, focus or tap any ⓘ for help. Sections collapse, and tables open a
   row dialog with the same validation the generator uses.
3. Watch the **YAML preview** and the **Validation** pane. Then **Copy**, **Download**,
   **Download all edited (.zip)**, or build a multi-document **bundle**.

From code, without the web server:

```python
import manifest_gen as gen
res = gen.generate("Deployment", {"name": "web", "image": "nginx:1.27", "replicas": "3"})
print(gen.to_yaml(res.manifest) if res.ok else res.errors)
```

## Documentation

| Topic | Document |
|---|---|
| Installation, prerequisites, auto-install | [docs/installation.md](docs/installation.md) |
| Running in Docker / gunicorn | [docs/docker.md](docs/docker.md) |
| Security model (binding, CSP, Host check, no state) | [docs/security.md](docs/security.md) |
| Manifests mode: features, browser specifics, field formats | [docs/manifests.md](docs/manifests.md) |
| Application templates and how to add one | [docs/templates.md](docs/templates.md) |
| Cluster config mode and how each platform was validated | [docs/cluster-config.md](docs/cluster-config.md) |
| Networking & Storage (CNI / CSI) mode | [docs/networking-storage.md](docs/networking-storage.md) |
| Hover help / field reference (`field_docs.py`) | [docs/tooltips.md](docs/tooltips.md) |
| Architecture, file layout, HTTP API, using the generator from code | [docs/architecture.md](docs/architecture.md) |
| Testing (API, browser e2e, real-tool checks, kind clusters) | [docs/testing.md](docs/testing.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |

There is also a [documentation index](docs/README.md).

## Project layout

```text
.
├── k8s_manifest_web.py      # entry point (dependency check, starts Flask)
├── web/                     # Flask app (app.py), templates/index.html, static/ (app.js, app.css)
├── bootstrap.py             # dependency checks, PyYAML / Flask installer
├── manifest_gen.py          # ┐
├── cluster_config_gen.py    # │ UI-free generator core
├── clusterplatforms/        # │
├── addons_gen.py, addons/   # │
├── app_templates.py         # │
├── field_docs.py            # │
├── providers.py             # ┘
├── tests/                   # test suites
├── docs/                    # documentation + docs/images
├── Dockerfile, .dockerignore
├── run_tests.sh             # runs every suite
└── .github/                 # CI, issue / PR templates, Dependabot
```

## Testing

```bash
pip install playwright                          # optional, for the browser e2e test
./run_tests.sh                                  # all offline suites + Flask API tests + e2e (CHROME=/path/to/chrome)
python3 tests/test_web.py                       # API tests only
KIND_SUDO=1 ./run_tests.sh --kind --templates-kind   # optional real-cluster checks
```

The suites cover:

- the generator core: manifests, cluster configs, platforms, ~2,300 template checks and the CNI / CSI add-ons
- tooltip coverage for every field
- 964 Flask API checks
- a Playwright browser test


## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Report vulnerabilities as described in
[SECURITY.md](SECURITY.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © 2026 John Shea
