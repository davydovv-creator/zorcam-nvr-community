"""app/diagnose.py on logs like the ones of the frozen Pi 3 (under-voltage, SD card stall, hard reset)."""
import importlib.util
import os

from conftest import REPO

spec = importlib.util.spec_from_file_location("diagnose", os.path.join(REPO, "app", "diagnose.py"))
diagnose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnose)

FROZEN = {
    "kernel": [
        "2026-09-27T16:24:14+03:00 nvr kernel: EXT4-fs (mmcblk0p2): orphan cleanup on readonly fs",
        "2026-09-27T16:24:27+03:00 nvr kernel: hwmon hwmon1: Undervoltage detected!",
        "2026-09-27T16:34:20+03:00 nvr kernel: INFO: task jbd2/mmcblk0p2-8:156 blocked for more than 120 seconds.",
    ],
    "services": [
        "2026-09-27T16:30:00+03:00 nvr daemon.py[740]: [2026-09-27T16:30:00] debug: slow write 1.jpg: 3.2 s",
        "2026-09-27T16:31:00+03:00 nvr systemd[1]: zorcam-nvr-capture.service: Watchdog timeout (limit 1min)!",
        "2026-09-27T16:31:05+03:00 nvr systemd[1]: zorcam-nvr-capture.service: Scheduled restart job, restart counter is at 1.",
    ],
    "monitor": ["2026-09-27T16:33:00+03:00 nvr zorcam-nvr-debug[1]: throttled=0x50005 temp=51.0'C arm=600MHz "
                "load=7.05,5.14,2.64 mem_avail=560M swap_used=0M io_pressure=45.10 zorcam-nvr-capture=60M "
                "D-state: jbd2/mmcblk0p2-8(156) daemon.py(740)"],
    "throttled": "throttled=0x50000", "boots": 3, "persistent": True, "disk": None, "origin": "test",
}
HEALTHY = {"kernel": [], "services": [], "monitor": ["x throttled=0x0 temp=48.0'C mem_avail=500M io_pressure=0.10 "
                                                     "D-state: none"],
           "throttled": "throttled=0x0", "boots": 2, "persistent": True, "disk": None, "origin": "test"}


def by_area(findings):
    return {area: (status, summary) for status, area, summary, *_ in findings}


def test_frozen_pi_points_at_the_power_supply():
    found = by_area(diagnose.analyze(FROZEN))
    assert found["Power"][0] == diagnose.BAD
    assert found["CPU throttling"][0] == diagnose.WARN and "under-voltage" in found["CPU throttling"][1]
    assert found["SD card"][0] == diagnose.BAD and "hung task" in found["SD card"][1]
    assert found["Shutdowns"][0] == diagnose.WARN
    assert "zorcam-nvr-capture x2" in found["Services"][1]
    assert diagnose.verdict(diagnose.analyze(FROZEN)).startswith("Most likely cause: the power supply")


def test_healthy_pi_has_no_problems():
    findings = diagnose.analyze(HEALTHY)
    assert all(status in (diagnose.OK, diagnose.INFO) for status, *_ in findings), findings
    assert diagnose.verdict(findings) == "No problems found in the logs."


def test_a_waiting_card_detection_is_not_a_failing_card():
    """Hung task warnings of the card detection only (kworker in mmc_rescan): the card was busy, it did not fail."""
    trace = ["2026-10-03T21:04:57+03:00 nvr kernel: INFO: task kworker/3:2:37050 blocked for more than 120 seconds.",
             "2026-10-03T21:04:57+03:00 nvr kernel: Workqueue: events_freezable mmc_rescan",
             "2026-10-03T21:06:58+03:00 nvr kernel: INFO: task kworker/3:2:37050 blocked for more than 241 seconds.",
             "2026-10-03T21:06:58+03:00 nvr kernel: Workqueue: events_freezable mmc_rescan"]
    found = by_area(diagnose.analyze({**HEALTHY, "kernel": trace}))
    assert found["SD card"] == (diagnose.WARN, "busy with long writes: the card detection waited 2 times "
                                               "(no card errors, no stuck programs)")
    assert not diagnose.verdict(diagnose.analyze({**HEALTHY, "kernel": trace})).startswith("Most likely cause: the SD")
    # a program stuck as well: the card stalls
    stuck = [*trace, "2026-10-03T21:07:00+03:00 nvr kernel: INFO: task jbd2/mmcblk0p2-8:156 blocked for more than 120 seconds."]
    assert by_area(diagnose.analyze({**HEALTHY, "kernel": stuck}))["SD card"][0] == diagnose.BAD


def test_throttled_bits():
    assert diagnose.decode_throttled("throttled=0x50000") == (0x50000, ["under-voltage has occurred",
                                                                         "throttling has occurred"])
    assert diagnose.decode_throttled("") == (None, [])


def test_restarts_to_apply_new_settings_are_not_problems():
    logs = {**HEALTHY, "services": [
        "2026-09-28T01:04:15+03:00 nvr python3[2111]: [2026-09-28T01:04:15] zorcam.conf, cameras.json or secrets.env "
        "changed, restarting",
        "2026-09-28T01:04:20+03:00 nvr systemd[1]: zorcam-nvr-capture.service: Scheduled restart job, restart counter is at 1.",
    ]}
    assert by_area(diagnose.analyze(logs))["Services"][0] == diagnose.OK
    logs["services"].append("2026-09-28T02:00:00+03:00 nvr systemd[1]: zorcam-nvr-capture.service: Scheduled restart job, "
                            "restart counter is at 2.")  # an hour later: a real one
    assert by_area(diagnose.analyze(logs))["Services"] == (diagnose.WARN, "restarts: zorcam-nvr-capture x1")


def test_overloaded_pi_with_five_cameras():
    """Like the Pi with 5 cameras: the capture service throttled at MemoryHigh millions of times, reconnects per
    camera, clips without stream segments — all three are named, with the cameras."""
    line = "2026-10-02T09:06:{:02d}+03:00 nvr python3[5089]: [2026-10-02T09:06:{:02d}] [{}] {}"
    logs = {**HEALTHY, "memory_events": {"zorcam-nvr-capture": {"low": 0, "high": 12191852, "max": 0}},
            "services": [line.format(i, i, "Nelly-bedroom", "no frames for 15 s, reconnecting") for i in range(9)]
            + [line.format(i, i, "nelly-hall", "no frames for 15 s, reconnecting") for i in range(4)]
            + [line.format(50, 50, "home", "stream clip error for 20261002_090650_000: no stream segments for the clip")]}
    found = by_area(diagnose.analyze(logs))
    assert found["Memory"][0] == diagnose.WARN and "12191852 times" in found["Memory"][1]
    assert "Nelly-bedroom reconnects x9" in found["Camera"][1] and "nelly-hall reconnects x4" in found["Camera"][1]
    assert found["Clips"][0] == diagnose.WARN and found["Clips"][1].startswith("1 clips not saved")


def test_motion_push_lines_are_not_camera_errors():
    """The gallery logs "motion push cam94: delivered to 0 of 0 subscriptions (none of users who can see the camera: -)":
    not a camera connection problem."""
    push = ("2026-10-15T18:46:43+03:00 nvr2 python3[971]: [2026-10-15T18:46:43] motion push cam94: delivered to 0 of 0 "
            "subscriptions (none of users who can see the camera: -)")
    found = by_area(diagnose.analyze({**HEALTHY, "services": [push] * 5}))
    assert found["Camera"][0] == diagnose.OK


def test_unknown_external_address_is_a_warning():
    """The gallery logs the STUN result only when it changes; the last line is the state."""
    head = "nvr python3[971]: [2026-10-16T10:00:00] stun: external address"
    lost = f"2026-10-16T10:00:00+03:00 {head} not determined: no STUN server answered"
    found = f"2026-10-16T09:00:00+03:00 {head} 93.184.216.34 (via stun.cloudflare.com:3478)"
    area = by_area(diagnose.analyze({**HEALTHY, "services": [lost]}))
    assert area["External address"][0] == diagnose.WARN
    assert by_area(diagnose.analyze({**HEALTHY, "services": [found, lost]}))["External address"][0] == diagnose.WARN
    ok = by_area(diagnose.analyze({**HEALTHY, "services": [lost, found]}))["External address"]
    assert ok[0] == diagnose.OK and "93.184.216.34" in ok[1]
    assert "External address" not in by_area(diagnose.analyze(HEALTHY))
    assert by_area(diagnose.analyze({**HEALTHY, "services": [lost]}))["Camera"][0] == diagnose.OK


def test_memory_events_file(tmp_path):
    path = tmp_path / "memory.events"
    path.write_text("low 0\nhigh 42\nmax 0\noom 0\n")
    assert diagnose.memory_events(str(path))["high"] == 42
    assert diagnose.memory_events(str(tmp_path / "missing")) == {}


def test_gallery_memory_limit_on_file_cache_is_normal():
    """The gallery reaches MemoryHigh on the page cache of the clips it sends (seen 17.10.2026: high 828, anon 55 MB)."""
    mb = 2**20
    logs = {**HEALTHY, "memory_events": {"zorcam-nvr-gallery": {"high": 828}},
            "memory_stat": {"zorcam-nvr-gallery": {"anon": 55 * mb, "file": 23 * mb}}}
    level, text = by_area(diagnose.analyze(logs))["Gallery memory"]
    assert level == diagnose.OK and "828" in text and "55 MB" in text
    logs["memory_stat"]["zorcam-nvr-gallery"]["anon"] = 150 * mb
    assert by_area(diagnose.analyze(logs))["Gallery memory"][0] == diagnose.WARN
    assert "Gallery memory" not in by_area(diagnose.analyze(HEALTHY))
