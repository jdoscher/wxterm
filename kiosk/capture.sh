#!/usr/bin/env bash
# Screenshot or record the live kiosk screen. Needs sudo (the capture runs as the kiosk user inside its Wayland session).
#   kiosk/capture.sh shot [file.png]           full-screen PNG
#   kiosk/capture.sh video 30 [file.mp4]       record 30 seconds
# Files land in $STATE_DIR/captures (readable by the kiosk group) unless a path is given.
# For an animated GIF of one radar loop, press g on the display (or scripts/wxctl.sh gif).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
what="${1:-shot}"
uid=$(id -u "$KIOSK_USER")
out_dir="$STATE_DIR/captures"
[[ -d $out_dir ]] || sudo install -d -m 2775 -o "$KIOSK_USER" -g "$KIOSK_USER" "$out_dir"
run() {  # cage's Wayland socket is only visible to the kiosk user, so look it up as that user
  sudo -u "$KIOSK_USER" env XDG_RUNTIME_DIR="/run/user/$uid" sh -c 'for s in "$XDG_RUNTIME_DIR"/wayland-*; do case $s in *.lock) ;; *) WAYLAND_DISPLAY=${s##*/}; export WAYLAND_DISPLAY; break ;; esac; done; exec "$@"' sh "$@"
}
case "$what" in
  shot)  f="${2:-$out_dir/wxterm-$(date +%Y%m%d-%H%M%S).png}"; run grim "$f" ;;
  video) secs="${2:-30}"; f="${3:-$out_dir/wxterm-$(date +%Y%m%d-%H%M%S).mp4}"
         run timeout --signal=INT "$secs" wf-recorder -f "$f" >/dev/null 2>&1 || true ;;
  *) echo "usage: capture.sh shot [file] | video SECONDS [file]"; exit 1 ;;
esac
sudo chmod 664 "$f" 2>/dev/null || true
echo "$f"
