#!/usr/bin/env bash
# Switch the machine between the weather kiosk and the normal desktop. Takes effect on reboot (or pass --now).
#   sudo kiosk/mode.sh kiosk     boot to tty1 -> cage -> foot -> wxterm; no login screen; the lid is ignored, or
#                                suspends the machine with LID=suspend in kiosk.conf
#   sudo kiosk/mode.sh desktop   boot to the normal graphical login; sleep and lid back to defaults
#   kiosk/mode.sh status
# The collectors (Docker) keep running in both modes. In kiosk mode Ctrl+Alt+F2 gives a text console.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
mode="${1:-status}"
now="${2:-}"
DROPIN=/etc/systemd/system/getty@tty1.service.d/wxterm-autologin.conf
LOGIND=/etc/systemd/logind.conf.d/wxterm.conf
SLEEP="sleep.target suspend.target hibernate.target hybrid-sleep.target"

status() {
  echo "default target: $(systemctl get-default)"
  if [[ -f $DROPIN ]]; then echo "tty1 autologin: $(grep -o -- '--autologin [^ ]*' $DROPIN | cut -d' ' -f2)"
  else echo "tty1 autologin: off"; fi
  echo "sleep: $(systemctl is-enabled suspend.target 2>/dev/null || true)"
  if [[ -f $LOGIND ]]; then echo "lid: $(sed -n 's/^HandleLidSwitch=//p' $LOGIND)"; fi
  if [[ -r /sys/power/mem_sleep ]]; then echo "suspend mode: $(cat /sys/power/mem_sleep)"; fi
}

case "$mode" in
  kiosk)
    [[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
    mkdir -p "$(dirname $DROPIN)" "$(dirname $LOGIND)"
    printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin %s --noclear %%I $TERM\n' "$KIOSK_USER" > $DROPIN
    if [[ "$LID" == suspend ]]; then
      # sleep on lid close (battery or power), never when docked or on an external screen; never on idle
      printf '[Login]\nHandleLidSwitch=suspend\nHandleLidSwitchExternalPower=suspend\nHandleLidSwitchDocked=ignore\nIdleAction=ignore\n' > $LOGIND
      systemctl unmask -q sleep.target suspend.target
      systemctl mask -q hibernate.target hybrid-sleep.target
    else
      printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\nIdleAction=ignore\n' > $LOGIND
      systemctl mask -q $SLEEP
    fi
    systemctl set-default -q multi-user.target
    ;;
  desktop)
    [[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
    rm -f $DROPIN $LOGIND
    systemctl unmask -q $SLEEP
    systemctl set-default -q graphical.target
    ;;
  status) status; exit 0 ;;
  *) echo "usage: mode.sh kiosk|desktop|status [--now]"; exit 1 ;;
esac
systemctl daemon-reload
# logind reads its config only at start; restarting it is safe (sessions survive) and applies the lid setting now
systemctl try-reload-or-restart systemd-logind 2>/dev/null || true
status
if [[ "$now" == "--now" ]]; then
  systemctl reboot
else
  echo "reboot to apply (or rerun with --now)"
fi
