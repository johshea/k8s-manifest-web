# Running in Docker

The repository ships a `Dockerfile` that serves the web UI with **gunicorn** (two workers) on a
`python:3.13-slim` base, running as user `nobody`.

```bash
docker build -t k8s-manifest-web .
docker run --rm -p 127.0.0.1:8080:8080 k8s-manifest-web      # then open http://localhost:8080
```

- Inside the container the server binds `0.0.0.0:8080`. Publish it on **127.0.0.1** as shown above
  unless you really want it reachable from the network. The app has **no login**; see
  [security.md](security.md).
- The **Host-header guard** (DNS-rebinding protection) is preset to
  `K8SGUI_ALLOWED_HOSTS=localhost,127.0.0.1`. If you reach the container under another name, add
  it, for example `-e K8SGUI_ALLOWED_HOSTS=localhost,127.0.0.1,k8s-tools.lan`. `*` turns the check off.
- The image contains only the runtime files: the generator core (`manifest_gen.py`,
  `cluster_config_gen.py`, `clusterplatforms/`, `addons_gen.py`, `addons/`, `app_templates.py`,
  `field_docs.py`, `providers.py`), `bootstrap.py`, `k8s_manifest_web.py` and `web/`. Tests, tools,
  docs and screenshots are excluded by `.dockerignore`.
- A `HEALTHCHECK` polls `GET /healthz` every 30 s.
- Access logs go to stdout (`--access-logfile -`). Request bodies, which can contain Secret
  values, are never logged.

The same gunicorn setup works without Docker:

```bash
pip install gunicorn
gunicorn -b 127.0.0.1:8080 -w 2 'web.app:create_app()'
```
