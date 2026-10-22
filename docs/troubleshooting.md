# Troubleshooting

| Symptom | Fix |
|---|---|
| `400 unexpected Host header` | You opened it by a name other than localhost / 127.0.0.1 (or through a proxy): set `K8SGUI_ALLOWED_HOSTS=myhost` (or `*`). |
| `Address already in use` | Another program uses port 8080: `python3 k8s_manifest_web.py --port 9000`. |
| Downloaded kubeconfig / Secret is world-readable | Browsers can't set modes: `chmod 600 file`, or use the zip downloads, which carry the modes. |

| `error: externally-managed-environment` | Handled automatically; or use the venv option, or `sudo apt install python3-yaml`. |
| `No module named pip` (Ubuntu) | `sudo apt install python3-pip` (or `python3-venv` for venvs). |
| Native sidecar never starts / `restartPolicy` rejected on an initContainer | Cluster is older than 1.29 (or 1.28 without the SidecarContainers gate): use role `sidecar (regular container)` instead. |
| `kubectl apply` says `strict decoding error`/unknown field | Upgrade kubectl/cluster; fields like CronJob `timeZone` need k8s ≥1.27. |
| `kubeadm config validate` says `no kind "InitConfiguration" is registered for version "kubeadm.k8s.io/v1beta4"` | kubeadm older than 1.31 - upgrade kubeadm (v1beta4 is the current API) or migrate with `kubeadm config migrate`. |
| `kind create cluster`: `permission denied ... docker.sock` | Add yourself to the `docker` group (`sudo usermod -aG docker $USER`, re-login) or run with `sudo` (`KIND_SUDO=1` for the tests). |
| k3s exits with `failed to create vxlan device: operation not supported` | Kernel lacks vxlan (common in containers/VMs): choose `flannel-backend: host-gw` or `wireguard-native`. |
| kubectl ignores `certificate-authority: ~/...` | kubectl doesn't expand `~` in kubeconfig paths - use an absolute path or paste the PEM. |
