#!/usr/bin/env bash
# One-time (re-runnable) kiosk install on Debian/Ubuntu. Settings: kiosk/kiosk.conf (see kiosk.conf.example).
#   sudo bash kiosk/setup.sh
# Installs Docker and the collectors, the display, a kiosk user that autologs into tty1 and runs
# cage (Wayland kiosk) -> foot (fullscreen terminal) -> wxterm, plus screenshot/GIF tools. With KIOSK_MODE=yes it
# switches the machine to boot into the display; `sudo kiosk/mode.sh desktop` brings the normal desktop back.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
. "$SRC/kiosk/lib.sh"
DEPLOY_USER="${DEPLOY_USER:-${SUDO_USER:-root}}"

echo "== packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  cage foot fonts-dejavu-core grim wf-recorder ffmpeg gifsicle python3-venv rsync curl ca-certificates \
  docker.io docker-compose

echo "== code -> $WXTERM_DIR (owner $DEPLOY_USER)"
mkdir -p "$WXTERM_DIR"
if [[ "$SRC" != "$WXTERM_DIR" ]]; then
  rsync -a --exclude .git --exclude .venv --exclude __pycache__ --exclude '*.egg-info' "$SRC/" "$WXTERM_DIR/"
fi
cd "$WXTERM_DIR"

echo "== locations + collectors"
ls locations/*.env >/dev/null 2>&1 || { echo "no locations; add one with scripts/new-location.py"; exit 1; }
[[ -f locations/home.env ]] || echo "   note: no locations/home.env yet. Make yours with: scripts/new-location.py home LAT LON"
ua="wxterm/1.1 (+https://wxterm.app${NWS_CONTACT:+; $NWS_CONTACT})"
id "$KIOSK_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$KIOSK_USER"
install -d -m 2775 -o "$KIOSK_USER" -g "$KIOSK_USER" "$STATE_DIR" "$STATE_DIR/requests"
token="$MCP_TOKEN"
if [[ "$MCP" == yes && -z "$token" ]]; then   # keep the token from an earlier run so clients stay connected
  token=$(sed -n 's/^MCP_TOKEN=//p' .env 2>/dev/null | head -1)
  [[ -n "$token" ]] || token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
fi
umask 077
{
  echo "# Written by kiosk/setup.sh from kiosk/kiosk.conf; read by docker compose. Holds the MCP token: keep private."
  printf 'WX_UA=%s\n' "$ua"
  echo "STATE_DIR=$STATE_DIR"
  echo "KIOSK_GID=$(getent group "$KIOSK_USER" | cut -d: -f3)"
  echo "WXTERM_ONLY=$LOCATIONS"
  if [[ "$MCP" == yes ]]; then
    echo "COMPOSE_PROFILES=mcp"
    echo "MCP_TOKEN=$token"
    echo "MCP_PORT=$MCP_PORT"
    echo "MCP_BIND=$MCP_BIND"
  fi
} > .env
umask 022
python3 scripts/gen-compose.py
systemctl enable --now docker
docker compose up -d --build --remove-orphans

echo "== display venv"
[[ -x display/.venv/bin/python ]] || python3 -m venv display/.venv
display/.venv/bin/pip -q install -e display

echo "== kiosk user $KIOSK_USER + $STATE_DIR"
for g in video input render; do   # render doesn't exist on every distro
  getent group "$g" >/dev/null && usermod -aG "$g" "$KIOSK_USER"
done
install -d -m 2775 -o "$KIOSK_USER" -g "$KIOSK_USER" "$STATE_DIR/gifs" "$STATE_DIR/captures"
for u in $ADMIN_USERS $DEPLOY_USER; do
  id "$u" >/dev/null 2>&1 || continue
  [[ "$u" == root ]] && continue
  usermod -aG docker,"$KIOSK_USER" "$u"
done
for u in $ADMIN_USERS; do   # a ~/gifs shortcut to the GIFs saved with the g key
  h=$(getent passwd "$u" | cut -d: -f6) || continue
  if [[ -n "$h" && ! -e "$h/gifs" ]]; then ln -s "$STATE_DIR/gifs" "$h/gifs"; chown -h "$u:$u" "$h/gifs"; fi
done
cat > "$STATE_DIR/display.env" <<EOF
# Written by kiosk/setup.sh from kiosk/kiosk.conf; read by the kiosk user's login shell
WXTERM_STATE=$STATE_DIR/state.json
WXTERM_GIF_DIR=$STATE_DIR/gifs
WXTERM_START=$START_LOCATION
WXTERM_THEME=$THEME
WXTERM_BIGFONT=$BIGFONT
WXTERM_SHUFFLE_MIN=$SHUFFLE_MIN
WXTERM_GIF_FPS=$GIF_FPS
WXTERM_ONLY=$LOCATIONS
WXTERM_RADAR_STEP=$RADAR_STEP
WXTERM_TYPE_STEP=$TYPE_STEP
EOF
chown "$KIOSK_USER:$KIOSK_USER" "$STATE_DIR/display.env"
H=$(getent passwd "$KIOSK_USER" | cut -d: -f6)
install -d -o "$KIOSK_USER" -g "$KIOSK_USER" "$H/.config/foot"
sed -e "s|@FONT@|$FONT|" -e "s|@FONT_SIZE@|$FONT_SIZE|" kiosk/foot.ini > "$H/.config/foot/foot.ini"
sed -e "s|@STATE_DIR@|$STATE_DIR|g" -e "s|@WXTERM_DIR@|$WXTERM_DIR|g" kiosk/bash_profile > "$H/.bash_profile"
chown "$KIOSK_USER:$KIOSK_USER" "$H/.config/foot/foot.ini" "$H/.bash_profile"

echo "== ownership"
chown -R "$DEPLOY_USER": "$WXTERM_DIR"
chmod 600 .env
[[ "$MCP" == yes ]] && echo "   MCP server on ${MCP_BIND}:${MCP_PORT}/mcp; connection details: $WXTERM_DIR/scripts/wxctl.sh mcp"

if [[ "$KIOSK_MODE" == yes ]]; then
  echo "== kiosk mode"
  bash kiosk/mode.sh kiosk
  echo "done. Reboot (or: sudo systemctl restart getty@tty1) to start the display."
else
  echo "done. Boot mode unchanged; switch with: sudo $WXTERM_DIR/kiosk/mode.sh kiosk"
fi
