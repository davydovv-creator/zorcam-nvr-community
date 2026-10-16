#!/usr/bin/env bash
# Install / update Zorcam on a Raspberry Pi (capture, gallery, go2rtc for WebRTC Live).
# Run from a clone of the repository as a user with sudo (e.g. admin):  ~/zorcam-nvr/app/install.sh
# The services run as their own system user zorcam-nvr: the program goes to /opt/zorcam-nvr (read-only for it), settings and
# recordings to /var/lib/zorcam-nvr (config/, recordings/).
# Safe to re-run: only changed files are copied, only affected services are restarted.
# Settings, users and cameras are set afterwards in the browser: the setup wizard opens on the first visit.
set -euo pipefail

GO2RTC_VERSION="${GO2RTC_VERSION:-1.9.14}"  # tested version; can be overridden
GO2RTC_BIN=/usr/local/bin/go2rtc
FRP_VERSION="${FRP_VERSION:-0.61.1}"  # frpc: the tunnel to the Zorcam cloud (used only when connected in Settings)
FRPC_BIN=/usr/local/bin/frpc
SVC_USER=zorcam-nvr
PREFIX=/opt/zorcam-nvr
STATE=/var/lib/zorcam-nvr
CONFIG_DIR="$STATE/config"
DATA_DIR="$STATE/recordings"
SECRETS="$CONFIG_DIR/secrets.env"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_off=$'\e[0m'
step() { echo; echo "==> $*"; }
ok()   { echo "  ${c_ok}OK${c_off}  $*"; }
warn() { echo "  ${c_warn}!!${c_off}  $*"; WARNINGS=$((WARNINGS + 1)); }
die()  { echo "  ${c_err}ERROR${c_off}  $*" >&2; exit 1; }
WARNINGS=0

# ---------------------------------------------------------------- checks
step "Environment"
[ "$(id -u)" != 0 ] || die "run as a regular user with sudo (e.g. admin), not as root"
[ -f "$SRC/gallery/app.py" ] && [ -f "$SRC/go2rtc/go2rtc.yaml" ] || die "the script must be in the app folder of the repository"
# sudo -v asks for a password unless every sudoers rule of the user is NOPASSWD (verifypw=all): admin is also in
# the sudo group, so first a plain command, which NOPASSWD lets through (install over SSH by the deploy agent, no terminal)
sudo -n true 2>/dev/null || sudo -v || die "sudo rights are required"
ok "user $(id -un) with sudo"
# the memory limits of capture and go2rtc are percents of RAM (a Pi 3 reports ~920 MB): below that it will not hold
MEM_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null)
if [ -n "$MEM_KB" ] && [ "$MEM_KB" -lt 819200 ]; then
    warn "less than 1 GB of memory: not supported, at most 1-2 cameras"
fi

# ---------------------------------------------------------------- the old names
# Before the repository became zorcam-nvr, Zorcam was installed as zorcam (user zorcam, /opt/zorcam, /var/lib/zorcam,
# zorcam-* services).
# Renamed once: recordings, settings, users, push subscriptions and the cloud link stay; every step can be repeated
# after a failure. Units are named one by one: zorcam-* also matches zorcam-nvr-*
rename_old() {  # rename_old OLD_NAME
    local old="$1" unit f debug_on=0
    step "Renaming the installation $old to $SVC_USER"
    for unit in go2rtc-config.path capture gallery go2rtc cloud debug-monitor; do
        sudo systemctl disable -q --now "$old-$unit" 2>/dev/null || true
    done
    for unit in capture gallery go2rtc cloud debug-monitor go2rtc-config; do
        sudo rm -rf "$UNIT_DIR/$old-$unit.service" "$UNIT_DIR/$old-$unit.service.d"
    done
    sudo rm -f "$UNIT_DIR/$old-go2rtc-config.path" "/usr/local/bin/$old-debug-monitor"
    sudo systemctl daemon-reload
    # the debug settings (debug.sh on) stay on under the new names
    for f in "/etc/systemd/journald.conf.d/$old-debug.conf" "/etc/sysctl.d/90-$old-debug.conf"; do
        if sudo test -f "$f"; then sudo mv "$f" "${f/$old-debug/$SVC_USER-debug}"; debug_on=1; fi
    done
    [ "$debug_on" = 0 ] || warn "debugging was on: run $SRC/debug.sh on again to start its monitor"
    if getent group "$old" >/dev/null && ! getent group "$SVC_USER" >/dev/null; then
        sudo groupmod -n "$SVC_USER" "$old"
    fi
    if id "$old" >/dev/null 2>&1 && ! id "$SVC_USER" >/dev/null 2>&1; then
        sudo usermod -l "$SVC_USER" -d "$STATE" "$old"
    fi
    if sudo test -d "/var/lib/$old" && ! sudo test -e "$STATE"; then
        sudo mv "/var/lib/$old" "$STATE"
    fi
    sudo rm -f "$STATE/.zorcam.conf.applied"  # all services start anew anyway
    sudo rm -rf "/opt/$old"
    sudo test -d "/var/lib/$old" && die "/var/lib/$old is still there next to $STATE: move what you need by hand"
    ok "now user $SVC_USER, $STATE, $PREFIX, $SVC_USER-* services"
}
old=zorcam
if [ -f "$UNIT_DIR/$old-gallery.service" ] || id "$old" >/dev/null 2>&1 || sudo test -d "/var/lib/$old"; then
    rename_old "$old"
fi

# ---------------------------------------------------------------- service user and folders
step "Service user $SVC_USER"
if ! id "$SVC_USER" >/dev/null 2>&1; then
    # a system user: no password, no login, no sudo; systemd-logind leaves its /dev/shm files alone (RemoveIPC)
    sudo useradd --system --user-group --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    ok "created the system user $SVC_USER"
fi
# the installing user reads the recordings (du, scp) through the group; the settings stay private (700)
if ! id -nG "$(id -un)" | tr ' ' '\n' | grep -qx "$SVC_USER"; then
    sudo usermod -aG "$SVC_USER" "$(id -un)"
    ok "$(id -un) added to the group $SVC_USER (reading the recordings; works from the next login)"
fi
for d in "$STATE:750" "$DATA_DIR:750" "$CONFIG_DIR:700" "$STATE/storage:750"; do
    sudo test -d "${d%%:*}" || sudo install -d -o "$SVC_USER" -g "$SVC_USER" -m "${d##*:}" "${d%%:*}"
done
# what the recordings disk service (root) keeps: the chosen disk and its state; the gallery only reads it
sudo test -d /var/lib/zorcam-nvr-storage || sudo install -d -o root -g "$SVC_USER" -m 750 /var/lib/zorcam-nvr-storage
if ! sudo test -f "$SECRETS"; then
    sudo install -o "$SVC_USER" -g "$SVC_USER" -m 600 "$SRC/../config/secrets.env.example" "$SECRETS"
    ok "created $SECRETS (camera passwords are saved there by the gallery)"
fi
ok "$STATE: config/ (settings, private), recordings/"

# ---------------------------------------------------------------- packages
step "Packages"
missing=()
for pkg in ffmpeg python3-pil python3-cryptography curl fdisk; do  # fdisk: sfdisk for the recordings disk
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "  installing: ${missing[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y -qq "${missing[@]}"
fi
ok "ffmpeg, python3-pil, python3-cryptography, curl, fdisk"

# ---------------------------------------------------------------- downloads
# SHA-256 of the release files of the tested versions: a file changed on the way or on GitHub is not installed
# (it runs as root). frp: from its frp_sha256_checksums.txt; go2rtc publishes none: the files of the release.
known_sha256() {
    case "$1" in
        go2rtc-1.9.14-go2rtc_linux_arm64) echo 359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50 ;;
        go2rtc-1.9.14-go2rtc_linux_arm)   echo 4d7e1639af5a2722a28e864468fd8099b3c1682565446c798bf9e3b38fde12e4 ;;
        go2rtc-1.9.14-go2rtc_linux_armv6) echo 4dc20370556b29f3a90f4c7a09dcd95472c8f74cca56d4d1fb91f32bdd15174c ;;
        go2rtc-1.9.14-go2rtc_linux_amd64) echo 32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6 ;;
        frp-0.61.1-arm64) echo af6366f2b43920ebfe6235dba6060770399ed1fb18601e5818552bd46a7621f8 ;;
        frp-0.61.1-arm)   echo 8c7ebb07916e69d959d2a90f80900850c41b6dc1decdb87ccba4bc2f3574f855 ;;
        frp-0.61.1-amd64) echo bff260b68ca7b1461182a46c4f34e9709ba32764eed30a15dd94ac97f50a2c40 ;;
    esac
}
# check_sha256 FILE NAME: 0 if the file is the known one; another version (GO2RTC_VERSION=..., FRP_VERSION=...)
# has no checksum here and is installed with a warning
check_sha256() {
    local want
    want="$(known_sha256 "$2")"
    if [ -z "$want" ]; then
        warn "no checksum of $2 in install.sh: installed without the check"
        return 0
    fi
    [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$want" ]
}

# ---------------------------------------------------------------- go2rtc
step "go2rtc $GO2RTC_VERSION"
case "$(uname -m)" in
    aarch64) asset=go2rtc_linux_arm64 ;;
    armv7l)  asset=go2rtc_linux_arm ;;
    armv6l)  asset=go2rtc_linux_armv6 ;;
    x86_64)  asset=go2rtc_linux_amd64 ;;
    *) die "unknown architecture $(uname -m)" ;;
esac
RESTART_GO2RTC=0
current="$("$GO2RTC_BIN" -version 2>/dev/null | awk '{print $3}' || true)"
if [ "$current" = "$GO2RTC_VERSION" ]; then
    ok "already installed ($asset)"
else
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    curl -fsSL --retry 3 -o "$tmp" \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/${asset}" \
        || die "could not download go2rtc (access to github.com is required)"
    check_sha256 "$tmp" "go2rtc-$GO2RTC_VERSION-$asset" \
        || die "the downloaded go2rtc is not the released file (checksum mismatch): not installed"
    chmod +x "$tmp"
    "$tmp" -version >/dev/null 2>&1 || die "the downloaded go2rtc does not run on this system"
    sudo install -m 755 "$tmp" "$GO2RTC_BIN"
    RESTART_GO2RTC=1
    ok "installed $("$GO2RTC_BIN" -version | awk '{print $3, $4}') ($asset)"
fi

# ---------------------------------------------------------------- frpc (Zorcam cloud, optional)
step "frpc $FRP_VERSION (Zorcam cloud tunnel)"
case "$(uname -m)" in
    aarch64) frp_arch=arm64 ;;
    armv7l|armv6l) frp_arch=arm ;;
    x86_64)  frp_arch=amd64 ;;
    *)       frp_arch="" ;;
esac
RESTART_CLOUD=0
current="$("$FRPC_BIN" -v 2>/dev/null || true)"
if [ -z "$frp_arch" ]; then
    warn "no frpc for $(uname -m): the Zorcam cloud will not connect, everything else works"
elif [ "$current" = "$FRP_VERSION" ]; then
    ok "already installed"
else
    frp_tmp="$(mktemp -d)"
    frp_name="frp_${FRP_VERSION}_linux_${frp_arch}"
    # the cloud is optional: without frpc the Pi works on its own, so a failed download is only a warning
    if curl -fsSL --retry 3 -o "$frp_tmp/frp.tar.gz" \
            "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${frp_name}.tar.gz" \
        && { check_sha256 "$frp_tmp/frp.tar.gz" "frp-$FRP_VERSION-$frp_arch" \
             || { warn "the downloaded frpc is not the released file (checksum mismatch)"; false; }; } \
        && tar -xzf "$frp_tmp/frp.tar.gz" -C "$frp_tmp" "$frp_name/frpc" \
        && "$frp_tmp/$frp_name/frpc" -v >/dev/null 2>&1; then
        sudo install -m 755 "$frp_tmp/$frp_name/frpc" "$FRPC_BIN"
        RESTART_CLOUD=1
        ok "installed frpc $("$FRPC_BIN" -v)"
    else
        warn "could not download frpc (github.com): the Zorcam cloud will not connect, everything else works"
    fi
    rm -rf "$frp_tmp"
fi

# ---------------------------------------------------------------- files
# copy SRC DST MODE -> returns 0 when the file changed
copy() {  # the program belongs to root: the services can read it, not change it
    if cmp -s "$1" "$2"; then return 1; fi
    sudo install -D -m "$3" "$1" "$2"
    echo "  updated $2"
    return 0
}
sudo_copy() {
    if sudo cmp -s "$1" "$2"; then return 1; fi
    sudo install -m 644 "$1" "$2"
    echo "  updated $2"
    return 0
}

step "Application files"
RESTART_CAPTURE=0; RESTART_GALLERY=0; UNITS_CHANGED=0
copy "$SRC/capture/daemon.py"        "$PREFIX/capture/daemon.py"    755 && RESTART_CAPTURE=1 || true
copy "$SRC/gallery/app.py"           "$PREFIX/gallery/app.py"       755 && RESTART_GALLERY=1 || true
copy "$SRC/gallery/push.py"          "$PREFIX/gallery/push.py"      644 && RESTART_GALLERY=1 || true
copy "$SRC/gallery/onvif.py"         "$PREFIX/gallery/onvif.py"     644 && RESTART_GALLERY=1 || true
copy "$SRC/gallery/yandex.py"        "$PREFIX/gallery/yandex.py"    644 && RESTART_GALLERY=1 || true
copy "$SRC/gallery/stun.py"          "$PREFIX/gallery/stun.py"      644 && RESTART_GALLERY=1 || true
copy "$SRC/gallery/zorcam_users.py"   "$PREFIX/gallery/zorcam_users.py" 755 && RESTART_GALLERY=1 || true
copy "$SRC/gallery/zorcam_html.py"    "$PREFIX/gallery/zorcam_html.py"  644 && RESTART_GALLERY=1 || true
copy "$SRC/common/zorcam_config.py"   "$PREFIX/capture/zorcam_config.py" 644 && RESTART_CAPTURE=1 || true
copy "$SRC/common/zorcam_config.py"   "$PREFIX/gallery/zorcam_config.py" 755 && RESTART_GALLERY=1 || true
copy "$SRC/common/zorcam_i18n.py"     "$PREFIX/capture/zorcam_i18n.py"  644 && RESTART_CAPTURE=1 || true
copy "$SRC/common/zorcam_i18n.py"     "$PREFIX/gallery/zorcam_i18n.py"  644 && RESTART_GALLERY=1 || true
copy "$SRC/common/zorcam_cloud.py"    "$PREFIX/gallery/zorcam_cloud.py" 644 && RESTART_GALLERY=1 || true
copy "$SRC/common/zorcam_cloud.py"    "$PREFIX/cloud/zorcam_cloud.py"   644 && RESTART_CLOUD=1 || true
copy "$SRC/common/zorcam_config.py"   "$PREFIX/cloud/zorcam_config.py"  644 && RESTART_CLOUD=1 || true
copy "$SRC/common/zorcam_i18n.py"     "$PREFIX/cloud/zorcam_i18n.py"    644 && RESTART_CLOUD=1 || true
copy "$SRC/cloud/daemon.py"          "$PREFIX/cloud/daemon.py"        755 && RESTART_CLOUD=1 || true
# the recordings disk (USB): run by systemd as root, on a timer and on a request of the gallery; nothing to restart
copy "$SRC/storage/zorcam_storage.py" "$PREFIX/storage/zorcam_storage.py" 755 || true
copy "$SRC/storage/zorcam_storage.py" "$PREFIX/gallery/zorcam_storage.py" 644 && RESTART_GALLERY=1 || true  # USB disks, state
# page markup: the templates, CSS and JS are read once when the gallery starts (and the version hash in the address of a
# CSS/JS file, /static/app.css?v=<hash>, and of an app icon, /apple-touch-icon.png?v=<hash>, is computed then), so a change
# of any of them restarts it
for f in "$SRC"/gallery/templates/*; do
    copy "$f" "$PREFIX/gallery/templates/$(basename "$f")" 644 && RESTART_GALLERY=1 || true
done
for f in "$SRC"/gallery/static/*; do
    copy "$f" "$PREFIX/gallery/static/$(basename "$f")" 644 && RESTART_GALLERY=1 || true
done
copy "$SRC/go2rtc/go2rtc.yaml"       "$PREFIX/go2rtc/go2rtc.yaml"    644 && RESTART_GO2RTC=1  || true
# the edition: box on the Zorcam Box image (ZORCAM_EDITION=box), community otherwise; a later run without the variable
# keeps what is installed (the deploy agent installs updates without it)
EDITION="${ZORCAM_EDITION:-$(cat "$PREFIX/EDITION" 2>/dev/null || echo community)}"
case "$EDITION" in community|box) ;; *) die "ZORCAM_EDITION must be community or box, not $EDITION" ;; esac
EDITION_TMP="$(mktemp)"
echo "$EDITION" > "$EDITION_TMP"
copy "$EDITION_TMP" "$PREFIX/EDITION" 644 && { RESTART_GALLERY=1; RESTART_CLOUD=1; } || true
rm -f "$EDITION_TMP"
# which commit is installed, for bug reports (cat /opt/zorcam-nvr/gallery/BUILD, debug.sh save); no restart needed
BUILD_TMP="$(mktemp)"
{ git -C "$SRC" describe --tags --always --dirty 2>/dev/null || echo "not from git"
  git -C "$SRC" log -1 --format=' from %cd' --date=format:'%Y-%m-%d' 2>/dev/null || true; } | tr -d '\n' > "$BUILD_TMP"
copy "$BUILD_TMP" "$PREFIX/gallery/BUILD" 644 || true
rm -f "$BUILD_TMP"
# only when needed: a chmod is a change for zorcam-nvr-go2rtc-config.path (IN_ATTRIB), and every run of this script
# restarted go2rtc, dropping Live and the stream recorders for a few seconds
[ "$(sudo stat -c %a "$SECRETS")" = 600 ] || sudo chmod 600 "$SECRETS"
sudo rm -f "$CONFIG_DIR/user_prefs.json"  # obsolete: the language is picked and kept on each device
ok "folders $PREFIX/capture, $PREFIX/gallery, $PREFIX/go2rtc, $PREFIX/cloud, $PREFIX/storage"

step "Settings"
CONF="$CONFIG_DIR/zorcam.conf"
as_svc() { sudo -u "$SVC_USER" "$@"; }  # the settings are readable only by the services' user
if ! sudo test -f "$CONF"; then
    sudo install -o "$SVC_USER" -g "$SVC_USER" -m 644 "$SRC/../config/zorcam.conf.example" "$CONF"
    ok "created $CONF (all values are defaults, change what you need)"
fi
if conf_out="$(as_svc python3 "$PREFIX/gallery/zorcam_config.py" 2>&1)"; then
    ok "$CONF has no errors"
else
    warn "$CONF has errors (defaults are used for them):"
    echo "$conf_out" | sed -n '/^Errors:/,$p' | tail -n +2
fi
# zorcam.conf changed since the last run: restart the services to apply it
CONF_STAMP="$STATE/.zorcam.conf.applied"
conf_hash="$(sudo sha256sum "$CONF" | cut -d' ' -f1)"
if [ "$(sudo cat "$CONF_STAMP" 2>/dev/null)" != "$conf_hash" ]; then
    RESTART_CAPTURE=1; RESTART_GALLERY=1; RESTART_GO2RTC=1
    echo "  $CONF changed: zorcam-nvr-capture, zorcam-nvr-gallery and zorcam-nvr-go2rtc will be restarted"
fi

step "Recordings"
CAMERA_IDS="$(as_svc python3 "$PREFIX/gallery/zorcam_config.py" --camera-ids)"
if [ -n "$CAMERA_IDS" ]; then
    ok "cameras: $(echo "$CAMERA_IDS" | tr '\n' ' ')(recordings in $DATA_DIR/<id> and <id>_videos)"
else
    ok "no cameras yet: they are added in the browser after the installation"
fi

step "systemd"
sudo_copy "$SRC/capture/zorcam-nvr-capture.service" "$UNIT_DIR/zorcam-nvr-capture.service" && { UNITS_CHANGED=1; RESTART_CAPTURE=1; } || true
sudo_copy "$SRC/gallery/zorcam-nvr-gallery.service" "$UNIT_DIR/zorcam-nvr-gallery.service" && { UNITS_CHANGED=1; RESTART_GALLERY=1; } || true
sudo_copy "$SRC/go2rtc/zorcam-nvr-go2rtc.service"    "$UNIT_DIR/zorcam-nvr-go2rtc.service"   && { UNITS_CHANGED=1; RESTART_GO2RTC=1; }  || true
sudo_copy "$SRC/cloud/zorcam-nvr-cloud.service"     "$UNIT_DIR/zorcam-nvr-cloud.service"    && { UNITS_CHANGED=1; RESTART_CLOUD=1; }   || true
# go2rtc does not watch zorcam.conf itself: this path unit restarts it when the file changes (e.g. saved on /config)
sudo_copy "$SRC/go2rtc/zorcam-nvr-go2rtc-config.path"    "$UNIT_DIR/zorcam-nvr-go2rtc-config.path"    && UNITS_CHANGED=1 || true
sudo_copy "$SRC/go2rtc/zorcam-nvr-go2rtc-config.service" "$UNIT_DIR/zorcam-nvr-go2rtc-config.service" && UNITS_CHANGED=1 || true
# the recordings disk: mounted at boot before the detector, checked every minute, a request of the gallery at once
for unit in zorcam-nvr-storage.service zorcam-nvr-storage.timer zorcam-nvr-storage.path; do
    sudo_copy "$SRC/storage/$unit" "$UNIT_DIR/$unit" && UNITS_CHANGED=1 || true
done
[ "$UNITS_CHANGED" = 1 ] && sudo systemctl daemon-reload
sudo systemctl enable -q zorcam-nvr-capture zorcam-nvr-gallery zorcam-nvr-go2rtc zorcam-nvr-cloud
sudo systemctl enable -q --now zorcam-nvr-go2rtc-config.path
sudo systemctl enable -q zorcam-nvr-storage.service
sudo systemctl enable -q --now zorcam-nvr-storage.timer zorcam-nvr-storage.path

restart_if() {  # restart_if FLAG SERVICE
    if [ "$1" = 1 ] || ! systemctl is-active -q "$2"; then
        sudo systemctl restart "$2"
        echo "  restarted $2"
    fi
}
restart_if "$RESTART_GO2RTC"  zorcam-nvr-go2rtc
restart_if "$RESTART_GALLERY" zorcam-nvr-gallery
restart_if "$RESTART_CAPTURE" zorcam-nvr-capture
# the cloud is optional: if its service does not start, the rest of the installation goes on
restart_if "$RESTART_CLOUD"   zorcam-nvr-cloud || warn "zorcam-nvr-cloud did not start: journalctl -u zorcam-nvr-cloud -n 30"
echo "$conf_hash" | sudo tee "$CONF_STAMP" >/dev/null
ok "services enabled"

# ---------------------------------------------------------------- firewall
if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q "Status: active"; then
    step "Firewall (ufw)"
    sudo ufw allow 8555/tcp >/dev/null
    sudo ufw allow 8555/udp >/dev/null
    ok "port 8555 TCP/UDP opened for WebRTC"
fi

# ---------------------------------------------------------------- memory limits
# Raspberry Pi kernels boot with cgroup_disable=memory: MemoryMax in the units then does nothing, and a runaway
# process takes the memory of the whole Pi (out of memory, SSH hangs). cgroup_enable=memory turns it on after a reboot.
CMDLINE=/boot/firmware/cmdline.txt
if ! grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null && [ -f "$CMDLINE" ]; then
    step "Memory limits of the services"
    if ! grep -qw "cgroup_enable=memory" "$CMDLINE"; then
        sudo cp "$CMDLINE" "$CMDLINE.bak"
        sudo sed -i '1 s/$/ cgroup_enable=memory cgroup_memory=1/' "$CMDLINE"
        ok "cgroup_enable=memory added to $CMDLINE (the old one: $CMDLINE.bak)"
    fi
    warn "the memory limits start working after a reboot: sudo reboot"
fi

# ---------------------------------------------------------------- verification
step "Verification"
sleep 2
for svc in zorcam-nvr-go2rtc zorcam-nvr-gallery zorcam-nvr-capture zorcam-nvr-cloud; do
    if systemctl is-active -q "$svc"; then ok "$svc is running"
    else warn "$svc is not running: journalctl -u $svc -n 30"; fi
done

api_up=0
for _ in $(seq 1 10); do
    curl -fs -m 2 http://127.0.0.1:1984/api/streams >/dev/null 2>&1 && { api_up=1; break; }
    sleep 1
done
if [ "$api_up" = 1 ]; then
    ok "go2rtc API answers on 127.0.0.1:1984"
    # go2rtc connects to each switched-on camera and reports the codec (a switched-off one it does not connect to)
    for id in $(as_svc python3 "$PREFIX/gallery/zorcam_config.py" --camera-ids --enabled); do
        probe="$(curl -fs -m 25 "http://127.0.0.1:1984/api/streams?src=$id&video=all" 2>/dev/null || true)"
        if echo "$probe" | grep -q '"codec_name": *"h264"'; then
            ok "camera $id connects, codec H.264"
        elif echo "$probe" | grep -Eq '"codec_name": *"(hevc|h265)"'; then
            ok "camera $id connects, codec H.265 (Live: any browser on iPhone and iPad, Safari on Mac; Chrome and Firefox on Android and computers may need H.264)"
        else
            warn "go2rtc could not get the stream of camera $id: check its address, login and password on the Settings page and journalctl -u zorcam-nvr-go2rtc -n 30"
        fi
    done
else
    warn "go2rtc API does not answer: journalctl -u zorcam-nvr-go2rtc -n 30"
fi

if ss -lnu 2>/dev/null | grep -q ':8555 ' && ss -lnt 2>/dev/null | grep -q ':8555 '; then
    ok "WebRTC listens on port 8555 (TCP and UDP)"
else
    warn "nothing listens on port 8555"
fi

# /healthz: 200 whenever the gallery runs (/login redirects to the setup wizard while there are no users)
code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1/healthz || true)"
if [ "$code" = 200 ]; then ok "gallery answers on port 80"
else warn "gallery does not answer (HTTP $code): journalctl -u zorcam-nvr-gallery -n 30"; fi

echo
ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
has_users() {
    as_svc python3 -c 'import sys, json; d = json.load(open(sys.argv[1])); sys.exit(0 if any(isinstance(v, dict) for v in d.values()) else 1)' \
        "$CONFIG_DIR/users.json" 2>/dev/null
}
if [ "$WARNINGS" = 0 ]; then
    echo "${c_ok}Done.${c_off}"
else
    echo "${c_warn}Done with warnings: $WARNINGS.${c_off} Send this output if you need help."
fi
if ! has_users; then
    echo "Now open http://${ip:-<pi-address>}/ in a browser on the home network: the setup wizard asks for the"
    echo "general settings, the admin password and the cameras."
elif [ -z "$CAMERA_IDS" ]; then
    echo "Open http://${ip:-<pi-address>}/ and add a camera: Settings tab -> Cameras -> Add a camera."
else
    echo "Open http://${ip:-<pi-address>}/: the Live tab should show LIVE (not LIVE · HLS) for every camera."
fi
