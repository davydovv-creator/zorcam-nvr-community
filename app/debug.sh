#!/usr/bin/env bash
# Extended debugging of Zorcam on the Pi, to find the cause of freezes and failures. Run as a user with sudo (e.g. admin):
#   ~/zorcam-nvr/app/debug.sh on       # turn debugging on (survives reboots)
#   ~/zorcam-nvr/app/debug.sh off      # back to production: everything "on" added is removed
#   ~/zorcam-nvr/app/debug.sh status   # what is on now
#   ~/zorcam-nvr/app/debug.sh save     # collect logs and system state into ~/zorcam-nvr-debug-*.tar.gz (no passwords)
# "on" writes more to the SD card (the journal is kept on disk, up to 200 MB): turn it off when the cause is found.
set -euo pipefail

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_off=$'\e[0m'
ok()   { echo "  ${c_ok}OK${c_off}  $*"; }
warn() { echo "  ${c_warn}!!${c_off}  $*"; }

MONITOR_BIN=/usr/local/bin/zorcam-nvr-debug-monitor
MONITOR_UNIT=/etc/systemd/system/zorcam-nvr-debug-monitor.service
JOURNALD_CONF=/etc/systemd/journald.conf.d/zorcam-nvr-debug.conf
SYSCTL_CONF=/etc/sysctl.d/90-zorcam-nvr-debug.conf
SERVICES=(zorcam-nvr-capture zorcam-nvr-gallery zorcam-nvr-go2rtc)
dropin() { echo "/etc/systemd/system/$1.service.d/zorcam-nvr-debug.conf"; }

# ---------------------------------------------------------------- monitor
# One line every 30 s to the journal (tag zorcam-nvr-debug): power, temperature, CPU clock, load, memory, disk waits,
# memory of every Zorcam service and processes stuck on the disk (state D). The last lines before a freeze
# show what ran out: power (throttled), memory or the SD card.
monitor() {
    local dstate svc_mem psi
    while true; do
        dstate="$(ps -eo stat=,pid=,comm= | awk '$1 ~ /^D/ {printf "%s(%s) ", $3, $2}')"
        svc_mem=""
        for s in "${SERVICES[@]}"; do  # the cgroup file itself: no systemctl process every 30 s for each service
            svc_mem+="$s=$(( $(cat "/sys/fs/cgroup/system.slice/$s.service/memory.current" 2>/dev/null || echo 0) / 1048576 ))M "
        done
        psi="$(awk '/^some/ {print $2}' /proc/pressure/io 2>/dev/null || echo "n/a")"
        echo "$(vcgencmd get_throttled 2>/dev/null || echo throttled=n/a)" \
             "$(vcgencmd measure_temp 2>/dev/null || echo temp=n/a)" \
             "arm=$(( $(vcgencmd measure_clock arm 2>/dev/null | cut -d= -f2 || echo 0) / 1000000 ))MHz" \
             "load=$(cut -d' ' -f1-3 /proc/loadavg | tr ' ' ,)" \
             "mem_avail=$(( $(awk '/MemAvailable/ {print $2}' /proc/meminfo) / 1024 ))M" \
             "swap_used=$(free -m | awk '/Swap/ {print $3}')M" \
             "io_pressure=${psi#avg10=}" \
             "${svc_mem}" \
             "D-state: ${dstate:-none}"
        sleep 30
    done
}

# ---------------------------------------------------------------- on / off
on() {
    echo "==> Extended debugging on"
    sudo install -m 755 "$(readlink -f "$0")" "$MONITOR_BIN"
    sudo tee "$MONITOR_UNIT" >/dev/null <<UNIT
[Unit]
Description=Zorcam debug monitor (power, memory, disk waits every 30 s)

[Service]
ExecStart=$MONITOR_BIN monitor
SyslogIdentifier=zorcam-nvr-debug
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable -q --now zorcam-nvr-debug-monitor
    ok "monitor: a line every 30 s (journalctl -t zorcam-nvr-debug)"

    # the journal on disk: the lines before a freeze or a hard reset survive the reboot
    sudo mkdir -p /etc/systemd/journald.conf.d /var/log/journal
    printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\n' | sudo tee "$JOURNALD_CONF" >/dev/null
    sudo systemctl restart systemd-journald
    ok "journal kept on disk, up to 200 MB (journalctl -b -1 shows the previous boot)"

    # the kernel reports processes stuck on the disk after 30 s instead of 120 s, and every time
    printf 'kernel.hung_task_timeout_secs = 30\nkernel.hung_task_warnings = -1\n' | sudo tee "$SYSCTL_CONF" >/dev/null
    sudo sysctl -q -p "$SYSCTL_CONF"
    ok "kernel: hung task warnings after 30 s"

    # Zorcam: request times in the gallery, capture statistics every minute, ffmpeg warnings, go2rtc debug log
    for s in "${SERVICES[@]}"; do
        sudo mkdir -p "$(dirname "$(dropin "$s")")"
        printf '[Service]\nEnvironment=ZORCAM_DEBUG=1\nEnvironment=ZORCAM_GO2RTC_LOG=debug\n' \
            | sudo tee "$(dropin "$s")" >/dev/null
    done
    sudo systemctl daemon-reload
    sudo systemctl restart "${SERVICES[@]}"
    ok "Zorcam services restarted with ZORCAM_DEBUG=1"
    echo
    echo "Reproduce the problem, then: ~/zorcam-nvr/app/debug.sh save   (after a freeze and a reboot too)"
    echo "Turn it off when done:       ~/zorcam-nvr/app/debug.sh off"
}

off() {
    echo "==> Back to production"
    if [ -f "$MONITOR_UNIT" ]; then
        sudo systemctl disable -q --now zorcam-nvr-debug-monitor 2>/dev/null || true
        sudo rm -f "$MONITOR_UNIT"
    fi
    sudo rm -f "$MONITOR_BIN"
    ok "monitor removed"

    local changed=0
    for s in "${SERVICES[@]}"; do
        if [ -f "$(dropin "$s")" ]; then
            sudo rm -f "$(dropin "$s")"
            sudo rmdir "$(dirname "$(dropin "$s")")" 2>/dev/null || true
            changed=1
        fi
    done
    sudo systemctl daemon-reload
    if [ "$changed" = 1 ]; then
        sudo systemctl restart "${SERVICES[@]}"
        ok "Zorcam services restarted without ZORCAM_DEBUG"
    fi

    if [ -f "$SYSCTL_CONF" ]; then
        sudo rm -f "$SYSCTL_CONF"
        sudo sysctl -q kernel.hung_task_timeout_secs=120 kernel.hung_task_warnings=10
    fi
    ok "kernel: default hung task warnings"

    # production: the journal in memory only, to spare the SD card (the logs saved on disk are deleted)
    sudo rm -f "$JOURNALD_CONF"
    sudo rm -rf /var/log/journal
    sudo systemctl restart systemd-journald
    ok "journal in memory only (saved logs deleted; ~/zorcam-nvr-debug-*.tar.gz files are kept)"
}

status() {
    echo "==> Debugging status"
    if systemctl is-active -q zorcam-nvr-debug-monitor 2>/dev/null; then ok "monitor running"; else echo "  --  monitor off"; fi
    for s in "${SERVICES[@]}"; do
        if [ -f "$(dropin "$s")" ]; then ok "$s: ZORCAM_DEBUG=1"; else echo "  --  $s: production"; fi
    done
    if [ -d /var/log/journal ]; then
        warn "journal on disk ($(sudo du -sh /var/log/journal 2>/dev/null | cut -f1)): more writes to the SD card"
    else
        echo "  --  journal in memory only"
    fi
    echo "  $(vcgencmd get_throttled 2>/dev/null || echo "throttled=n/a")   (0x0: power was fine since boot)"
}

# ---------------------------------------------------------------- save
save() {
    local stamp dir out old_umask
    old_umask="$(umask)"
    umask 077  # the journal holds addresses and user names: the folder and the archive are for this user only
    stamp="$(date +%Y%m%d-%H%M)"
    dir="$(mktemp -d)/zorcam-nvr-debug-$stamp"
    out="$HOME/zorcam-nvr-debug-$stamp.tar.gz"
    mkdir -p "$dir"
    echo "==> Collecting into $out"
    {
        date; uname -a; uptime
        vcgencmd get_throttled; vcgencmd measure_temp; vcgencmd measure_clock arm; vcgencmd measure_volts core
        echo; free -m; echo; df -h / /dev/shm
        echo; ps -eo stat,pid,etime,rss,pcpu,args --sort=-rss | head -25
    } > "$dir/system.txt" 2>&1 || true
    systemctl --no-pager status "${SERVICES[@]}" zorcam-nvr-debug-monitor > "$dir/services.txt" 2>&1 || true
    journalctl --list-boots --no-pager > "$dir/boots.txt" 2>&1 || true
    journalctl -b 0 --no-pager > "$dir/journal-this-boot.txt" 2>&1 || true
    journalctl -b -1 --no-pager > "$dir/journal-previous-boot.txt" 2>&1 || true
    journalctl -t zorcam-nvr-debug --since "-2 days" --no-pager > "$dir/monitor.txt" 2>&1 || true
    { sudo dmesg -T || true; } 2>&1 | cat > "$dir/dmesg.txt"
    # values without passwords; the settings belong to the zorcam-nvr user
    { sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_config.py || true; } 2>&1 | cat > "$dir/zorcam-conf.txt"
    cat /opt/zorcam-nvr/gallery/BUILD > "$dir/build.txt" 2>/dev/null || true
    # passwords never leave the Pi: mask any login:password@ in URLs, just in case
    sed -i -E 's#(rtsps?://[^:/@[:space:]]*):[^@[:space:]]*@#\1:****@#g' "$dir"/*.txt
    tar -czf "$out" -C "$(dirname "$dir")" "$(basename "$dir")"
    chmod 600 "$out"  # an archive of the same minute left from before keeps its old rights otherwise
    rm -rf "$(dirname "$dir")"
    umask "$old_umask"
    ok "$out ($(du -h "$out" | cut -f1))"
    echo "Copy it to your computer: scp admin@$(hostname -I | awk '{print $1}'):$out ."
}

[ "$(id -u)" != 0 ] || [ "${1:-}" = "monitor" ] || { echo "run as a regular user with sudo (e.g. admin), not as root"; exit 1; }
case "${1:-status}" in
    on) on ;;
    off) off ;;
    status) status ;;
    save) save ;;
    monitor) monitor ;;
    *) echo "usage: $0 on | off | status | save"; exit 2 ;;
esac
