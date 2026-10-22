# Security notes

- It binds **127.0.0.1** by default. `--host 0.0.0.0` makes it reachable from the network
  **without any login**, and it prints a warning. Prefer an SSH tunnel
  (`ssh -L 8080:127.0.0.1:8080 host`). `--debug` is refused on non-loopback addresses.
- Debug mode is off unless you pass `--debug`. Requests are limited to 2 MiB. POSTs must be
  `application/json`, which a foreign web page can't send without a CORS preflight that this app
  never grants, so this works as a cheap CSRF guard. A **Host-header check** stops DNS-rebinding:
  only `localhost` / `127.0.0.1` / `[::1]` are accepted on loopback. Set `K8SGUI_ALLOWED_HOSTS`
  (comma-separated, `*` = off) for other setups. The Dockerfile allows `localhost,127.0.0.1`.
- Responses carry a strict CSP (`default-src 'self'`, no inline scripts, no third-party content),
  plus `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, and `no-store` for the API.
- Values travel only in POST bodies, never in URLs, so access logs only show paths. The server
  keeps no state, writes no files and never logs request bodies (generator exceptions are logged
  by type only).
- The frontend is plain JS + CSS with no CDN or external URLs, so it works offline.
