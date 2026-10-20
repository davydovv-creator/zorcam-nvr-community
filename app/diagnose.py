#!/usr/bin/env python3
"""Diagnose Zorcam problems on the Pi from its logs: voltage drops, CPU throttling, SD card stalls and errors,
memory shortage, service restarts, camera errors, unclean shutdowns.

    python3 ~/zorcam-nvr/app/diagnose.py                      # this Pi, logs of the last 7 days
    python3 ~/zorcam-nvr/app/diagnose.py --days 2
    python3 ~/zorcam-nvr/app/diagnose.py ~/zorcam-nvr-debug-*.tar.gz  # an archive made by debug.sh save

Only reads: it changes nothing. The more logs there are, the more it finds: with "debug.sh on" the journal is kept
on disk (the previous boots survive a freeze) and a monitor line is logged every 30 s.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
from collections import Counter
from datetime import datetime

STATE_DIR = os.environ.get("ZORCAM_STATE_DIR", "/var/lib/zorcam-nvr")  # Zorcam settings and recordings

OK, WARN, BAD, INFO = "OK", "WARN", "PROBLEM", "INFO"
SERVICES = ("zorcam-nvr-capture", "zorcam-nvr-gallery", "zorcam-nvr-go2rtc")

# vcgencmd get_throttled bits: https://www.raspberrypi.com/documentation/computers/os.html#get_throttled
THROTTLED_BITS = [
    (0x1, "under-voltage now"), (0x2, "ARM frequency capped now"), (0x4, "throttled now"),
    (0x8, "soft temperature limit now"), (0x10000, "under-voltage has occurred"),
    (0x20000, "ARM frequency capping has occurred"), (0x40000, "throttling has occurred"),
    (0x80000, "soft temperature limit has occurred"),
]

KERNEL_PATTERNS = {
    "undervoltage": r"[Uu]ndervoltage detected|[Uu]nder-voltage detected",
    "sd_error": r"mmc\d+: .*(timeout|error|CRC|failed|reset)|mmcblk\d.*(I/O error|error -\d+)"
                r"|Buffer I/O error on dev mmcblk|blk_update_request: .*mmcblk|critical medium error",
    "fs_error": r"EXT4-fs (error|warning)|Remounting filesystem read-only|remount.*read-only|JBD2: .*error",
    "hung_task": r"blocked for more than \d+ seconds|hung_task",
    # the trace of a hung task: the card detection waiting for the card (busy with a long write), not a failure
    "card_detect": r"Workqueue: \S+ mmc_rescan",
    "unclean": r"orphan cleanup on readonly fs|recovery complete|needs_recovery",
    "oom": r"Out of memory|oom-kill|Killed process|oom_reaper|Memory cgroup out of memory",
    "thermal": r"(cpu|thermal).*(throttl|critical temperature)|Temperature above threshold",
}
SERVICE_PATTERNS = {
    "restart": r"Scheduled restart job",
    "planned": r", restarting$",  # a service restarts itself to apply new settings: not a problem
    "watchdog": r"[Ww]atchdog timeout",
    "killed": r"Main process exited, code=(killed|dumped)|Failed with result '(oom-kill|signal|watchdog|core-dump)'",
    "camera": r"camera: (.+)|401 Unauthorized|Connection refused|timed out",
    "reconnect": r"reconnecting|no frames for",
    "slow_write": r"debug: slow write",
    "clip_error": r"stream clip error",
    "stun": r"stun: external address",  # the gallery logs it only when the result changes: the last line is the state
}
CGROUP = "/sys/fs/cgroup/system.slice/{}.service/memory.events"
CGROUP_STAT = "/sys/fs/cgroup/system.slice/{}.service/memory.stat"
GALLERY_ANON_WARN_MB = 120  # MemoryHigh of the gallery is 200 MB: the process itself rarely needs half of it


def line_time(line):
    """Time of a journal line (journalctl -o short-iso: the first word), or None."""
    try:
        return datetime.fromisoformat(line.split(" ", 1)[0])
    except ValueError:
        return None


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- sources
def live_sources(days):
    since = f"-{days} days"
    kernel = run(["journalctl", "_TRANSPORT=kernel", "--since", since, "-o", "short-iso", "--no-pager", "-q"])
    if not kernel.strip():
        kernel = run(["dmesg", "-T"])
    services = run(["journalctl", "--since", since, "-o", "short-iso", "--no-pager", "-q"]
                   + [a for s in SERVICES for a in ("-u", s)])
    monitor = run(["journalctl", "-t", "zorcam-nvr-debug", "--since", since, "-o", "short-iso", "--no-pager", "-q"])
    boots = run(["journalctl", "--list-boots", "--no-pager", "-q"])
    return {
        "kernel": kernel.splitlines(),
        "services": services.splitlines(),
        "monitor": monitor.splitlines(),
        "throttled": run(["vcgencmd", "get_throttled"]).strip(),
        "boots": len([b for b in boots.splitlines() if b.strip()]),
        "persistent": os.path.isdir("/var/log/journal"),
        "disk": shutil.disk_usage(STATE_DIR if os.path.isdir(STATE_DIR) else "/"),
        "memory_events": {s: memory_events(CGROUP.format(s)) for s in SERVICES},
        "memory_stat": {"zorcam-nvr-gallery": memory_events(CGROUP_STAT.format("zorcam-nvr-gallery"))},
        "origin": f"this Pi, last {days} days",
    }


def memory_events(path):
    """memory.events (or memory.stat) of a service's cgroup as {name: count}, {} if not readable."""
    try:
        with open(path) as f:
            return {k: int(v) for k, v in (line.split() for line in f if line.strip())}
    except (OSError, ValueError):
        return {}


def archive_sources(path):
    texts = {}
    with tarfile.open(path) as tar:
        for m in tar.getmembers():
            if m.isfile() and m.name.endswith(".txt"):
                texts[os.path.basename(m.name)] = tar.extractfile(m).read().decode(errors="replace")
    journal = (texts.get("journal-previous-boot.txt", "") + "\n" + texts.get("journal-this-boot.txt", "")).splitlines()
    kernel = [line for line in journal if " kernel: " in line] + texts.get("dmesg.txt", "").splitlines()
    services = [line for line in journal if any(s in line for s in SERVICES) or "daemon.py" in line or "app.py" in line]
    throttled = re.search(r"throttled=0x[0-9a-fA-F]+", texts.get("system.txt", ""))
    boots = [b for b in texts.get("boots.txt", "").splitlines() if re.match(r"\s*-?\d+\s", b)]
    return {
        "kernel": kernel, "services": services, "monitor": texts.get("monitor.txt", "").splitlines(),
        "throttled": throttled.group(0) if throttled else "",
        "boots": len(boots), "persistent": bool(texts.get("journal-previous-boot.txt", "").strip()
                                                and "No journal files" not in texts["journal-previous-boot.txt"]),
        "disk": None, "memory_events": {}, "origin": os.path.basename(path),
    }


# ---------------------------------------------------------------- analysis
def matches(lines, pattern):
    rx = re.compile(pattern)
    return [line for line in lines if rx.search(line)]


def examples(lines, n=3):
    """The first and the last lines: when it started and when it was seen last."""
    picked = lines[:1] + lines[-(n - 1):] if len(lines) > n else lines
    return ["    " + line.strip()[:160] for line in dict.fromkeys(picked)]


def decode_throttled(text):
    m = re.search(r"0x([0-9a-fA-F]+)", text or "")
    if not m:
        return None, []
    value = int(m.group(1), 16)
    return value, [name for bit, name in THROTTLED_BITS if value & bit]


def monitor_stats(lines):
    """Worst values from the debug monitor lines (debug.sh on)."""
    stats = {"lines": 0, "throttled": Counter(), "temp_max": None, "mem_min": None, "dstate": 0, "io_max": None}
    for line in lines:
        if "throttled=" not in line:
            continue
        stats["lines"] += 1
        m = re.search(r"throttled=(0x[0-9a-fA-F]+)", line)
        if m and int(m.group(1), 16):
            stats["throttled"][m.group(1)] += 1
        m = re.search(r"temp=([\d.]+)", line)
        if m:
            stats["temp_max"] = max(stats["temp_max"] or 0, float(m.group(1)))
        m = re.search(r"mem_avail=(\d+)M", line)
        if m:
            stats["mem_min"] = min(stats["mem_min"] or 10**9, int(m.group(1)))
        m = re.search(r"io_pressure=([\d.]+)", line)
        if m:
            stats["io_max"] = max(stats["io_max"] or 0, float(m.group(1)))
        if "D-state:" in line and "D-state: none" not in line:
            stats["dstate"] += 1
    return stats


def analyze(src):
    """[(status, area, summary, [details], advice)] for every area; the same order as the report."""
    out = []
    kern = {k: matches(src["kernel"], p) for k, p in KERNEL_PATTERNS.items()}
    svc = {k: matches(src["services"], p) for k, p in SERVICE_PATTERNS.items()}
    mon = monitor_stats(src["monitor"])
    value, flags = decode_throttled(src["throttled"])

    # power
    details = []
    if value is not None:
        details.append(f"    vcgencmd get_throttled = 0x{value:x}" + (f" ({', '.join(flags)})" if flags else ""))
    if mon["throttled"]:
        details.append("    monitor saw: " + ", ".join(f"{k} x{n}" for k, n in mon["throttled"].most_common()))
    power_bad = bool(kern["undervoltage"]) or (value is not None and value & 0x10001) or \
        any(int(k, 16) & 0x10001 for k in mon["throttled"])
    if power_bad:
        out.append((BAD, "Power", f"under-voltage detected ({len(kern['undervoltage'])} kernel messages)",
                    details + examples(kern["undervoltage"]),
                    "Use the official power supply (Pi 3: 5.1 V 2.5 A) and a short thick cable; unplug USB devices. "
                    "Under-voltage makes the SD card stall and the whole Pi freeze."))
    elif value is None and not mon["lines"]:
        out.append((INFO, "Power", "unknown: vcgencmd is not available here", details, ""))
    else:
        out.append((OK, "Power", "no voltage drops found", details, ""))

    # CPU throttling and temperature
    throttle_bits = (value or 0) & 0xE000E
    throttled_mon = any(int(k, 16) & 0xE000E for k in mon["throttled"])
    temp = f"max temperature {mon['temp_max']:.0f} °C (monitor)" if mon["temp_max"] else ""
    if throttle_bits or throttled_mon or kern["thermal"]:
        cause = "caused by under-voltage" if power_bad else "caused by heat"
        out.append((WARN, "CPU throttling", f"the CPU was slowed down, most likely {cause}"
                    + (f"; {temp}" if temp else ""), examples(kern["thermal"]),
                    "Fix the power supply first." if power_bad else
                    "Add heatsinks, a ventilated case, keep the Pi out of the sun."))
    elif mon["temp_max"] and mon["temp_max"] >= 75:
        out.append((WARN, "CPU throttling", f"no throttling, but hot: {temp}", [],
                    "Above 80 °C the CPU slows down: add heatsinks or a ventilated case."))
    else:
        out.append((OK, "CPU throttling", "no throttling found" + (f"; {temp}" if temp else ""), [], ""))

    # SD card
    sd_errors = kern["sd_error"] + kern["fs_error"]
    hung = kern["hung_task"]
    stalls = [line for line in hung if re.search(r"jbd2|flush|kworker|mmc|daemon\.py|python", line)] or hung
    slow = svc["slow_write"]
    if sd_errors:
        out.append((BAD, "SD card", f"{len(sd_errors)} card or file system errors", examples(sd_errors),
                    "The card is failing or was damaged by a power cut: back up /var/lib/zorcam-nvr/config, replace it with "
                    "a High Endurance card and reinstall."))
    elif stalls and len(kern["card_detect"]) >= len(stalls) and not mon["dstate"] and len(slow) <= 3:
        # only the card detection waited for its turn: the card was busy writing, it did not fail
        out.append((WARN, "SD card", f"busy with long writes: the card detection waited {len(stalls)} times "
                    "(no card errors, no stuck programs)", examples(stalls),
                    "Not a card fault: many cameras recording at once keep the card busy. If programs stall too "
                    "(slow writes, other hung tasks), check the power and the card."))
    elif stalls or mon["dstate"] or (slow and len(slow) > 3):
        parts = []
        if stalls:
            parts.append(f"{len(stalls)} hung task warnings")
        if mon["dstate"]:
            parts.append(f"processes waiting for the disk in {mon['dstate']} of {mon['lines']} monitor lines")
        if slow:
            parts.append(f"{len(slow)} slow frame writes (over 1 s)")
        out.append((BAD if stalls else WARN, "SD card", "the card stalls: " + "; ".join(parts),
                    examples(stalls or slow),
                    "Usually weak power or a worn card: fix the power first; if it persists, "
                    "replace the card with a High Endurance one (or boot from a USB SSD)."))
    else:
        out.append((OK, "SD card", "no card errors or stalls found", [], ""))

    # unclean shutdowns
    if kern["unclean"]:
        out.append((WARN, "Shutdowns", f"the file system was recovered after an unclean shutdown "
                    f"({len(kern['unclean'])} messages): a freeze with a hard reset or a power cut",
                    examples(kern["unclean"]), "Frequent hard resets damage the SD card; consider a UPS."))
    else:
        out.append((OK, "Shutdowns", "no unclean shutdowns found", [], ""))

    # memory
    if kern["oom"]:
        out.append((BAD, "Memory", f"out of memory: {len(kern['oom'])} kernel messages", examples(kern["oom"]),
                    "See which process was killed; the service limits in the systemd units restart a runaway "
                    "service. A desktop or other programs on the Pi also take memory."))
    elif (high := src.get("memory_events", {}).get("zorcam-nvr-capture", {}).get("high", 0)) > 1000:
        out.append((WARN, "Memory", f"the kernel slowed zorcam-nvr-capture down {high} times at its memory limit "
                    "(MemoryHigh) since the service started: the detectors lose frames and reconnect", [],
                    "Too many cameras for this Pi: use the cameras' second stream for the detector, save clips "
                    "on fewer cameras or switch some off."))
    elif mon["mem_min"] is not None and mon["mem_min"] < 100:
        out.append((WARN, "Memory", f"low: at least {mon['mem_min']} MB available (monitor)", [],
                    "Stop other programs on the Pi."))
    else:
        out.append((OK, "Memory", "no out-of-memory events" + (
            f"; at least {mon['mem_min']} MB available" if mon["mem_min"] is not None else ""), [], ""))

    # the gallery at its MemoryHigh: the page cache of the clips and frames it sends counts there too, and the kernel
    # drops it at the limit — harmless; only the process's own memory (anon) near the limit is a problem
    ghigh = src.get("memory_events", {}).get("zorcam-nvr-gallery", {}).get("high", 0)
    gstat = src.get("memory_stat", {}).get("zorcam-nvr-gallery", {})
    if ghigh and "anon" in gstat:
        anon, cache = gstat["anon"] // 2**20, gstat.get("file", 0) // 2**20
        if anon >= GALLERY_ANON_WARN_MB:
            out.append((WARN, "Gallery memory", f"the gallery process holds {anon} MB (anon) and reached its memory "
                        f"limit {ghigh} times since it started", [],
                        "The gallery itself grows: restart it (sudo systemctl restart zorcam-nvr-gallery) and "
                        "report it with debug.sh save."))
        else:
            out.append((OK, "Gallery memory", f"reached its limit {ghigh} times on the file cache of clips and "
                        f"frames (normal); the process itself holds {anon} MB, cache {cache} MB", [], ""))

    # services
    restarts = Counter()
    planned = [p for p in map(line_time, svc["planned"]) if p]

    def after_planned(line):  # a restart right after "..., restarting" (new settings) is not a problem
        t = line_time(line)
        return t is not None and any(0 <= (t - p).total_seconds() <= 30 for p in planned)
    svc["restart"] = [line for line in svc["restart"] if not after_planned(line)]
    for line in svc["restart"] + svc["watchdog"] + svc["killed"]:
        for s in SERVICES:
            if s in line:
                restarts[s] += 1
    if restarts:
        out.append((WARN, "Services", "restarts: " + ", ".join(f"{s} x{n}" for s, n in restarts.most_common()),
                    examples(svc["watchdog"] + svc["killed"] + svc["restart"]),
                    "journalctl -u SERVICE shows what happened before each restart."))
    else:
        out.append((OK, "Services", "no restarts, watchdog timeouts or crashes found", [], ""))

    # camera
    # the gallery's push lines ("... who can see the camera: -") are not camera errors
    cam = [line for line in svc["camera"] if "motion push" not in line
           and ("zorcam-nvr-capture" in line or "daemon.py" in line or "camera:" in line)]
    reasons = Counter(re.search(r"camera: ([^|]+)", line).group(1).strip() for line in cam
                      if re.search(r"camera: ([^|]+)", line))
    per_camera = Counter(m.group(1) for line in svc["reconnect"] if (m := re.search(r"\] \[([^\]]+)\] ", line)))
    if reasons or len(svc["reconnect"]) > 3:
        what = ", ".join([f"{r} x{n}" for r, n in reasons.most_common(3)]
                         + [f"{c} reconnects x{n}" for c, n in per_camera.most_common(5)]) \
            or f"{len(svc['reconnect'])} reconnects"
        advice = ("Check the camera login and password (Settings -> camera)." if any("401" in r for r in reasons) else
                  "Check the camera power, network cable and address (rtsp_url).")
        out.append((WARN, "Camera", f"connection problems: {what}", examples(cam or svc["reconnect"]), advice))
    else:
        out.append((OK, "Camera", "no connection problems found", [], ""))

    # the external address for WebRTC from the internet (STUN): not a failure, Live falls back to HLS
    stun_lines = svc["stun"]
    if stun_lines and re.search(r"stun: external address (not determined|lost)", stun_lines[-1]):
        out.append((WARN, "External address", "not determined: no usable external address for WebRTC from the internet",
                    examples(stun_lines[-3:]),
                    "Live from the internet falls back to HLS. Check that the device can reach a STUN server (UDP 3478) "
                    "and that the provider gives a public IP address (not CGNAT); the Zorcam cloud works either way."))
    elif stun_lines:
        out.append((OK, "External address", re.sub(r"^.*stun: external address ", "", stun_lines[-1]).strip(), [], ""))

    # clips without the camera video: the stream recorder had no segments (a placeholder was saved instead)
    if svc["clip_error"]:
        out.append((WARN, "Clips", f"{len(svc['clip_error'])} clips not saved: no stream segments",
                    examples(svc["clip_error"]),
                    "The stream recorder of the camera was not running (camera or go2rtc unavailable at that "
                    "moment): see the camera connection problems above."))
    else:
        out.append((OK, "Clips", "every clip was saved", [], ""))

    # disk space
    if src["disk"]:
        free_gb = src["disk"].free / 1e9
        out.append((WARN if free_gb < 1 else OK, "Disk space", f"{free_gb:.1f} GB free", [],
                    "Shorten the cameras' video and snapshot retention or delete old recordings in Settings."
                    if free_gb < 1 else ""))

    # what the logs cover
    note = (f"journal on disk, {src['boots']} boots" if src["persistent"] else
            "journal in memory only: logs before the last reboot are lost")
    mon_note = f"{mon['lines']} monitor lines" if mon["lines"] else "no monitor lines"
    out.append((INFO if src["persistent"] and mon["lines"] else WARN, "Log coverage", f"{note}; {mon_note}", [],
                "" if src["persistent"] and mon["lines"] else
                "Run ~/zorcam-nvr/app/debug.sh on to keep the journal on disk and log the system every 30 s, "
                "then run this again after the next problem."))
    return out


def verdict(findings):
    bad = [area for status, area, *_ in findings if status == BAD]
    if not bad:
        return "No problems found in the logs." if not any(s == WARN for s, *_ in findings) else \
            "No serious problems; see the warnings above."
    order = ["Power", "SD card", "Memory"]
    first = sorted(bad, key=lambda a: order.index(a) if a in order else len(order))[0]
    return {
        "Power": "Most likely cause: the power supply (under-voltage). Fix it first: it also explains SD card "
                 "stalls and freezes.",
        "SD card": "Most likely cause: the SD card. Replace it with a High Endurance card.",
        "Memory": "Most likely cause: memory shortage.",
    }.get(first, f"Problems: {', '.join(bad)}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("archive", nargs="?", help="an archive made by debug.sh save (default: this Pi)")
    ap.add_argument("--days", type=int, default=7, help="how many days of logs to read on this Pi (default 7)")
    args = ap.parse_args()
    src = archive_sources(args.archive) if args.archive else live_sources(args.days)
    color = sys.stdout.isatty()
    paint = {OK: "\033[32m", WARN: "\033[33m", BAD: "\033[31m", INFO: "\033[36m"}
    print(f"Zorcam diagnostics ({src['origin']})\n")
    findings = analyze(src)
    for status, area, summary, details, advice in findings:
        tag = f"{paint[status]}{status:<7}\033[0m" if color else f"{status:<7}"
        print(f"{tag} {area}: {summary}")
        for line in details:
            print(line)
        if advice:
            print(f"        -> {advice}")
    print("\n" + verdict(findings))


if __name__ == "__main__":
    main()
