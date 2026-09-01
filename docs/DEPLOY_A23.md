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


## Live pipeline run (T-007, M5) - 2026-09-01 UTC

Live end-to-end reconstruction of real MyFonts styles on the real A23 via the
deployed worker: image `a23font:v2`, `A23FONT_PIPELINE_LIVE=true`, code at
`7766f94`. One job submitted through the production API exactly as a user
would; evidence captured for M5.A1 (real style TTF+OTF, heavy validation),
M5.A2 (SQLite/cache persistence across worker restart), M5.A3 (resource
observations in the job report).

| Item | Value |
| --- | --- |
| Run window | 2026-09-01 00:57:51Z - 01:18:31Z (job wall time 1237 s), T-007 r1 d1 |
| Image | a23font:v2 sha256:670817787994ff484e5fee7fde060a53686b7aefbc8c2654b487e72d148870f3 |
| Rollback | redeploy tag a23font:v1 (08f82423b961, kept) and/or set A23FONT_PIPELINE_LIVE=false |
| Source URL | https://www.myfonts.com/collections/postamp-grotesk-font-fontfabric (resolves 11 styles, run sequentially; first two completed) |
| Job | J-tebpLchS_zMm9IHy, POST /jobs 303 -> /jobs/J-tebpLchS_zMm9IHy, worker w-c1acb37e39 |
| Outcome | 2 styles DONE / 0 failed; operator-cancelled at 01:18:29Z to bound the run (job terminal CANCELLED with report_json; 9 styles never started) |
| Raster traffic | sig.monotype.com: 1832 fetches, all HTTP 200 (zero non-2xx); gmap discovery 4 pages/style |

### M5.A1 evidence - real styles reconstructed + heavily validated

Style 1 "Postamp Grotesk Variable Regular" (md5 3452d65020a1476ec13080d7216186db):
- discovery 390 glyphs; frozen 296 (75.9%), failed 94 (fast-lane confidence; refinement
  ladder phases are unsupported by the direct raster endpoint, skipped honestly)
- duration 504.2 s; heavy validation passed: fonttools TTF + OTF ok, harfbuzz
  "all 28 cmap-present corpus codepoints shape to real glyphs", freetype
  "32/32 ink chars rasterized non-empty at 32px"
- artifacts in volume a23font-data under /data/cache/pipeline/b3e92902f108d20f-65b8dfe9daa02444/:
  final.ttf 53272 B (sha256 4b140b78074dfaf3...), final.otf 36992 B (sha256 a9790437cc50c25a...),
  fontmodel.json 326698 B

Style 2 "Postamp Grotesk Thin" (md5 213c3759d29b3c451838f2973edd76ff):
- discovery 390 glyphs; frozen 296; duration 515.4 s; same validation pass profile
- final.ttf 53200 B (sha256 ba8bdc247f439cb9...), final.otf 36568 B (sha256 2e7320a19ccd82aa...)

Style 3 "Thin Mix" checkpointed at 126 frozen glyphs when the cancel arrived
(graceful cancel path persists the checkpoint). Exact raster cache write-through:
1857 npz observations under /data/cache/observations.

### M5.A2 evidence - persistence across worker restart

`docker restart a23font-worker` after the run: graceful `worker_stopped`,
successor worker w-e9deea5a2a starts with `startup_stale_requeue=0` (terminal
job untouched), `/jobs/J-tebpLchS_zMm9IHy/status` still returns CANCELLED with
the 2 DONE style rows, job page HTTP 200, and final.ttf sha256 byte-identical
before/after (4b140b78074dfaf3...). Restart-resume of frozen glyphs was proven
offline in T-006 (tests/test_worker_resume.py); this run proves the live
persistence half.

### M5.A3 evidence - resources in the job report

report_json (CANCELLED terminal write): duration_s 1237.21, max_rss_kb 287548
(worker peak RSS via getrusage), mem_available_kb 2135524, platform Linux.
/proc/meminfo: MemAvailable 2146264 kB before submit -> 2125356 kB after
cancel (delta ~ -20 MB; MemTotal 3676288 kB). `docker stats` reports 0B on
this device (Android cgroup limitation), so the in-process getrusage value is
the authoritative RSS. dmesg tail: no OOM kills; load average ~0.8-1.0
throughout; existing services (tunnel, ddns, monitor) healthy.

### Recorded deviation: hollow-font incident (found live, fixed in this task)

- Original behaviour: worker passed `identity.stable_id` (the "md5:"-prefixed
  stable id) as the raster identity; the sig.monotype.com endpoint requires the
  bare 32-hex md5, so every raster GET 404'd, zero glyphs froze, and heavy
  validation trivially passed on a .notdef+space-only font.
- Measured problem: first live attempt (job J-B_1fCrsaAYxb79YT, 00:38-00:49Z)
  marked 2 styles DONE with `glyphs_frozen: 0`, 1308-byte TTFs; worker log
  shows `HTTP/1.1 404 Not Found` for every `/render/105/font/md5:<hex>` fetch.
- Replacement (commit 7766f94): worker passes the raw md5; orchestrator fails a
  style with NO_GLYPHS_FROZEN when nothing froze and invalidates hollow
  binary/fontmodel cache hits instead of serving them; DISCOVERY_EMPTY guard
  fails styles whose gmap discovery produced zero glyphs.
- Evidence: rebuilt v2 rerun -> 1832/1832 raster fetches HTTP 200, 296/390
  glyphs frozen per style, real binaries (sizes/shas above); offline regression
  tests `tests/test_orchestrator.py::test_zero_usable_observations_fails_honest_no_glyphs_frozen`
  and `::test_hollow_binary_cache_entry_is_invalidated_not_served` (suite 157 passed).
  The two hollow cache entries from the failed attempt were deleted from the
  volume before the rerun.

### Notes / TODO

- Glyph metrics are from the heuristic estimator; the browser measureText
  producer is a later milestone. Vietnamese extension pending
  (VIETNAMESE_PENDING); kerning/features intentionally skipped (typography
  inference milestone).
- Styles 4-11 of the collection were not run (bounded operator cancel); the
  operator-cancel evidence path wrote report_json honestly. M6 covers
  multi-style DONE_WITH_ERRORS + ZIP packaging; M7 covers kill/restart resume
  mid-job and cache reuse on repeat runs.
- A23FONT_EXTRA_SOURCE_HOSTS left empty: no raster CDN redirects observed from
  the phone (all fetches answered directly by sig.monotype.com).
