#!/usr/bin/env bash
# Remove Zorcam from the Pi: services, program files (/opt/zorcam-nvr), go2rtc and firewall rules.
# Recordings, settings (passwords, users) and the repository clone are removed only after a separate question each;
# the service user zorcam-nvr goes with the last of /var/lib/zorcam-nvr;
# the GitHub SSH key (~/.ssh/zorcam_deploy) is kept, so the repository can be cloned again.
# Run as the user that installed it (with sudo):  cd ~ && ~/zorcam-nvr/app/uninstall.sh
# (from the home folder: a script cannot move the terminal out of ~/zorcam-nvr once it is deleted)
#   --yes   answer "yes" to every question (removes everything)
set -euo pipefail

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_off=$'\e[0m'
ok()  { echo "  ${c_ok}OK${c_off}  $*"; }
warn() { echo "  ${c_warn}!!${c_off}  $*"; }
YES=0
[ "${1:-}" = "--yes" ] && YES=1
ask() {
    [ "$YES" = 1 ] && return 0
    local a
    read -r -p "  $1 [y/N] " a
    [[ "$a" =~ ^[Yy]$ ]]
}

[ "$(id -u)" != 0 ] || { echo "run as a regular user with sudo (e.g. admin), not as root"; exit 1; }
[ -n "$HOME" ] && [ -d "$HOME" ] || { echo "HOME is not set"; exit 1; }

SERVICES=(zorcam-nvr-capture zorcam-nvr-gallery zorcam-nvr-go2rtc zorcam-nvr-cloud zorcam-nvr-go2rtc-config.path zorcam-nvr-go2rtc-config.service zorcam-nvr-debug-monitor zorcam-nvr-storage.path zorcam-nvr-storage.timer zorcam-nvr-storage.service)
SVC_USER=zorcam-nvr
STATE=/var/lib/zorcam-nvr
PROGRAM=(/opt/zorcam-nvr)
DATA=("$STATE/recordings")
CONFIG="$STATE/config"
# the clone this script is in (~/zorcam-nvr)
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[ -d "$REPO/.git" ] || REPO="$HOME/zorcam-nvr"
START_DIR="$PWD"
cd "$HOME"  # nothing below depends on the current folder, which may be one of those removed

echo "==> Found"
units=()
for s in "${SERVICES[@]}"; do
    unit="$s"; [[ "$unit" == *.* ]] || unit="$unit.service"
    if [ -f "/etc/systemd/system/$unit" ]; then units+=("$unit"); echo "  service $unit"; fi
done
for d in "${PROGRAM[@]}" "${DATA[@]}" "$CONFIG" "$REPO"; do
    sudo test -d "$d" && echo "  folder $d ($(sudo du -sh "$d" 2>/dev/null | cut -f1))"
done
[ -x /usr/local/bin/go2rtc ] && echo "  /usr/local/bin/go2rtc"
[ -x /usr/local/bin/frpc ] && echo "  /usr/local/bin/frpc"
echo

if ask "Remove Zorcam services, program files, go2rtc and firewall rules?"; then
    for unit in "${units[@]}"; do
        sudo systemctl disable --now "$unit" 2>/dev/null || true
        sudo rm -f "/etc/systemd/system/$unit"
    done
    sudo systemctl daemon-reload
    sudo systemctl reset-failed 2>/dev/null || true
    ok "services removed"
    # the recordings disk (USB): unmounted, what is recorded on it stays there
    if mountpoint -q "$STATE/recordings"; then sudo umount "$STATE/recordings" || true; fi
    if mountpoint -q /mnt/zorcam-nvr-disk; then sudo umount /mnt/zorcam-nvr-disk && ok "recordings disk unmounted"; fi
    for d in "${PROGRAM[@]}"; do
        if [ -d "$d" ]; then sudo rm -rf "$d"; ok "removed $d"; fi
    done
    sudo rm -f /usr/local/bin/go2rtc /usr/local/bin/frpc
    sudo rm -rf /dev/shm/zorcam_*
    ok "go2rtc and temporary files removed"
    # what app/debug.sh on added: service drop-ins, the monitor, the journal on disk, kernel settings
    if [ -f /etc/systemd/journald.conf.d/zorcam-nvr-debug.conf ] || [ -f /etc/sysctl.d/90-zorcam-nvr-debug.conf ]; then
        sudo rm -rf /etc/systemd/system/zorcam-*.service.d /usr/local/bin/zorcam-nvr-debug-monitor \
            /etc/systemd/journald.conf.d/zorcam-nvr-debug.conf /etc/sysctl.d/90-zorcam-nvr-debug.conf /var/log/journal
        sudo systemctl daemon-reload
        sudo systemctl restart systemd-journald
        ok "debugging settings removed"
    fi
    if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "8555"; then
        sudo ufw delete allow 8555/tcp >/dev/null 2>&1 || true
        sudo ufw delete allow 8555/udp >/dev/null 2>&1 || true
        ok "firewall rules for port 8555 removed"
    fi
else
    echo "  services and program files kept"
fi

# the detector keeps writing frames while it runs: stop it for the deletion, start it again if the services stay
capture_was_active=0
for d in "${DATA[@]}"; do
    sudo test -d "$d" || continue
    if ask "Delete the recordings in $d? They cannot be restored"; then
        if systemctl is-active -q zorcam-nvr-capture 2>/dev/null; then
            capture_was_active=1
            sudo systemctl stop zorcam-nvr-capture
        fi
        sudo rm -rf "$d" 2>/dev/null || { sleep 1; sudo rm -rf "$d" 2>/dev/null; } || true
        if sudo test -d "$d"; then warn "could not remove everything in $d"; else ok "removed $d"; fi
    fi
done
if [ "$capture_was_active" = 1 ]; then
    sudo systemctl start zorcam-nvr-capture
    ok "zorcam-nvr-capture started again"
fi

if sudo test -d "$CONFIG" && ask "Delete the settings in $CONFIG (zorcam.conf, cameras and their passwords, users, push keys)?"; then
    sudo rm -rf "$CONFIG" /var/lib/zorcam-nvr-storage; ok "removed $CONFIG"
fi
# nothing of Zorcam left in /var/lib/zorcam-nvr: its user goes too
if ! sudo test -d "$CONFIG" && ! sudo test -d "${DATA[0]}" && [ ! -d /opt/zorcam-nvr ]; then
    sudo rm -rf "$STATE"
    if id "$SVC_USER" >/dev/null 2>&1; then sudo userdel "$SVC_USER" 2>/dev/null || true; ok "removed the user $SVC_USER"; fi
fi

# the repository clone goes last: this script is part of it
if [ -d "$REPO" ] && ask "Delete the repository clone $REPO?"; then
    rm -rf "$REPO"; ok "removed $REPO"
fi

echo
echo "${c_ok}Done.${c_off} Packages installed by install.sh (ffmpeg, python3-pil, python3-cryptography, curl) are kept;"
echo "remove them with apt if nothing else needs them. On the iPhone, delete the Zorcam icon from the Home Screen."
if [ "${#units[@]}" -eq 0 ] && [ "$YES" = 0 ]; then echo "  ${c_warn}(no Zorcam services were installed)${c_off}"; fi
if [ ! -d "$START_DIR" ]; then
    echo
    warn "this terminal is still in the deleted folder $START_DIR: run  cd ~"
fi
