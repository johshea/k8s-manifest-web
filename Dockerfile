# Web UI in a container:
#   docker build -t k8s-manifest-web .
#   docker run --rm -p 127.0.0.1:8080:8080 k8s-manifest-web      # then open http://localhost:8080
# The container binds 0.0.0.0 inside, so publish it on 127.0.0.1 as above unless you want it on the
# network (no login!). Host-header guard: K8SGUI_ALLOWED_HOSTS (comma-separated, '*' = off).
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt "gunicorn>=21"
COPY manifest_gen.py cluster_config_gen.py addons_gen.py app_templates.py field_docs.py providers.py bootstrap.py k8s_manifest_web.py ./
COPY clusterplatforms ./clusterplatforms
COPY addons ./addons
COPY web ./web
ENV K8SGUI_ALLOWED_HOSTS=localhost,127.0.0.1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER nobody
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "--access-logfile", "-", "web.app:create_app()"]
