#!/bin/sh
# A23Font deploy runbook. Executed ON the A23 phone (Termux sshd, port 8022).
# Docker lives inside the Debian chroot at /data/local/chroot/debian.
# Re-executes itself under su when not already root.
#
# Usage: sh deploy/a23/deploy.sh {up|down|status|build}
set -eu

# Self-elevate: Termux ssh sessions land as the Termux user, not root.
if [ "$(id -u)" != "0" ]; then
  exec su -c "sh '$0' ${1:-status}"
fi

A23FONT_DIR="${A23FONT_DIR:-/data/local/chroot/debian/opt/a23font}"
CHROOT_DIR="/data/local/chroot/debian"
PROJECT="a23font"
HOST_PORT="${A23FONT_HOST_PORT:-8090}"

chroot_exec() {
  # Note: this device's chroot needs an absolute binary path (/bin/sh).
  chroot "$CHROOT_DIR" /bin/sh -lc "$1"
}

have_compose() {
  chroot_exec 'docker compose version' >/dev/null 2>&1 \
    || chroot_exec 'docker-compose version' >/dev/null 2>&1
}

build() {
  if have_compose; then
    chroot_exec "cd /opt/a23font && docker compose --project-name ${PROJECT} build"
  else
    # Device reality: the docker bridge netns has NO egress on this phone
    # (Android netd blocks it), so classic builds fail apt/pip. BuildKit is
    # built into dockerd 20.10 and --network=host runs RUN steps in the host
    # netns, where DNS/egress work. Canonical Dockerfile stays unchanged.
    chroot_exec "cd /opt/a23font && DOCKER_BUILDKIT=1 docker build --network=host -t a23font:v1 ."
  fi
}

run_container() {
  # $1 = container name; remaining args = docker run tail
  _name="$1"
  shift
  _state=$(chroot_exec "docker inspect -f '{{.State.Running}}' ${_name} 2>/dev/null" || echo missing)
  case "${_state}" in
    missing)
      chroot_exec "docker run -d --name ${_name} --restart unless-stopped $*"
      ;;
    false)
      chroot_exec "docker start ${_name}"
      ;;
    true)
      echo "${_name} already running"
      ;;
  esac
}

up() {
  if have_compose; then
    chroot_exec "cd /opt/a23font && docker compose --project-name ${PROJECT} up -d"
    return
  fi
  chroot_exec 'docker volume create a23font-data'
  # --network host: bridge netns has no egress on this device (matches the
  # existing a23-cloudflare-ddns / reunion-fund containers). The web app
  # binds A23FONT_HTTP_HOST:A23FONT_HTTP_PORT (0.0.0.0:8090) directly on the
  # phone; -p port mapping is not applicable with host networking.
  run_container a23font-web \
    --env-file /opt/a23font/.env \
    --network host \
    -v a23font-data:/data \
    a23font:v1
  run_container a23font-worker \
    --env-file /opt/a23font/.env \
    --network host \
    -v a23font-data:/data \
    a23font:v1 python -m worker.main
}

down() {
  # Stop and remove both containers. Never delete the a23font-data volume.
  if have_compose; then
    chroot_exec "cd /opt/a23font && docker compose --project-name ${PROJECT} down" || true
  fi
  for _name in a23font-web a23font-worker; do
    chroot_exec "docker stop ${_name} >/dev/null 2>&1 || true"
    chroot_exec "docker rm ${_name} >/dev/null 2>&1 || true"
  done
}

status() {
  chroot_exec 'docker ps -a --filter name=a23font'
}

case "${1:-status}" in
  up) up ;;
  down) down ;;
  status) status ;;
  build) build ;;
  *) echo "usage: $0 {up|down|status|build}" >&2; exit 2 ;;
esac