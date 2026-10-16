# Zorcam

**English** · [Русский](README.ru.md)

A lightweight video surveillance system for the Raspberry Pi 3: frame capture from IP cameras over RTSP, motion detection, live streaming and a web gallery with sign-in. One or several cameras. Runs on Debian 13 (Raspberry Pi OS) without Docker and without Python dependencies beyond apt packages. After `install.sh` everything is set up in the browser: a setup wizard asks for the settings, the users and the cameras.

| | |
|---|---|
| Capture | a persistent RTSP connection to each camera through go2rtc, keyframes only (~2.5 frames/s); a snapshot for the Timelapse every 5 s (Snapshot interval of the camera) |
| On motion | a 10-second H.264 clip from the moment of motion, a push notification with the camera name |
| Gallery | port 80: Live (every camera, 1, 2 or 3 per row; 2, 3 or 4 on a laptop screen) / Records: Timelapse and Video (a tab per camera: a timelapse of the snapshots, motion videos), an iPhone app and a wide layout with a sidebar for a laptop, English and Russian |

## Live stream

The Live tab opens by default. The main way to show it is WebRTC via [go2rtc](https://github.com/AlexxIT/go2rtc): the full camera stream (every frame, 25 fps) without transcoding, with a ~0.3–0.5 s delay.

- The Live tab shows every camera under its name, with its own player. With several cameras, the buttons above them put 1, 2 or 3 cameras per row (2, 3 or 4 on a wide screen); the choice is kept on the device (in the browser), so a phone and a computer can show them differently. A player that shows no new picture for the WebRTC wait plus 15 s (stuck in Buffering, e.g. after the phone moves from Wi-Fi to LTE) reconnects by itself, WebRTC first; a change of network reconnects every player, and the round-arrow button above the players does the same by hand (in the iPhone app, pulling down over a player does not refresh the page). go2rtc keeps a single connection to each camera: the detector reads it all the time, Live players and HLS share it.
- Pan, tilt and zoom: for an ONVIF camera that can turn (PTZ), a fold-out "Camera control" toggle appears under its player (closed after every page load), with arrow and zoom buttons in it (for `viewer` and `admin`, not for `liveviewer`; only for the cameras enabled for the user). Hold a button to move, release to stop; on a laptop, hover over the camera with its control open and use the arrow keys and `+` / `-`. Presets are not there yet. The gallery talks to the camera over ONVIF with the camera's login and its password (the same as for RTSP) at the camera's "ONVIF address" (home network addresses only): a camera found by the ONVIF search keeps the address it answered from, for a camera added by hand fill in the "ONVIF address" field, e.g. `http://192.168.1.50:8000/onvif/device_service`. There is no fallback address: without it the control toggle under the player is inactive. Some cameras (for example Bootslab) accept ONVIF only for a separate ONVIF user created in the camera's settings: then use that login and password for the camera in Zorcam. Every move command carries a 2 s timeout, so the camera stops by itself if the stop is lost. No buttons: the camera has no PTZ, ONVIF is off, or the login is refused (see `journalctl -u zorcam-nvr-gallery | grep ptz`).
- The go2rtc API listens on `127.0.0.1:1984` only. The browser exchanges SDP with go2rtc through the gallery (`POST /live/webrtc`), so the gallery sign-in applies.
- Video goes from go2rtc to the browser directly over port **8555 (TCP and UDP)**. This port must be reachable from the network the gallery is opened from.
- The go2rtc RTSP server listens on `127.0.0.1:8554` only (not reachable from the network). The detector and HLS read the camera there, so the camera password never appears in ffmpeg command lines, which every user of the Pi can see with `ps`.
- For Live in any browser the camera must send H.264. go2rtc passes H.265 over WebRTC without re-encoding, and every browser on iPhone and iPad shows it (they all run on the Safari engine), as does Safari on Mac (iOS 18, macOS 15 and later); Chrome and Firefox on Android and computers may not.

The fallback is HLS. If WebRTC does not connect within 8 s (port 8555 closed, a browser without WebRTC), the player switches to HLS. The gallery starts ffmpeg on demand, which repackages the stream from go2rtc into HLS without transcoding (~3% of one Pi 3 core). Segments are written to `/dev/shm` to spare the SD card. When the player has not requested the stream for 30 s, ffmpeg stops. Safari plays HLS natively; other browsers load [hls.js](https://github.com/video-dev/hls.js) from jsDelivr and therefore need internet access. HLS has a few seconds of delay. A 1080p stream takes about 3 Mbit/s in both modes.

WebRTC from the internet: forward port 8555 (TCP and UDP) to the Pi on the router, nothing else is needed. The gallery finds the external IP of the Pi itself by STUN (the Zorcam cloud's server if the Pi is linked, then `stun.cloudflare.com`, then `stun.l.google.com`; every 5 min, in the background) and adds it, together with the address the app is opened at when that is public (the external IP or your router's dynamic DNS domain, its IP looked up in DNS), with port 8555 to the go2rtc answer for the browser; a new IP from the provider is picked up by itself. If no STUN server answers, or the provider gives a non-public address (CGNAT), Settings -> Status shows "External address: not determined" (amber, not an alarm) and Live from the internet falls back to HLS. At home, by the local address, go2rtc gives the Pi's local addresses. Through the Zorcam cloud no forwarded port is needed at all: the cloud's TURN server relays the video. go2rtc does no STUN gathering for each viewer (`ice_servers: []`): it answered only when that was over, and a STUN server that did not answer (for example, routed into a VPN on the router) held every Live start for 5 s. If the external port on the router is not 8555, change `WEBRTC_PORT` in `app/gallery/app.py`.

## iPhone app and sign-in

The gallery can be added to the Home Screen as a separate app. Open the gallery in Safari, tap Share, then Add to Home Screen. While the gallery is open in Safari on an iPhone or iPad, the header shows the Apple logo: tapping it shows these steps. The app opens straight on the Live tab, without the address bar and Safari panels, with its own icon. On Android and in a regular browser the gallery works as a web page; Chrome on Android may also offer to install it.

Sign-in is remembered for a year and extended on every visit, so it does not expire while the app is used. Sessions are stored in `/var/lib/zorcam-nvr/config/sessions.json` (mode 600, token hashes only), so they survive gallery and Pi restarts. With a valid session the sign-in page goes straight to Live. Sign out is on the Settings page (the Settings tab in the bottom bar); it deletes the session.

The Home Screen app has its own cookies, separate from Safari, so you sign in to it once separately.

**Wide screen.** On a screen 1024 px wide or more (a laptop) the gallery is a web app with a left sidebar (Live, Records, Settings, and who is signed in at the bottom) instead of the bottom bar; the content is a centered column. Live shows 2, 3 or 4 cameras per row, Videos are a grid of 3 to 5 columns with the day headers sticking to the top while scrolling, Timelapse is the player, Settings is a list of tabs on the left and the chosen tab on the right, the sign-in and the setup wizard are a narrow centered column. Below 1024 px (phones, tablets in portrait) nothing changes. Everything can be reached from the keyboard (the focus is outlined).

## Settings

The Settings tab in the bottom bar opens the Settings page for every role. It has tabs: General — the language (see Language) and, for `admin`, General settings (the external IP address for WebRTC); Cameras — the cameras (Add a camera, Change) and the recordings of each camera with delete buttons (see Storage); Status — the version and the status of Zorcam and of the cameras; Users — who is signed in and Sign out, for `admin` also every user. On Cameras and Status the Home Screen app reloads the page with a pull-down gesture. Cameras and Status are shown to `admin` only. The page is the same on an iPhone and on a computer.

**Camera resolution**: 1080p or lower, set on the camera itself, not in Zorcam (see Camera setup).

**Cameras** are kept in `/var/lib/zorcam-nvr/config/cameras.json`: for each camera its ID, name, RTSP address without login and password, login, the optional second stream for the detector (see Camera setup), motion notifications and recording settings (see Storage). The passwords are in `/var/lib/zorcam-nvr/config/secrets.env` (mode 600), one line per camera: `ZORCAM_CAMERA_PASSWORD_<ID>` (the ID in capitals, `-` becomes `_`). Cameras are added in the setup wizard and on Settings → Add a camera; Settings → Change edits one or deletes it (with or without its recordings). Before saving, the gallery asks the camera itself (RTSP DESCRIBE); if it answers, for example, `401 Unauthorized`, the camera is not saved until the login is fixed or "Save anyway" is ticked. The camera ID is the name of its recordings folders and of its go2rtc stream, so it cannot be changed later. go2rtc builds the full URL with the password only when it connects (via `echo:python3 zorcam_config.py --rtsp-source <id>`); the detector and HLS take the stream from go2rtc. A new or changed camera appears in Live at once; the detector and go2rtc restart by themselves.

**Recent frame.** For `admin`, the top of the camera form (Settings → Change) shows the camera's newest saved frame, and Settings → Cameras shows a small thumbnail next to each camera, so it is clear which camera is which; the frame is the one the device already keeps on its storage (nothing extra is requested from the camera). A camera without frames yet (new, switched off) shows a "No frame" plate.

**Motion zones.** For `admin`, the camera form (Settings → Change) has a "Motion zones" row. It opens the camera's frame with a 16×9 grid: paint cells with a finger (a tap or a stroke switches them; the page does not scroll while painting), then choose "Leave out" (painted cells are not counted) or "Count only them" (only painted cells are counted). Nothing painted means no zones. The detector measures the change over the counted cells only, relative to their area but never relative to less than 5% of the frame, and the motion box on the poster stays inside them; `motion_threshold` is the same for all cameras. If everything is left out, the screen warns that motion will not be detected. The zones are in `/var/lib/zorcam-nvr/config/motion_zones.json` (mode 600; for each camera ID its mode and 144 cells `0`/`1`, row by row), not in `cameras.json`: saving them does not restart the camera, the detector notices the file within a few seconds. Deleting a camera removes its zones.

**Switch a camera off** without deleting it: the switch next to it in Settings → Cameras (or Change → Camera on). A switched-off camera is not connected, the detector and go2rtc leave it alone, it is not in Live or the cloud, and the "no frames" check skips it; its recordings, settings and password stay, and the same switch turns it on again (the camera is checked then, as when it is added).

**Sound in Live and clips**: Change → Sound in Live and clips (off by default). The sound goes as the camera sends it, never re-encoded on the Pi (or in the cloud): WebRTC plays G.711 (PCMA, PCMU) and Opus, HLS plays AAC; with another codec Live stays silent. The video starts muted (an iPhone plays only that by itself): the speaker button on the player turns the sound on. Saved clips get the sound too, also without re-encoding, but only if the camera sends AAC (set it in the camera): G.711 does not fit the MPEG-TS pieces clips are cut from and iPhone does not play it in mp4, so such a camera's clips stay without sound. The motion detector takes only the video.

**Other parameters** live in `/var/lib/zorcam-nvr/config/zorcam.conf` (INI format). `install.sh` creates it from `config/zorcam.conf.example`, where every parameter is described and commented out with its default value.

| Section | What it sets |
|---|---|
| `[language]` | default language on devices where no language is picked: auto (device language), en, ru |
| `[capture]` | motion threshold, camera reconnect timeouts |
| `[live]` | when to switch to HLS, when to stop HLS, the Pi's external IP for WebRTC (optional, see Live stream) |
| `[notifications]` | how often motion notifications come; failure notifications: on/off, reminders |
| `[health]` | how often to check the system and thresholds: no frames, disk space, temperature |
| `[gallery]` | clips per page (10, 30 or 60), sign-in duration, the stand (`environment`: prod, stage or test) |

How to change them:
- **In the app**, as `admin`: in the setup wizard; the external IP on Settings → General settings; the rest on the `/config` page (there is no link to it, type the address: `http://PI-IP-ADDRESS/config`). Every value is validated.
- **On the Pi**: `sudo nano /var/lib/zorcam-nvr/config/zorcam.conf`.

Either way, the detector and the gallery notice changes to `zorcam.conf` and restart by themselves (the detector within 10 s). To check the files: `sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_config.py` prints the resulting values, the cameras (without passwords) and errors. A parameter with an error takes its default value.

**Stands (prod, stage, test).** The same build runs on the test stands too. `environment` in `[gallery]` (`prod` by default, or `stage`, or `test`) says which stand the Pi is. Nothing changes for `prod`. On `test` and `stage` the app shows a TEST or STAGE badge next to the name Zorcam (in the header on a phone, in the sidebar on a laptop), the page title and the name under the Home Screen icon become `Zorcam test` or `Zorcam stage`, Settings → Status gets an Environment line, and the default address in Settings → General → Zorcam cloud is that stand's cloud, `https://cloud.test.zorcam.ru` or `https://cloud.stage.zorcam.ru` (for `prod`, `https://cloud.zorcam.ru`). An address already saved or paired stays as it is. To set it, add the lines to the file and restart the gallery (it also notices the change by itself within a minute):

```
sudo -u zorcam-nvr nano /var/lib/zorcam-nvr/config/zorcam.conf
sudo systemctl restart zorcam-nvr-gallery
```

```ini
[gallery]
environment = test
```

The same can be done on the `/config` page.

## Language

The interface, the Settings and `/config` pages, monitoring messages and push notifications are available in English and Russian.

**The language is picked on each device** on the Settings page (the Settings tab in the bottom bar, every role): Auto, English or Russian. The choice is kept on the device itself, in the Home Screen app or the browser (the `zorcam_lang` cookie), not on the Pi: it applies to every page of that device, sign-in included, and to the push notifications that come to that device; other devices of the same user have their own. Auto means the device language (the `Accept-Language` header); if it is neither Russian nor English, English is used.

Devices where no language is picked get the default `[language] language`, the first setting of the setup wizard and of `/config`: `auto` (device language), `en` or `ru`. Devices subscribed to push before translations appeared get Russian notifications until the app is opened on them again.

All interface texts are in `app/common/zorcam_i18n.py`. Settings files (`zorcam.conf`, `secrets.env.example`, `go2rtc.yaml`), install scripts and tests are always in English.

## Users and roles

| Role | Access |
|---|---|
| `liveviewer` | Live only: no recordings or push; Settings: language and sign-out |
| `viewer` | Live, videos, frames, motion push notifications; Settings: language and sign-out |
| `admin` | everything: the same + General settings, cameras, status and users on Settings, `/config`, deleting recordings, failure push notifications; there is one administrator, named `admin` |

The setup wizard creates only `admin`. `admin` adds the other users on Settings → Users → New user: a name (no spaces, up to 32 characters; `admin` is taken; letter case does not matter at sign-in, so `Slava` and `slava` are one name), the role, Viewer (`viewer`: Live, Videos, Timelapse, motion notifications) or Live (`liveviewer`: Live only), a password and the cameras they can see (all by default). For every user the same tab changes the role, the password (empty: keep it; a new password ends that user's sessions on other devices) and the access to each camera, and has Delete the user; `admin` has only the password. Names cannot be changed in the interface. A camera switched off disappears from the user's Live, Videos and Timelapse, its stream and files are closed to them and its motion push notifications do not reach them. A new camera is closed to every user but `admin` until admin switches it on for them on Users; Settings reminds of that after a camera is added. All users are stored in `/var/lib/zorcam-nvr/config/users.json` (mode 600, PBKDF2 password hashes, `cameras` — the cameras a user can see); they can also be changed on the Pi (a user created with the command sees no camera until they are switched on on Users):
```bash
sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_users.py set NAME ROLE   # create, or change the password and role (admin: only the user admin); asks for the password twice
sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_users.py del NAME
sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_users.py list
```
A deleted user loses access immediately. A role changed with the command applies from the next request.

## Monitoring and self-healing

- **Detector (`zorcam-nvr-capture`).** When the camera sends no frames for longer than `stall_timeout` (15 s), ffmpeg is restarted and the detector reconnects. The wait is at least three of the camera's keyframe intervals (at most 60 s), so a camera with a keyframe every 10 s is not taken for a lost one; the first frame of a connection is awaited up to 60 s, since on a loaded Pi ffmpeg takes tens of seconds to start and connect. While a camera keeps failing, the pause before the next attempt doubles, 3, 6, 12 … up to 60 s, and goes back to 3 s after a minute of frames: an unreachable camera does not start a new ffmpeg every few seconds. When the Pi cannot keep up with encoding (more than 3 clips of a camera at the encoder), a new keyframe clip is not saved (the log says `encode queue full`; Settings → Status shows how many in 24 h) instead of waiting for hours with its frames in memory. Settings → Status also checks the detector's memory: when the kernel keeps slowing the detectors down at the service's `MemoryHigh` (`high` in `memory.events`), it is a problem, with a push to admin: there are more cameras than this Pi can handle. ffmpeg has an RTSP network timeout, `rtsp_timeout`. ffmpeg errors go to the log (`journalctl -u zorcam-nvr-capture`) without the camera password. On a reconnect the detector also asks the camera itself (RTSP DESCRIBE) and adds its answer, for example `camera: 401 Unauthorized` or `camera: timed out`. The detector analyses every keyframe in memory and builds clips from them, but saves a frame for the Timelapse to the SD card at most once per the camera's Snapshot interval (5 s by default), to spare the card. A snapshot saved after the detector saw motion since the previous one (or while the pause after a trigger lasts) is named `<date>_<time>_<ms>_m.jpg` instead of `<date>_<time>_<ms>.jpg`: the `_m` mark costs nothing (no extra decoding), works with Save motion videos off, and everything that lists, shows, counts and deletes snapshots treats both names alike; the timelapse shows the mark. A separate thread writes it: a slow card does not delay motion detection and notifications (while it is busy, snapshots are skipped), and a failed write, for example on a full disk, does not drop the camera connection. For the analysis the JPEG is decoded straight at 1/8 size in grey and compared at 160×90, so the camera resolution hardly affects how fast the detector reacts. The detector's ffmpeg connects without stream analysis (`-probesize 32 -analyzeduration 0`) and without decoder frame threads (`-flags low_delay`): otherwise it stays about 1.4 s behind the camera for good. It encodes the JPEGs in one thread (`-threads 1`), so the ffmpeg of each camera takes about 40 MB less. The reaction delay is mostly the interval between keyframes (0.4 s at 2.5 frames/s). The detector runs a separate process for each camera (`daemon.py --camera <id>`, lines in the log start with `[<id>]`), so a camera that hangs does not stop the others; a process that exits or stops responding for 60 s is restarted. The clip's poster is the frame that triggered detection (the second of the two compared frames) and is written at once: until the clip is recorded and glued, Videos shows it with "Preparing video…", and the page turns it into a playable clip within a few seconds once the file is ready. From the same small 160×90 frames the detector finds the area where the picture changed (pixels that differ by 28 or more of 255, lone specks removed; none if the whole picture changed) and writes it, with the moment of the motion in the clip (the first of the two compared keyframes, the last one without the motion, since the box is the difference between them; seconds from its start, to 0.1 s; accurate to about a second or two, since the pieces are named in whole seconds), to a few dozen bytes `<clip>.json` next to the poster; nothing is drawn into the poster file. Videos and the clip page lay a red frame over the poster from it; the clip page lights the "Motion" badge and the frame once playback reaches that moment, and a mark on the scale under the video (tap it to jump there) shows where it is. Clips without a `.json` (made before this) show none of it. The `.json` is deleted with its clip. If the camera connection drops in the middle of a clip, what was recorded is still saved. A clip that a stopped process did not finish is marked as not saved: its poster gets the text "Could not save the video" (the new process does it in the background, while it already detects motion). Each writes its state (last frame, reconnects in 24 h, last error) to `/dev/shm/zorcam_capture_<id>.json`.
- **systemd watchdog.** The detector and the gallery regularly tell systemd they are alive (`WatchdogSec`); the gallery does so only while it answers `/healthz`. systemd restarts a hung process by itself.
- **Memory and CPU limits.** Each service has a limit in its systemd unit (`MemoryMax`: capture 61% of the memory, go2rtc 22%, gallery 250 MB; on a Pi 3 that is the former 560 MB and 200 MB, on a Pi 4/5 the limits grow with the memory; no swap; `CPUQuota`, low `CPUWeight`). A service that goes beyond it is killed and restarted by systemd, while the rest of the Pi and SSH stay reachable. `systemctl status zorcam-nvr-gallery` shows the current memory use. On a Raspberry Pi the kernel boots with `cgroup_disable=memory` by default, and the memory limits do nothing: `install.sh` adds `cgroup_enable=memory` to `/boot/firmware/cmdline.txt` (the old file is `cmdline.txt.bak`); the limits work after a reboot (`sudo reboot`). Clips are encoded one at a time for all cameras, with at most 2 threads and without look-ahead (`-tune zerolatency`, about 120 MB at 1080p) and at the lowest priority (`nice 19`), so the detectors do not lose frames: several encodings at once ran a Pi 3 out of memory. Above `MemoryHigh` (capture: 52% of the memory, 480 MB on a Pi 3) the kernel slows down every process of the service, and the detectors lose frames: `memory.events` in `/sys/fs/cgroup/system.slice/zorcam-nvr-capture.service/` counts it as `high`. The page cache of files a service writes counts toward its memory too, so the detector flushes every snapshot and clip to the card and drops it from the cache: the gallery reads it from the card when it is viewed. The temporary files in `/dev/shm` belong to the system user `zorcam-nvr`, which systemd-logind leaves alone (it removes the files of ordinary users when their last session ends, `RemoveIPC`), so logging out of SSH does not wipe the stream buffers, HLS and thumbnails. If the stream buffer disappears anyway, the detector creates it again.
- **A check every minute (gallery).** It checks for fresh frames from each camera, that `zorcam-nvr-capture` and `zorcam-nvr-go2rtc` are running, free disk space and the CPU temperature. On a problem, devices subscribed by `admin` users get a "⚠ Zorcam: …" push with the reason (for example, `401 Unauthorized` from the camera). If the problem persists, a reminder comes every hour. After recovery a "✓ Zorcam: … back to normal" push comes. Tapping it opens the Settings page.
- **HTTP connections.** The gallery speaks HTTP/1.1 and keeps connections alive (60 s idle), so a page with dozens of thumbnails or a timelapse does not open a connection for every file; through the cloud every new connection is a new channel of the tunnel. An answer without a body length (an error, a redirect) and any answer to a POST close the connection, so an unread request body cannot be taken for the next request. `/healthz` and the systemd watchdog are unchanged; every connection has its own thread.
- **Cleanup.** Every 10 minutes the gallery deletes frames and clips older than each camera's limits (Keep videos, days and Keep snapshots, days in the camera settings); the folders of deleted cameras by the defaults. If free space still drops below `[health] min_free_disk_mb` (1024 MB by default), the gallery at once deletes the oldest snapshots and clips of all cameras (a clip with its poster) until 512 MB more than that is free, and sends the admins a push (once a day at most); every such cleanup is listed in Settings → Status.

## Motion push notifications

When the detector sees motion, the gallery sends subscribed devices of `viewer` and `admin` users a "Motion: <camera name>, HH:MM:SS" notification (in the user's language, see Language). Tapping it opens the Timelapse of that camera paused at the moment of the motion (at the first snapshot with motion at or after it; if that day's recordings are already deleted, today with a note). The "Video" links over the Timelapse strip open the clip in a window over the player. Notifications come at most once a minute for each camera. Each camera has its own Motion notifications switch (Settings → Change): when it is off, motion on that camera sends no push.

They are turned on with the bell button in the header, separately on each device. A test notification arrives right after turning them on. Tapping the bell again turns them off on that device.

Each user (`viewer` and `admin`) chooses what to be notified about in Settings → Alerts: the general All cameras switch and a switch for each camera open to the user. The switches work at once, there is no Save button, and the choice applies to the user's account on every device. Turning a camera on there also turns on its Motion notifications switch in the camera settings (for every user, if it was off); turning it off affects only the user's own account.

- **iPhone.** iOS 16.4+ is required, and notifications work only in the Home Screen app.
- **HTTPS.** The gallery must be opened over HTTPS (for example, via your router's dynamic DNS domain with a certificate). Over `http://` the browser does not allow subscribing. Add the Home Screen app from the HTTPS address.
- **How it works.** `zorcam-nvr-capture` reports motion to the gallery with `POST http://127.0.0.1/internal/motion`; requests not from localhost are rejected. The gallery encrypts the notification and sends it to the browser's push service (Apple, Google, Mozilla) using the Web Push standard with VAPID. The code is in `app/gallery/push.py`, with `python3-cryptography` as the only dependency.
- **Files.** The VAPID key and subscriptions are stored in `/var/lib/zorcam-nvr/config/vapid_private.pem` and `push_subscriptions.json` (mode 600). If the key is deleted, every device has to subscribe again.

## Zorcam cloud

Optional: open the Pi from anywhere without port forwarding or a public IP, all your Pis in one app, sign-in with Yandex ID, access for the family. Without the cloud Zorcam works as before: recording, the gallery and push do not need it. Recordings and snapshots stay on the Pi: the cloud does not store them and never gets the camera or gallery passwords. Live through the cloud goes by WebRTC without port 8555 forwarded: the cloud's TURN server relays the video (encrypted end to end, DTLS-SRTP), with temporary logins the cloud gives the gallery. The cloud runs in Russia, free at first.

1. **Connect.** Settings → General → Zorcam cloud → the cloud address → Connect. The gallery shows a code valid for 10 minutes.
2. **Enter the code.** In the cloud app (`https://cloud.<cloud domain>`, sign in with Yandex ID) → Connect a Pi → the code. In a few seconds the Pi is online: the gallery opens at `https://<id>.<cloud domain>` as the same gallery user, without its password (the owner signs in as `admin`).
3. **Invite.** In the app: Devices → the Pi → Invite → a gallery user name (the role and cameras are set as usual, Settings → Users). The link is valid 7 days, for one Yandex ID sign-in.

How it works: after pairing the `zorcam-nvr-cloud` service (user `zorcam-nvr`) keeps an frpc tunnel to the cloud over `wss` on port 443 (the Pi connects out, no router ports) and reports once a minute that it is up. The cloud confirms every request with a signed 60-second token (`X-Zorcam-Identity`, the cloud key comes with the pairing); the gallery takes it only from the tunnel (127.0.0.1) and then applies that user's role and cameras from `users.json`. About motion the cloud learns only the camera and the time, no snapshot. State: `/var/lib/zorcam-nvr/config/cloud.json` (mode 600). Unlink with "Disconnect from the cloud" in the gallery or in the app. Log: `journalctl -u zorcam-nvr-cloud`.

**Timelapse.** The Timelapse tab (called Snapshots before) plays the snapshots (at home and through the cloud alike; there is no grid of snapshots any more, and the old links of the grid, with `mode=`, `horizon=` or `fpage=`, open the same tab). It plays the snapshots of Today (since midnight; any other day is in the calendar below; there is no Hour / Today switch) as a video at 15 frames a second, with pause, a position slider and the time of the frame in the Pi's time zone (for example `10:53:28 11.10.2026 MSK`; a zone without a letter abbreviation is shown as `UTC+03:00`), at the bottom left over the picture. The speed buttons are real-time multipliers: the default plays every snapshot, so it is faster than life by the snapshot interval times 15 frames a second (at a 5 s interval, ×75: 75 seconds of life in a second), and the other four are a quarter, a half, twice and four times that (×19, ×38, ×75, ×150, ×300; from 1000 on the number is written `1.5k`; the button's hint reads `75× real time`). The interval is taken from the loaded frames (the median gap between neighbouring snapshots), and the default is chosen again whenever a period or a day is loaded. The slower buttons show every frame at a lower frame rate (a quarter and a half of 15 a second), the faster ones skip every second or fourth frame; with fewer than two snapshots the interval is unknown and the buttons show the plain ×0.25 … ×4. Under the slider a strip marks the frames saved after motion (the `_m` snapshots) in red; a tap on the strip jumps there, and when the motion has a saved video, a "Video hh:mm:ss" link to it appears over the mark (`/timelapse` also returns `videos`, the saved clips of the period, and `clip`, the clip length). Below the orange marks the strip has day marks (a taller line and the date `dd.mm`) and hour marks (a short line, and the hour where it fits; the step of 1, 2, 3, 6 or 12 hours follows the width of the screen). A pause in the recording longer than three snapshot intervals and a minute (the camera was switched off or did not answer) takes its real length on the strip and the slider and is shown as a gap: the bar is cut out, with a line at each edge, and the hours inside it stand where they are in time; a tap or a slide into the gap goes to its nearer edge. No frames are dropped: the player gets every snapshot of the period, so a day at a short Snapshot interval is a long video (a day at the default 5 s is about 17 000 frames, 19 min at x1) and a list of several hundred KB. The Pi sends only the list of file names, compactly (the time and the motion mark the player reads from the names), gzip-compressed when the browser asks for it (about 10 times smaller), and keeps at most 6 ready answers for 20 s; On a computer the keyboard works while the timelapse is on screen: Space plays or pauses, the left and right arrows step one frame back or forward (with Shift, 10 frames), Home and End go to the first and the last frame. Under the player is a calendar: a row of days, the oldest at the left and today at the right end, with the month above its first day. Tap a day and its snapshots are loaded and played (the choice is remembered on the device). Under the number each day has two fixed marks: on the left a stack of frames with a play triangle over it (the day has a timelapse, that is snapshots; the text colour), on the right a camera (the day has motion videos; the text colour). A mark that is not true stays as a faint outline, so the cells line up; a day without snapshots is dimmed and cannot be tapped, a videos-only day too. Videos that failed to save are not counted. The `[` and `]` keys go to the previous and the next day with snapshots. The phone loads the frames a few at a time ahead of the playback as 320×180 thumbnails and lets the played ones go, so its memory stays small. The list comes from `/timelapse/<camera id>?period=hour|today` (the page asks for `today`) (or `?day=YYYY-MM-DD` for one calendar day of the Pi's time, 00:00:00–23:59:59; the calendar comes from `?days=1`: every day from the oldest with snapshots or videos up to today, at most 400, with the number of snapshots and videos of each, counted from file names only and cached for 20 s like the frame lists) (viewer and admin, only the cameras the user sees); the folder is read by file names, no file is opened (a day at a short Snapshot interval is thousands of names, so keep Keep snapshots, days as long as you want to look back).

Once an hour a connected Pi also sends the cloud a health report for the cloud's admin page: the version, uptime, load, free memory and disk, CPU temperature, the state and memory counters of the Zorcam services, for each camera its ID, the age of the last frame and the reconnects in 24 h, and the results of Settings → Status (texts of the problems only). Camera names, addresses, passwords, recordings and snapshots are not in it. The cloud admin can also ask for the same report right away (Request the state on the admin page): the cloud asks the gallery through the tunnel on behalf of the owner. The cloud keeps the reports 7 days.

## Yandex Smart Home

Every camera can become a motion sensor in the Yandex app (Alice) for scenarios: the speaker says someone is at the door, the light turns on. No video goes to Yandex, only the camera name and "motion" / "no motion". Zorcam works as a private smart home skill that Yandex calls through the [Zorcam cloud](#zorcam-cloud): the skill needs the Pi connected to the cloud (Settings → General → Zorcam cloud), at its address `https://<id>.<cloud domain>`. Without the cloud the section in Settings only explains this, and the skill addresses on the Pi do not answer. If you disconnect the cloud later, the skill keys and the link are kept and work again once it is connected; notifications are not sent meanwhile.

1. **Skill.** On [dialogs.yandex.ru/developer/smart-home](https://dialogs.yandex.ru/developer/smart-home) create a smart home skill, access: private. The values for Backend and Account linking are on Settings → General → Yandex Smart Home (the page shows the addresses ready, for your Pi's cloud address): Endpoint URL `https://<id>.<cloud domain>/yandex`, application ID `zorcam`, authorization URL `https://<id>.<cloud domain>/yandex/auth`, token and token refresh URL `https://<id>.<cloud domain>/yandex/token`. Make up an application secret. Save and press Publish: notifications work only for a published skill, not a draft.
2. **Keys.** On the same Zorcam page enter the application secret, the skill ID (the skill's General settings) and the OAuth token for notifications: sign in with the account that owns the skill and open the link on the page, Yandex shows the token.
3. **Link.** In the Yandex app: + → Smart home device → your skill → Link to Yandex, sign in as `admin`. The cameras appear as motion sensors.
4. **Scenario.** Scenarios → + → When: device → a camera → Motion; Then: speaker → say a phrase.

The sensor reports "motion" once per event and "no motion" after a minute of a quiet detector, regardless of the camera's push switch. Unlink in the Yandex app or with the button on the Zorcam page (the page opens at home and through the cloud, as admin). Wrong passwords and secrets on the skill addresses are counted and blocked per client address, which the cloud passes to the Pi. The tokens given to Yandex are stored as hashes in `/var/lib/zorcam-nvr/config/yandex.json` (mode 600) with the skill secret and token. Code: `app/gallery/yandex.py`; what was sent and what Yandex answered: `journalctl -u zorcam-nvr-gallery | grep yandex`.

## Layout

```
config/secrets.env.example     secrets template (camera passwords)
app/capture/daemon.py          persistent capture, detection, clip building (systemd: zorcam-nvr-capture)
app/gallery/app.py             web gallery with videos and Live (systemd: zorcam-nvr-gallery)
app/gallery/push.py            Web Push sending (VAPID, aes128gcm encryption)
app/gallery/yandex.py          Yandex Smart Home: skill, motion sensors
app/cloud/daemon.py            Zorcam cloud: pairing, frpc tunnel (systemd: zorcam-nvr-cloud)
app/common/zorcam_cloud.py      Zorcam cloud: keys, signatures, sign-in through the cloud, events
app/gallery/templates/         page frames (HTML, string.Template)
app/gallery/static/            styles (CSS), scripts (JS) and app icons for the Home Screen
app/gallery/zorcam_html.py     markup that always escapes (text and attribute values)
app/go2rtc/go2rtc.yaml         go2rtc config: RTSP -> WebRTC for Live (systemd: zorcam-nvr-go2rtc)
app/common/zorcam_config.py     zorcam.conf and cameras.json: reading, checking, writing; RTSP URLs for go2rtc
app/common/zorcam_i18n.py       interface texts in English and Russian
app/gallery/zorcam_users.py     users and roles (users.json)
app/install.sh                 install / update on the Pi
app/uninstall.sh               removal of Zorcam from the Pi
app/debug.sh                   extended debugging on/off, collecting logs
app/diagnose.py                what the logs say: voltage drops, throttling, SD card, memory
config/zorcam.conf.example      every parameter with its description and default
tests/                         tests: settings, users, push, gallery by role and language (pytest)
CHANGELOG.md                   change history by version
```

## Hardware

Zorcam runs on a Raspberry Pi 3 with one camera, but a Pi freezes most often because of the power supply or the SD card, not the program. What to use:

| Part | Recommended | Why |
|---|---|---|
| Raspberry Pi | Pi 4 (2 GB or more) or Pi 5; the minimum is a Pi 3 Model B/B+ (1 GB) | a Pi 3 handles one 1080p camera, a Pi 4/5 has spare memory and CPU, USB 3 and a faster SD card slot. Less than 1 GB (Pi Zero 2 W, 512 MB) is not supported: at most 1-2 cameras, `install.sh` warns. The memory limits of the detector and go2rtc are percents of RAM |
| System | Debian 13 (Raspberry Pi OS Lite, 64-bit), no desktop | the desktop takes memory the services need |
| Power supply | the official Raspberry Pi supply: 5.1 V 2.5 A micro-USB for a Pi 3, 5.1 V 3 A USB-C for a Pi 4, 5.1 V 5 A (27 W) USB-C for a Pi 5 | a phone charger or a long thin cable lets the voltage drop under load: the Pi throttles, the SD card stalls and the whole Pi freezes, SSH included |
| SD card | a High Endurance card of 32–64 GB, A1/A2, U3/V30 (for example, SanDisk High/Max Endurance, Samsung PRO Endurance) | the detector writes frames and clips all day long; ordinary cards wear out and stall. On a Pi 4/5 (and a Pi 3B+) booting from a USB SSD is even better |
| Cooling | heatsinks, a case with vents, out of direct sun | above 80 °C the CPU slows down (throttling) |
| Network | Ethernet for the Pi and the camera | the stream is ~3 Mbit/s all the time; the 2.4 GHz Wi-Fi of a Pi 3 is slower and less stable |
| UPS (optional) | a UPS HAT or a small UPS for the Pi and the camera | a power cut during a write can damage the file system on the SD card |

How to check the Pi after installation:

```bash
vcgencmd get_throttled   # 0x0: no voltage drops and no throttling since boot
vcgencmd measure_temp    # under 70 °C in normal work
```

Anything other than `0x0` means the power supply or its cable is too weak (`0x50000`: the voltage dropped and the CPU was throttled). Diagnosing freezes is described in Debugging.

## Installation

Run every command on the Pi as a user with `sudo`, e.g. `admin`, from any folder: the commands use full paths. The services themselves run as their own system user `zorcam-nvr` (no password, no login, no `sudo`): the program is in `/opt/zorcam-nvr` (read-only for it), the settings in `/var/lib/zorcam-nvr/config` (only `zorcam-nvr` can read them) and the recordings in `/var/lib/zorcam-nvr/recordings` (`install.sh` adds your user to the `zorcam-nvr` group to read them). You need a Raspberry Pi with Debian 13 (Raspberry Pi OS), `sudo` rights, internet access and an IP camera with an H.264 RTSP stream.

### Step 1. Access to the repository

The repository is public: nothing to set up, go on to step 2.

### Step 2. Download the program

```bash
sudo apt update && sudo apt install -y git      # Raspberry Pi OS Lite comes without git
git clone https://github.com/davydovv-creator/zorcam-nvr-community.git ~/zorcam-nvr
```

### Step 3. Install

```bash
~/zorcam-nvr/app/install.sh
```

The script installs the packages (`ffmpeg`, `python3-pil`, `python3-cryptography`, `curl`) and go2rtc, creates the system user `zorcam-nvr` and its folders in `/var/lib/zorcam-nvr`, puts the program into `/opt/zorcam-nvr`, creates `/var/lib/zorcam-nvr/config/zorcam.conf` with default values, installs and starts the `zorcam-nvr-capture`, `zorcam-nvr-gallery` and `zorcam-nvr-go2rtc` services and, with ufw active, opens port 8555. It asks nothing and ends with "Done." and the address to open.

**Time zone.** Recordings and snapshots are named by the Pi's clock in the Pi's time zone, and the gallery and the timelapse show that time; Settings → Status shows the zone (for example `Europe/Moscow (UTC+03:00)`). A fresh Raspberry Pi OS is often in UTC. To set Moscow time:

```bash
sudo timedatectl set-timezone Europe/Moscow
sudo systemctl restart zorcam-nvr-capture zorcam-nvr-gallery
```

The restart is needed: a running service keeps the zone it started with (until then Status shows the new zone in red). Recordings made before the change keep their names.

### Step 4. Setup in the browser

In a browser on the home network open `http://<Pi IP>/` (`hostname -I` shows the IP). While there are no users, the gallery opens the setup wizard; it works only from the home network, so nobody on the internet can take it over first.

1. **General settings.** Language. The other settings are under "Other settings"; their defaults suit most installations. For Live from the internet forward port 8555 (TCP and UDP) to the Pi on the router (see Live stream).
2. **Administrator.** The `admin` password. Other users are added later on Settings → Users. After this step you are signed in as `admin`.
3. **Cameras.** "Ready to add a camera?" Yes: the camera ID (Latin letters, digits, `-` and `_`: the recordings folders `/var/lib/zorcam-nvr/recordings/<id>` and `<id>_videos`), its name (shown above its Live player, on its tab and in notifications), the RTSP address from the camera settings without login and password, the login (`admin` by default), the password (empty: the camera is used without authentication) and the recording settings. The form shows the recommended camera settings (see Camera setup) and checks that the camera answers. Then add another camera or finish. Cameras can also be added later: Settings → Add a camera.

### Step 5. Open the gallery

After the wizard the gallery opens on Live: every camera should show LIVE (WebRTC); LIVE · HLS means WebRTC did not connect. `~/zorcam-nvr/app/install.sh` run again checks every camera: "camera <id> connects, codec H.264" (or "codec H.265": Live of such a camera shows on iPhone and iPad in any browser and in Safari on Mac; if Chrome or Firefox on Android or a computer shows no picture, switch it to H.264, see Camera setup).

From the internet: forward port 8555 (TCP and UDP) to the Pi on the router for Live, and open the gallery at an HTTPS address (for example, your router's dynamic DNS domain with a certificate); push notifications need HTTPS. Do not forward port 80 itself: the gallery serves plain HTTP, the password and the session would cross the internet unencrypted; the simplest safe way from outside is the Zorcam cloud. On an iPhone add the gallery to the Home Screen: in Safari tap the app icon in the header and follow the steps (see iPhone app and sign-in).

### What install.sh does

Repository folders go here:

| Repository | On the device |
|---|---|
| `app/capture/` | `/opt/zorcam-nvr/capture/` |
| `app/gallery/` | `/opt/zorcam-nvr/gallery/` |
| `app/go2rtc/` | `/opt/zorcam-nvr/go2rtc/` |
| `app/common/*.py` | into both `/opt/zorcam-nvr/capture/` and `/opt/zorcam-nvr/gallery/` |
| systemd units (`zorcam-nvr-go2rtc-config.path` restarts go2rtc when `zorcam.conf`, `cameras.json` or `secrets.env` changes) | `/etc/systemd/system/` |
| go2rtc | `/usr/local/bin/go2rtc` |

Zorcam 0.1.5 and later is installed on a clean Pi. An installation under the previous name zorcam (user `zorcam`, `/opt/zorcam`, `/var/lib/zorcam`, `zorcam-*` services) is renamed once. The user becomes `zorcam-nvr`, the settings and recordings move to `/var/lib/zorcam-nvr`, the old services and program folder are removed; recordings, users, push subscriptions and the cloud link are kept. The clone can stay where it is or be moved: `mv ~/zorcam ~/zorcam-nvr`. Running the script again is safe: only what changed is updated and only the affected services are restarted. The gallery listens on port 80 through `AmbientCapabilities=CAP_NET_BIND_SERVICE` while running as the user `zorcam-nvr` (`NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome=yes`: it cannot gain rights, change the system or read the home folders). The gallery itself deletes old frames and clips by each camera's retention in `cameras.json`.

## Storage

| Data | Kept for |
|---|---|
| Snapshots | 10 days (`frames_days` of the camera) |
| Videos | 5 days (`videos_days` of the camera) |

Each camera has its own settings (Settings → the camera → Change): Save motion videos (off: motion is still detected and notified, no clips), Video length, seconds (2 to 120, default 10), Keep videos, days (1 to 365), Save snapshots (off: the Timelapse tab of the camera stays empty), Snapshot interval, seconds (1 to 3600, default 5) and Keep snapshots, days (1 to 365). The video fields are active only while Save motion videos is on, the snapshot fields only while Save snapshots is on; the values of inactive fields stay. A snapshot every 5 seconds takes about 1 to 2 GB a day per camera.

Motion clips are never re-encoded on the Pi. A separate ffmpeg keeps cutting the camera video from go2rtc into 2-second pieces without re-encoding, in memory (`/dev/shm/zorcam_rec_<id>`, the SD card is not worn); on motion the pieces from 3 s before it to the end of the clip are glued into the `.mp4`, again without re-encoding. The clip has the camera's full frame rate and resolution and takes almost no CPU, but its files are several times larger (the camera bitrate: at 4 Mbit/s about 5 MB per 10 s), so shorten Keep videos, days or lower the bitrate on the camera. The pieces kept in memory (the clip length plus about 30 s, about 20 MB at 4 Mbit/s) count toward the memory limit of `zorcam-nvr-capture`. Better H.264: an H.265 clip and Live play on iPhone, iPad and in Safari on Mac, but not in every browser on Android and computers. If the pieces are missing (the camera or go2rtc was unavailable at that moment), the clip is not saved: Videos shows only its poster with the text "Could not save the video" (nothing is encoded on the Pi).

**Recordings on a USB disk.** Settings → General → Recordings disk (admin): the USB flash drives and disks plugged into the Pi (never the SD card or a disk the Pi boots from). *Format … and record to it* erases the disk and formats it (ext4); *Connect … without formatting* takes a disk already in ext4 (for example, one used with Zorcam before) as it is. The recordings on the SD card move to the disk, then snapshots and clips are written there: the SD card no longer wears out from them. The service `zorcam-nvr-storage` (root) does it: at boot before the detector, and every minute. If the disk is gone (unplugged, failed), recording goes on to the SD card and Status shows the problem *Recordings disk* (push to admin); when the disk is back, it is connected again and what was recorded meanwhile moves to it. *Disconnect the disk* stops recording to it (what is on it stays) so that it can be unplugged. A USB SSD is the most reliable; a 2.5" hard disk needs its own power or a powered hub (the Pi 3 USB ports give about 1.2 A together). The retention days of a camera can be raised: the cleanup by free space looks at the disk the recordings are on.

A user with the `admin` role can delete recordings by hand, for each camera separately: Settings → Cameras → Recordings, under the name of the camera. Delete under Timelapse clears the Timelapse of that camera and keeps its videos; Delete under Videos deletes its motion clips; Delete all below them deletes both. The app asks for confirmation first and, after deleting, simply shows the same Settings tab again (there is no page with the result; the counters of the camera are updated).

## Camera setup

**Finding cameras (ONVIF).** In the form of a new camera, Find cameras in the network (ONVIF) sends an ONVIF search (WS-Discovery) to the home network and lists the cameras that answered, with their address and model; the ones already added are marked. Enter the login and password of a camera and tap Add: Zorcam reads its video profiles and fills in the form, the RTSP address with the largest picture and the Additional stream for the detector: the camera's next stream after it that is smaller (the third stream of many cameras is too small or MJPEG). Check the fields and save; the password entered for ONVIF is used for the camera and is not shown in the page. ONVIF must be on in the camera (some cameras keep it off or want a separate ONVIF user, e.g. the camera account of Tapo); only home network addresses are queried. Cameras in another subnet behind the router (e.g. 192.168.2.x, while the Pi is in 192.168.1.x) do not hear this search: the router does not pass it on. Enter that subnet under Search, field Another subnet (e.g. `192.168.2.0`: without a mask it means the whole 192.168.2.x; home networks only, up to 254 addresses) and tap Search again: the Pi asks each of its addresses directly; the router must let UDP port 3702 through between the subnets. The subnet is kept on the device (a cookie), an empty field searches the network of the Pi only. No camera found: enter it manually.

In the camera's web interface or app, set up the stream that Zorcam uses (its RTSP address in the camera form); the camera form in the gallery shows the same recommendations:

| Setting | Value |
|---|---|
| Codec | H.264 (H.265 if you watch on iPhone, iPad or in Safari on Mac) |
| Frame rate | 25 fps |
| I-frame (keyframe) interval | 10 frames |
| Resolution | 1080p (1920×1080) or lower |

**It is strongly recommended to set the camera resolution to 1080p or lower, and to set it on the camera itself, not in Zorcam.** Zorcam takes the stream as the camera sends it: Live, the motion detector and the clips get the full resolution. If the camera's main stream is larger than 1080p, give Zorcam the RTSP address of its second stream (sub stream) at 1080p or lower.

**Second stream for the detector.** Most cameras also send a second, low-resolution stream (sub stream, e.g. 640×360). Enter its RTSP address in the camera form, field "Additional stream" (right below the RTSP address) (the same login and password). The motion detector, snapshots and clip posters then use it, while Live and motion clips keep the main stream in full quality. Decoding a 640×360 keyframe is many times cheaper than 1080p, so a Pi 3 handles 4 to 5 cameras instead of 2 to 3. Set the sub stream in the camera to H.264, about 640×360, with a keyframe every second. If it gives no frames 3 connections in a row, the detector uses the main stream for 30 minutes and then tries again; Settings → Status shows which stream it reads. go2rtc then keeps two connections to the camera, one per stream; most cameras allow that. Usual addresses (check them in the camera):

| Camera | Main stream | Second stream |
|---|---|---|
| Hikvision | `rtsp://IP:554/Streaming/Channels/101` | `rtsp://IP:554/Streaming/Channels/102` |
| Dahua, Amcrest | `rtsp://IP:554/cam/realmonitor?channel=1&subtype=0` | `rtsp://IP:554/cam/realmonitor?channel=1&subtype=1` |
| TP-Link Tapo | `rtsp://IP:554/stream1` | `rtsp://IP:554/stream2` |
| Reolink | `rtsp://IP:554/h264Preview_01_main` | `rtsp://IP:554/h264Preview_01_sub` |
| Others | `…/MAIN`, `…/main` | often `…/SUB`, `…/sub`; ONVIF Device Manager shows both |

Why exactly these values:
- **H.264.** Every browser shows it over WebRTC; H.265 every browser on iPhone and iPad and Safari on Mac. The Raspberry Pi 3 has no hardware H.265 decoder, so make the detector's second stream H.264 for sure.
- **25 fps.** Live shows every frame, so the stream stays smooth.
- **1080p or lower.** The Pi 3 has 1 GB of memory and no hardware decoder: the CPU decodes every keyframe of every camera, and encoding a 1080p clip takes about 120 MB of memory. With a 2K or 4K stream the detector falls behind, and clips take long to encode and hit the memory limit of the service.
- **I-frame every 10 frames.** The detector decodes keyframes only, so it gets 25 / 10 = 2.5 frames per second: the more of them, the sooner it notices motion (the Timelapse keeps one of them per the camera's Snapshot interval). There is nothing to set in Zorcam: the detector measures how many keyframes arrive (shown on Settings → Status). With several cameras keep the keyframe rate at 5 or below: the Pi 3 decodes every keyframe of every camera.

## Debugging

When the Pi freezes or something fails and the cause is not clear, turn on extended debugging:

```bash
~/zorcam-nvr/app/debug.sh on       # turn on (survives reboots)
~/zorcam-nvr/app/debug.sh status   # what is on now
~/zorcam-nvr/app/debug.sh save     # collect logs and system state into ~/zorcam-nvr-debug-<date>.tar.gz
~/zorcam-nvr/app/debug.sh off      # back to production
```

`on` adds:
- a monitor (`zorcam-nvr-debug-monitor`) that every 30 s writes to the journal the power state (`throttled`), temperature, CPU clock, load, free memory, disk (SD card) pressure, the memory of each Zorcam service and the processes stuck on the disk; see it with `journalctl -t zorcam-nvr-debug -f`;
- the journal on disk (up to 200 MB), so the lines before a freeze or a hard reset survive the reboot (`journalctl -b -1`);
- kernel warnings about processes stuck on the disk after 30 s instead of 120 s;
- in Zorcam: every gallery request with its status and time, detector statistics every minute and slow SD card writes, ffmpeg warnings, the go2rtc debug log.

To see what the logs say, run the diagnostics. It reads the logs (and the monitor lines, if debugging was on) and reports voltage drops, CPU throttling, SD card errors and stalls, unclean shutdowns, memory shortage, service restarts and camera errors, with what to do about each and the most likely cause:

```bash
python3 ~/zorcam-nvr/app/diagnose.py                            # this Pi, logs of the last 7 days (--days N)
python3 ~/zorcam-nvr/app/diagnose.py ~/zorcam-nvr-debug-<date>.tar.gz  # an archive made by save
```

Reproduce the problem (after a freeze, reboot the Pi), run `save` and send the archive. It has no passwords: `secrets.env` is not included and passwords in URLs are masked. `off` removes everything `on` added, restarts the services without debugging and keeps the journal in memory only again (the logs saved on disk are deleted, the `~/zorcam-nvr-debug-*.tar.gz` archives are kept). Debugging writes more to the SD card, so turn it off when the cause is found.

## Uninstall

```bash
cd ~ && ~/zorcam-nvr/app/uninstall.sh
```

Run it from the home folder (`cd ~`): the terminal cannot stay in `~/zorcam-nvr` once the script deletes it.

The script shows what it found and asks before each part:
1. services (`zorcam-nvr-capture`, `zorcam-nvr-gallery`, `zorcam-nvr-go2rtc` and the config watcher), program folders `/opt/zorcam-nvr/capture`, `/opt/zorcam-nvr/gallery`, `/opt/zorcam-nvr/go2rtc`, `/usr/local/bin/go2rtc`, temporary files in `/dev/shm`, ufw rules for port 8555;
2. recordings in `/var/lib/zorcam-nvr/recordings`: they cannot be restored;
3. settings in `/var/lib/zorcam-nvr/config`: `zorcam.conf`, the cameras and their passwords, users, their languages and push keys;
4. the repository clone `~/zorcam-nvr`.

When both the recordings and the settings are deleted, `/var/lib/zorcam-nvr` and the user `zorcam-nvr` go too.

The GitHub SSH key `~/.ssh/zorcam_deploy` (installation step 1) is kept, so the repository can be cloned again right away.

Anything you answer "no" to stays in place, so you can, for example, reinstall and keep the recordings and settings. `uninstall.sh --yes` removes everything without questions. Packages (`ffmpeg`, `python3-pil`, `python3-cryptography`, `curl`) are kept. On the iPhone, delete the Zorcam icon from the Home Screen.
