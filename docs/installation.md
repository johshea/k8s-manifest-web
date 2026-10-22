# Installation

## Prerequisites

- Python **3.10+** (tested 3.10-3.13; no GUI toolkit needed) and any modern browser
- PyYAML + Flask (installed automatically on first run, or via `requirements.txt`)
- Optional: `kubectl` to apply / dry-run the output; Playwright + Chrome/Chromium for the e2e test

## Running it

```bash
# macOS or Ubuntu - a venv is the cleanest option
cd k8s-manifest-web
python3 -m venv .venv && source .venv/bin/activate     # Ubuntu: sudo apt install python3-venv
pip install -r requirements.txt                        # PyYAML + Flask
python k8s_manifest_web.py --open                      # http://127.0.0.1:8080, opens your browser

# without a venv: the first run pip-installs PyYAML and Flask
# (--user, then --user --break-system-packages on PEP 668 systems; only touches ~/.local)
python3 k8s_manifest_web.py
python3 k8s_manifest_web.py --port 9000                # other port
python3 k8s_manifest_web.py --check                    # dependency check only
python3 k8s_manifest_web.py --no-install               # never pip-install (prints apt / venv hints)
```

Ubuntu packages also work: `sudo apt install python3-flask python3-yaml`. Auto-install can be
turned off with `K8S_GUI_NO_AUTO_INSTALL=1`, and the `--break-system-packages` retry with
`K8S_GUI_NO_BREAK_SYSTEM=1`; if pip is missing it tries `ensurepip --user` first.

Optional production-style serving (the Flask dev server is fine for one local user):

```bash
pip install gunicorn
gunicorn -b 127.0.0.1:8080 -w 2 'web.app:create_app()'
docker build -t k8s-manifest-web . && docker run --rm -p 127.0.0.1:8080:8080 k8s-manifest-web
```

See [docker.md](docker.md) for the container image.
