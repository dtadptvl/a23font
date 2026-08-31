# A23 deploy runbook

Target: Android phone "A23" running Termux sshd (port 8022) with a Debian
chroot at `/data/local/chroot/debian`. Docker 20.10 runs INSIDE the chroot
(storage driver `vfs` - builds are slow; that is expected).

## Where things live

| Item | Path |
| --- | --- |
| This script set | repo `deploy/a23/` (on the phone: `/data/local/chroot/debian/opt/a23font/deploy/a23/`) |
| Application code (chroot view) | `/opt/a23font` |
| Application code (Android view) | `/data/local/chroot/debian/opt/a23font` |
| Runtime env file | `/opt/a23font/.env` (never commit; no secrets live here) |
| Data volume | docker volume `a23font-data` mounted at `/data` in the containers |
| Host port | `127.0.0.1:8090` (web container), tunneled via cloudflared (host-side) |

## Getting code to the phone

Option A (preferred, inside the chroot as root):

```sh
chroot /data/local/chroot/debian sh -lc \
  'mkdir -p /opt && (git clone https://github.com/dtadptvl/a23font /opt/a23font || (cd /opt/a23font && git pull))'
```

Option B (from the dev host, when git/network is unavailable in the chroot):

```pwsh
scp -i C:\Users\PC\.ssh\id_ed25519_a23 -P 8022 -r `
  app pipeline worker templates static requirements.txt Dockerfile compose.yml deploy .env.example `
  root@<tailscale-ip>:/data/local/chroot/debian/opt/a23font/
```

Copy only the listed paths - never `.venv`, `.git`, `data/`, env/secret files.

## Deploy

All commands run as root on the phone (outside the chroot):

```sh
sh /data/local/chroot/debian/opt/a23font/deploy/a23/deploy.sh build   # image build (slow on vfs)
sh /data/local/chroot/debian/opt/a23font/deploy/a23/deploy.sh up      # start web + worker
sh /data/local/chroot/debian/opt/a23font/deploy/a23/deploy.sh status  # docker ps filter
sh /data/local/chroot/debian/opt/a23font/deploy/a23/deploy.sh down    # stop + remove containers
```

`deploy.sh` auto-detects `docker compose` inside the chroot and uses it
(project name `a23font`); otherwise it falls back to plain `docker run`
with containers named `a23font-web` and `a23font-worker`.

The web port is bound to `127.0.0.1` only. Public reachability comes from
the host-side cloudflared tunnel (Zero Trust ingress -> http://localhost:8090).

## Rollback

```sh
sh /data/local/chroot/debian/opt/a23font/deploy/a23/deploy.sh down
```

`down` stops and removes both containers but never deletes the
`a23font-data` volume, so data survives rollback. Bring the previous image
back with `build` + `up` when ready.

## Do not touch

- Container `a23-cloudflare-ddns` (production DDNS updater).
- Host-side `cloudflared` process and its remotely-managed token.
- Ports 8022 (ssh), 2080 (server-monitor), 2000 (cloudflared metrics).