#!/bin/sh
# A23 image build with deterministic offline assembly.
#
# Why: this phone's mobile-network DNS oscillates between bad phases and the
# docker bridge netns has no egress at all, so network builds on-device are
# unreliable. Artifacts are provisioned into $CACHE by EITHER path:
#   1. scp from the dev host over the Tailscale link (no phone DNS needed):
#        .buildcache/wheels/*.whl  cp312/aarch64 wheels for requirements.txt
#        .buildcache/tini-arm64    tini init binary (krallin/tini v0.19.0)
#   2. this script's retryable on-device download stage (needs a good DNS
#      window; installs curl/tini via apt instead of the tini binary).
# The assembly stage is fully offline and docker-commits a23font:v1,
# functionally identical to the canonical /Dockerfile (same base, deps,
# files, metadata; the healthcheck uses python urllib instead of curl in the
# no-apt path). deploy.sh adds the runtime healthcheck flags.
set -eu

CHROOT_DIR="/data/local/chroot/debian"
SRC="/opt/a23font"
CACHE="$SRC/.buildcache"
BASE="python:3.12-slim-bookworm"
IMG="a23font:v1"

chroot_exec() { chroot "$CHROOT_DIR" /bin/sh -lc "$1"; }

write_inner_scripts() {
  cat > "$CHROOT_DIR/tmp/a23dl.sh" <<'DL'
set -e
printf 'precedence ::ffff:0:0/96  100\n' >> /etc/gai.conf
export DEBIAN_FRONTEND=noninteractive
mkdir -p /var/lib/apt/lists/partial
if [ ! -f /cache/lists.done ]; then
  apt-get update
  ls /var/lib/apt/lists/*InRelease >/dev/null 2>&1 || { echo NO_LISTS_FETCHED; exit 1; }
  touch /cache/lists.done
fi
if [ ! -f /cache/debs.done ]; then
  ls /var/lib/apt/lists/*InRelease >/dev/null 2>&1 || { echo NO_LISTS_MOUNTED; exit 1; }
  apt-get install -y --no-install-recommends --download-only -o Dir::Cache::archives=/cache/debs curl tini
  ls /cache/debs/*.deb >/dev/null
  touch /cache/debs.done
fi
if [ ! -f /cache/wheels.done ]; then
  pip download -d /cache/wheels -r /src/requirements.txt
  touch /cache/wheels.done
fi
echo DOWNLOAD_STAGE_OK
DL
  cat > "$CHROOT_DIR/tmp/a23mk.sh" <<'MK'
set -e
if [ -f /cache/tini-arm64 ]; then
  cp /cache/tini-arm64 /usr/bin/tini
  chmod 755 /usr/bin/tini
elif ls /cache/debs/*.deb >/dev/null 2>&1; then
  dpkg -i /cache/debs/*.deb
else
  echo "no tini source in cache" >&2
  exit 1
fi
pip install --no-index --find-links=/cache/wheels -r /src/requirements.txt
mkdir -p /app /data
cp -a /src/app /app/app
cp -a /src/pipeline /app/pipeline
cp -a /src/worker /app/worker
cp -a /src/templates /app/templates
cp -a /src/static /app/static
cat > /app/healthcheck.py <<'HC'
#!/usr/bin/env python3
import sys, urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:8090/health/live", timeout=4)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
HC
chmod 755 /app/healthcheck.py
echo BUILD_STAGE_OK
MK
}

cache_ready() {
  [ -f "$CHROOT_DIR$CACHE/tini-arm64" ] || ls "$CHROOT_DIR$CACHE"/debs/*.deb >/dev/null 2>&1 || return 1
  chroot_exec "docker run --rm -v $CACHE:/cache -v $SRC:/src:ro $BASE \
    pip install --no-index --find-links=/cache/wheels --dry-run --quiet -r /src/requirements.txt"
}

download_stage() {
  chroot_exec "mkdir -p $CACHE/debs $CACHE/wheels $CACHE/aptlists/partial && docker run --rm --network host \
    -v $CACHE:/cache -v $CACHE/aptlists:/var/lib/apt/lists -v $SRC:/src:ro -v /tmp/a23dl.sh:/dl.sh:ro \
    $BASE sh /dl.sh"
}

download_loop() {
  attempts="${A23FONT_BUILD_ATTEMPTS:-25}"
  i=0
  until [ "$i" -ge "$attempts" ]; do
    i=$((i + 1))
    echo "=== download stage attempt $i/$attempts ==="
    if download_stage; then
      return 0
    fi
    if [ "$i" -ge "$attempts" ]; then
      echo "download stage failed after $attempts attempts (DNS/network phases)." >&2
      echo "Provision $CACHE/wheels and $CACHE/tini-arm64 from the dev host via scp instead." >&2
      return 1
    fi
    sleep 12
  done
}

build_stage() {
  chroot_exec "docker rm -f a23font-build >/dev/null 2>&1 || true"
  # No docker exec here: on this device exec does not join the container
  # mount namespace. Run the assembly script as the container command.
  chroot_exec "docker run --name a23font-build --network host \
    -v $CACHE:/cache -v $SRC:/src:ro -v /tmp/a23mk.sh:/mk.sh:ro \
    $BASE sh /mk.sh"
  # Runtime healthcheck is applied by deploy.sh (docker run --health-*);
  # the worker container disables it (--no-healthcheck).
  chroot_exec "docker commit \
    --change 'ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 A23FONT_DATA_ROOT=/data A23FONT_HTTP_HOST=0.0.0.0 A23FONT_HTTP_PORT=8090' \
    --change 'WORKDIR /app' \
    --change 'VOLUME /data' \
    --change 'EXPOSE 8090' \
    --change 'ENTRYPOINT [\"/usr/bin/tini\",\"--\"]' \
    --change 'CMD [\"python\",\"-m\",\"app.web.run\"]' \
    a23font-build $IMG"
  chroot_exec "docker rm -f a23font-build >/dev/null"
}

write_inner_scripts

if cache_ready; then
  echo "cache complete: offline assembly only"
else
  echo "cache incomplete: trying on-device download stage"
  download_loop
fi

build_stage
echo "image built: $IMG"