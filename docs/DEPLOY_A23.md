# DEPLOY_A23 - production deployment record

Deployment of the A23Font M2 production-reachability slice to the A23 phone
(Android/Termux + Debian chroot Docker), exposed as
`https://font.esma.eu.org` through the existing host-side cloudflared tunnel.

Runbook: `deploy/a23/deploy.sh` and `deploy/a23/README.md`.

## Real Values

(filled as live phases complete; no secrets in this file)

| Item | Value |
| --- | --- |
| Deploy date | _pending_ |
| Tailscale IP used | _pending_ |
| Chroot app path | /data/local/chroot/debian/opt/a23font (chroot: /opt/a23font) |
| Chosen web port | _pending_ (default 8090) |
| Docker image | a23font:v1 |
| Data volume | a23font-data -> /data |
| Public hostname | font.esma.eu.org |
| Tunnel id / name | _pending_ |
| Tunnel ingress backup | see below |
| Cloudflare zone (esma.eu.org) id | _pending_ |
| DNS record id (font CNAME) | _pending_ |
| Pushed commit sha | _pending_ |

## Tunnel ingress rollback reference

Existing cloudflared tunnel configuration captured BEFORE adding the
`font.esma.eu.org` entry (rollback reference - restore verbatim if needed):

```json
_pending_
```

## Verification evidence

- `/health/live` via tunnel: _pending_
- `/health/ready` via tunnel: _pending_
- Home page via tunnel: _pending_
- Local regression (status.sh, monitor 2080, a23-cloudflare-ddns, free -m): _pending_

## Rollback demonstration (M2.A4)

- `deploy.sh down` -> local port closed + tunnel non-200: _pending_
- `deploy.sh up` -> 200 restored: _pending_

## Manual fallback (only if Cloudflare API rejects config PUT)

If the tunnel is remotely managed and the API refuses configuration changes,
add the ingress entry manually in the Cloudflare Zero Trust dashboard:

1. Zero Trust -> Networks -> Tunnels -> select the tunnel -> Public Hostname.
2. Add hostname `font.esma.eu.org`, service `http://localhost:<port>`.
3. Ensure DNS CNAME `font -> <tunnel-id>.cfargotunnel.com` (proxied) exists.