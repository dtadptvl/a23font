# DEPLOY_A23 - production deployment record

Deployment of the A23Font M2 production-reachability slice to the A23 phone
(Android/Termux + Debian chroot Docker), exposed as
`https://font.esma.eu.org` through the existing host-side cloudflared tunnel.

Runbook: `deploy/a23/deploy.sh` and `deploy/a23/README.md`.

## Real Values

(filled as live phases complete; no secrets in this file)

| Item | Value |
| --- | --- |
| Deploy date | 2026-08-31 (UTC), task T-002 r1 d2 |
| Tailscale IP used | 100.88.133.27 (sshd port 8022; Termux user + Magisk su) |
| Chroot app path | /data/local/chroot/debian/opt/a23font (chroot: /opt/a23font) |
| Chosen web port | 8090 (verified free on Android + chroot listener lists before use) |
| Docker image | a23font:v1 (commit-built offline, sha256:08f82423b961...) |
| Data volume | a23font-data -> /data (both containers; survives deploy.sh down) |
| Public hostname | font.esma.eu.org |
| Tunnel id / name | 5f46d0a2-573b-486f-8fe0-baea46333811 / samsung-a23-home-server |
| Tunnel config version | 4 (backup below) -> 5 (with font entry) |
| Tunnel ingress backup | see below |
| Cloudflare zone (esma.eu.org) id | ec36344a0f4b1b0519f347c57a275c8d (account 91e649b9aa1e8c8d41ca7ffe7f4b8bd7) |
| DNS record id (font CNAME) | 2d56b8923d308de384fb0284875ad343 (CNAME -> 5f46d0a2-573b-486f-8fe0-baea46333811.cfargotunnel.com, proxied) |
| Pushed commit sha | see git log origin/main (deploy commits are T-002-prefixed) |

## Tunnel ingress rollback reference

Existing cloudflared tunnel configuration captured BEFORE adding the
`font.esma.eu.org` entry (rollback reference - restore verbatim if needed):

```json
{
  "ingress": [
    { "service": "http://127.0.0.1:2080", "hostname": "a23.esma.eu.org" },
    { "service": "http://localhost:3001", "hostname": "12a1-stage.tuananhdg.eu.org", "originRequest": {} },
    { "service": "http_status:404" }
  ],
  "warp-routing": { "enabled": false }
}
```

(verbatim config object of tunnel configuration version 4, fetched
2026-08-31T22:0x UTC; API response `source: "cloudflare"` - remotely managed.
Raw API backup kept locally outside the repo during the task; the PUT below
inserts the new hostname entry BEFORE the catch-all `http_status:404`,
keeping every existing entry byte-identical and in order.)

## Verification evidence (2026-08-31 ~22:05 UTC)

- `/health/live` via tunnel: HTTP 200 body `{"status":"ok","service":"a23font"}`
- `/health/ready` via tunnel: HTTP 200 body `{"status":"ready"}`
- Home page via tunnel: HTTP 200, `<title>A23Font</title>` (793 bytes)
- Local on phone: `curl http://127.0.0.1:8090/health/live` -> ok; worker
  logs heartbeat every 12th idle iteration (`"msg": "heartbeat", "iteration": 12, "idle": 12`)
- Containers: `a23font-web`/`a23font-worker` Up, restart=unless-stopped,
  restartCount=0, network host, volume a23font-data->/data
- Regression after deploy and again after rollback cycle:
  `status.sh` -> "Cloudflare Tunnel is healthy (PID 6838, 4 ready connections
  expected)"; monitor `http://127.0.0.1:2080/` -> 200; `a23-cloudflare-ddns`
  Up (6h, untouched); free -m total 3590 / available ~2150 MB, swap ~589/4095 MB

## Rollback demonstration (M2.A4) - recorded 2026-08-31 ~22:04 UTC

- `deploy.sh down` -> containers removed; local curl rc=7 (connection
  refused on 127.0.0.1:8090); `https://font.esma.eu.org/health/live`
  through Cloudflare -> 502 (3 consecutive probes)
- `deploy.sh up` -> both containers Up; local health ok;
  `https://font.esma.eu.org/health/live` -> 200 `{"status":"ok","service":"a23font"}`

## Manual fallback (only if Cloudflare API rejects config PUT)

If the tunnel is remotely managed and the API refuses configuration changes,
add the ingress entry manually in the Cloudflare Zero Trust dashboard:

1. Zero Trust -> Networks -> Tunnels -> select the tunnel -> Public Hostname.
2. Add hostname `font.esma.eu.org`, service `http://localhost:<port>`.
3. Ensure DNS CNAME `font -> <tunnel-id>.cfargotunnel.com` (proxied) exists.