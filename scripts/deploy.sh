#!/usr/bin/env bash
# Push this checkout to a kiosk (or any host) over SSH and optionally rebuild and restart.
#   scripts/deploy.sh HOST [--up]
# Copies everything except git data and host-generated files, including your gitignored locations/home.* and
# kiosk/kiosk.conf. --up regenerates docker-compose.yml, rebuilds the collectors, reinstalls the display and restarts
# it (the kiosk loop brings it straight back). Target dir: WXTERM_DIR from kiosk.conf, or WXTERM_DEST.
set -euo pipefail
cd "$(dirname "$0")/.."
. kiosk/lib.sh
host="${1:?usage: deploy.sh HOST [--up]}"
dest="${WXTERM_DEST:-$WXTERM_DIR}"
rsync -a --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude '*.egg-info' \
  --exclude /.env --exclude /docker-compose.yml ./ "$host:$dest/"
if [[ "${2:-}" == "--up" ]]; then
  ssh -o BatchMode=yes "$host" "cd $dest && python3 scripts/gen-compose.py && docker compose up -d --build --remove-orphans 2>&1 | tail -3"
  ssh -o BatchMode=yes "$host" "test -x $dest/display/.venv/bin/pip && $dest/display/.venv/bin/pip -q install -e $dest/display; \
    sudo -n pkill -u $KIOSK_USER -f 'python3 $dest/display/.venv/bin/wxterm' && echo 'display restarted' || echo 'display not running'"
fi
