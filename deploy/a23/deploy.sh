#!/bin/sh
# A23Font deploy runbook. Executed ON the A23 phone as root (Termux sshd),
# outside the Debian chroot. Docker lives inside the chroot.
#
# Usage: deploy.sh {up|down|status|build}
set -eu

A23FONT_DIR="${A23FONT_DIR:-/data/local/chroot/debian/opt/a23font}"
CHROOT="chroot /data/local/chroot/debian"
PROJECT="a23font"
HOST_PORT="${A23FONT_HOST_PORT:-8090}"

have_compose() {
  $CHROOT sh -lc 'docker compose version' >/dev/null 2>&1
}

build() {
  if have_compose; then
    $CHROOT sh -lc "cd /opt/a23font && docker compose --project-name ${PROJECT} build"
  else
    $CHROOT sh -lc "docker build -t a23font:v1 /opt/a23font"
  fi
}

run_container() {
  # $1 = container name; remaining args = docker run tail
  _name="$1"
  shift
  _state=$($CHROOT sh -lc "docker inspect -f '{{.State.Running}}' ${_name} 2>/dev/null" || echo missing)
  case "${_state}" in
    missing)
      $CHROOT sh -lc "docker run -d --name ${_name} --restart unless-stopped $*"
      ;;
    false)
      $CHROOT sh -lc "docker start ${_name}"
      ;;
    true)
      echo "${_name} already running"
      ;;
  esac
}

up() {
  if have_compose; then
    $CHROOT sh -lc "cd /opt/a23font && docker compose --project-name ${PROJECT} up -d"
    return
  fi
  $CHROOT sh -lc 'docker volume create a23font-data'
  run_container a23font-web \
    --env-file /opt/a23font/.env \
    -p "127.0.0.1:${HOST_PORT}:8090" \
    -v a23font-data:/data \
    a23font:v1
  run_container a23font-worker \
    --env-file /opt/a23font/.env \
    -v a23font-data:/data \
    a23font:v1 python -m worker.main
}

down() {
  # Stop and remove both containers. Never delete the a23font-data volume.
  if have_compose; then
    $CHROOT sh -lc "cd /opt/a23font && docker compose --project-name ${PROJECT} down" || true
  fi
  for _name in a23font-web a23font-worker; do
    $CHROOT sh -lc "docker stop ${_name} >/dev/null 2>&1 || true"
    $CHROOT sh -lc "docker rm ${_name} >/dev/null 2>&1 || true"
  done
}

status() {
  $CHROOT sh -lc "docker ps -a --filter name=a23font"
}

case "${1:-status}" in
  up) up ;;
  down) down ;;
  status) status ;;
  build) build ;;
  *) echo "usage: $0 {up|down|status|build}" >&2; exit 2 ;;
esac