#!/bin/sh
# A23 offline-leaning image build.
#
# Why: on this phone the mobile network DNS flips between phases (A records
# fail while AAAA resolves, IPv6 egress is blackholed), and the docker bridge
# netns has no egress at all. Long-running `docker build` RUN steps therefore
# cannot complete reliably. This script splits the build into:
#   stage 1 (download): retryable host-net container fetches .debs + wheels
#   stage 2 (offline):  dpkg -i / pip --no-index / COPY equivalent, then
#                       docker commit -> a23font:v1
# The resulting image is functionally identical to the canonical /Dockerfile
# (same base, packages, files, metadata); deploy/a23/Dockerfile.a23 remains
# the BuildKit path for phases where DNS behaves.
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
apt-get update
apt-get install -y --no-install-recommends --download-only -o Dir::Cache::archives=/cache/debs curl tini
ls /cache/debs/*.deb
pip download -d /cache/wheels -r /src/requirements.txt
echo DOWNLOAD_STAGE_OK
DL
  cat > "$CHROOT_DIR/tmp/a23mk.sh" <<'MK'
set -e
printf 'precedence ::ffff:0:0/96  100\n' >> /etc/gai.conf
dpkg -i /cache/debs/*.deb
pip install --no-index --find-links=/cache/wheels -r /src/requirements.txt
mkdir -p /app /data
cp -a /src/app /app/app
cp -a /src/pipeline /app/pipeline
cp -a /src/worker /app/worker
cp -a /src/templates /app/templates
cp -a /src/static /app/static
echo BUILD_STAGE_OK
MK
}

download_stage() {
  # Retryable: only this stage needs working DNS (host netns).
  chroot_exec "mkdir -p $CACHE/debs $CACHE/wheels && docker run --rm --network host \
    -v $CACHE:/cache -v $SRC:/src:ro -v /tmp/a23dl.sh:/dl.sh:ro \
    $BASE sh /dl.sh"
}

build_stage() {
  chroot_exec "docker rm -f a23font-build >/dev/null 2>&1 || true"
  chroot_exec "docker run -d --name a23font-build --network host \
    -v $CACHE:/cache -v $SRC:/src:ro -v /tmp/a23mk.sh:/mk.sh:ro \
    $BASE sleep 7200 >/dev/null"
  chroot_exec "docker exec a23font-build sh /mk.sh"
  chroot_exec "docker commit \
    --change 'ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 A23FONT_DATA_ROOT=/data A23FONT_HTTP_HOST=0.0.0.0 A23FONT_HTTP_PORT=8090' \
    --change 'WORKDIR /app' \
    --change 'VOLUME /data' \
    --change 'EXPOSE 8090' \
    --change 'HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD curl -fsS http://127.0.0.1:8090/health/live || exit 1' \
    --change 'ENTRYPOINT [\"/usr/bin/tini\",\"--\"]' \
    --change 'CMD [\"python\",\"-m\",\"app.web.run\"]' \
    a23font-build $IMG"
  chroot_exec "docker rm -f a23font-build >/dev/null"
}

write_inner_scripts

attempts="${A23FONT_BUILD_ATTEMPTS:-8}"
i=0
until [ "$i" -ge "$attempts" ]; do
  i=$((i + 1))
  echo "=== download stage attempt $i/$attempts ==="
  if download_stage; then
    break
  fi
  if [ "$i" -ge "$attempts" ]; then
    echo "download stage failed after $attempts attempts (DNS/network phases)" >&2
    exit 1
  fi
  sleep 15
done

build_stage
echo "image built: $IMG"