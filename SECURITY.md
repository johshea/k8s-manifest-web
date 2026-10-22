# Security Policy

## Supported versions

Security fixes go into the latest release on `main`.

| Version | Supported |
|---|---|
| 1.0.x | ✅ |
| < 1.0 | ❌ |

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/johshea/k8s-manifest-web/security/advisories/new) (Security tab →
"Report a vulnerability").

Please include the affected version or commit, the steps to reproduce, and the impact. You should get an
acknowledgement within a few days. Once a fix is released the report is credited, unless you'd
rather stay anonymous.

## Scope

In scope:

- the Flask server (`web/app.py`): request handling, the Host-header and content-type guards,
  CSP and headers, and anything that could leak form values (which may contain Secrets)
- generated output that is insecure by default (for example world-readable Secrets in a download)
- the Docker image

Out of scope: the security of clusters you create with the generated files. Review generated configs
before you apply them, especially the demo-only components such as the in-cluster NFS test server.
See [docs/security.md](docs/security.md) for the app's security model.
