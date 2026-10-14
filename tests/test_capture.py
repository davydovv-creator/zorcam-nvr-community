"""Capture helpers: the keyframe rate the camera actually sends, motion clips from the camera stream."""
import io
import queue
import json
import os
import time
from pathlib import Path

import pytest

import daemon


def test_keyframe_rate_is_the_median_interval():
    assert daemon.keyframe_rate([]) is None and daemon.keyframe_rate([0.0]) is None
    assert daemon.keyframe_rate([0.0, 0.4, 0.8, 1.2, 1.6]) == 2.5
    # a network hiccup (two frames at once after a gap) does not change it
    assert daemon.keyframe_rate([0.0, 0.4, 0.8, 1.6, 1.61, 2.0, 2.4]) == 2.5


def no_stream(start, end, path):
    raise RuntimeError("no stream segments for the clip")


def test_clip_appears_only_when_saved(tmp_path, monkeypatch):
    """The gallery lists a clip when both its poster and .mp4 exist: while it is glued the video is a .part file.
    Without stream segments nothing is encoded: the poster gets the text and the clip a .failed marker."""
    monkeypatch.setattr(daemon, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "failed_frame", lambda poster: b"failed:" + poster)
    seen = []

    def clip(start, end, path):
        seen.append(sorted(os.listdir(tmp_path)))
        with open(path, "wb") as f:
            f.write(b"mp4")
        return 4, 98
    monkeypatch.setattr(daemon, "stream_clip", clip)
    daemon.save_event(b"poster", "ok", (100.0, 113.0))
    assert seen == [["ok.jpg"]]  # no .mp4 yet, so the gallery does not list it
    assert sorted(os.listdir(tmp_path)) == ["ok.jpg", "ok.mp4"]

    daemon.write_poster(b"first", "early")  # on motion: the gallery shows it while the clip is recorded
    daemon.save_event(b"later", "early", (100.0, 113.0))
    assert (tmp_path / "early.jpg").read_bytes() == b"first"
    for n in ("early.jpg", "early.mp4"):
        os.remove(tmp_path / n)

    monkeypatch.setattr(daemon, "stream_clip", no_stream)
    daemon.write_poster(b"broken", "bad")
    daemon.save_event(b"broken", "bad", (100.0, 113.0))
    assert sorted(os.listdir(tmp_path)) == ["bad.failed", "bad.jpg", "ok.jpg", "ok.mp4"]
    assert (tmp_path / "bad.jpg").read_bytes() == b"failed:broken"


def test_unfinished_clips_are_marked_not_saved(tmp_path, monkeypatch):
    """A camera process stopped mid-clip leaves a poster with a .part or a truncated .mp4 (no moov): at the next
    start they become clips not saved (poster with the text, .failed marker, no video); complete clips stay."""
    monkeypatch.setattr(daemon, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "failed_frame", lambda poster: b"failed")
    box = lambda kind, data=b"": (8 + len(data)).to_bytes(4, "big") + kind + data  # noqa: E731
    (tmp_path / "good.jpg").write_bytes(b"jpg")
    (tmp_path / "good.mp4").write_bytes(box(b"ftyp", b"isom") + box(b"moov", b"x" * 20) + box(b"mdat", b"y"))
    (tmp_path / "cut.jpg").write_bytes(b"jpg")
    (tmp_path / "cut.mp4").write_bytes(box(b"ftyp", b"isom") + box(b"free") + (1000).to_bytes(4, "big") + b"mdat")
    (tmp_path / "wip.jpg").write_bytes(b"jpg")
    (tmp_path / "wip.mp4.part").write_bytes(b"ftyp")
    (tmp_path / "old.jpg").write_bytes(b"failed")
    (tmp_path / "old.failed").write_bytes(b"")
    assert daemon.mp4_complete(str(tmp_path / "good.mp4")) and not daemon.mp4_complete(str(tmp_path / "cut.mp4"))
    at_start = os.listdir(tmp_path)
    (tmp_path / "new.jpg").write_bytes(b"jpg")  # a clip of the new process, still recorded: not in the start listing
    daemon.recover_clips(at_start)
    assert sorted(os.listdir(tmp_path)) == ["cut.failed", "cut.jpg", "good.jpg", "good.mp4", "new.jpg",
                                            "old.failed", "old.jpg", "wip.failed", "wip.jpg"]
    assert (tmp_path / "cut.jpg").read_bytes() == b"failed" and (tmp_path / "good.jpg").read_bytes() == b"jpg"


def test_failed_frame_has_the_text(monkeypatch):
    """The placeholder frame is the dimmed poster with text over it, same size."""
    import io

    from PIL import Image
    poster = io.BytesIO()
    Image.new("RGB", (640, 360), (200, 200, 200)).save(poster, "JPEG")
    with Image.open(io.BytesIO(daemon.failed_frame(poster.getvalue()))) as img:
        assert img.size == (640, 360)
        colors = {img.getpixel((x, y))[0] > 150 for x in range(0, 640, 4) for y in range(120, 240, 4)}
        assert img.getpixel((5, 5))[0] < 100 and colors == {True, False}  # dimmed, white text in the middle


def test_detector_encodes_jpeg_in_one_thread():
    """The detector's JPEG encoder runs in one thread: a thread per core took ~40 MB more for each camera."""
    cmd = daemon.detector_cmd()
    out = cmd[cmd.index("-i") + 2:]
    assert out[out.index("-threads") + 1] == "1" and "mjpeg" in out
    assert cmd[cmd.index("-flags") + 1] == "low_delay" and "-skip_frame" in cmd[:cmd.index("-i")]


def test_snapshots_are_written_off_the_detector_loop(tmp_path, monkeypatch):
    """The detector only queues a snapshot: a slow SD card does not hold motion detection, and a failed write
    (no space) is logged once per cause and does not drop the camera connection."""
    import queue
    import threading
    import time

    def wait_until(done):
        deadline = time.monotonic() + 5
        while not done() and time.monotonic() < deadline:
            time.sleep(0.01)
        return done()

    monkeypatch.setattr(daemon, "RAW_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "SNAPSHOTS", queue.Queue(maxsize=2))
    logged = []
    monkeypatch.setattr(daemon, "log", logged.append)
    threading.Thread(target=daemon.write_snapshots, daemon=True).start()
    daemon.SNAPSHOTS.put(("a", b"jpg-a"))
    assert wait_until(lambda: (tmp_path / "a.jpg").exists())
    monkeypatch.setattr(daemon, "RAW_DIR", str(tmp_path / "missing"))
    for name in ("b", "c", "d"):
        daemon.SNAPSHOTS.put((name, b"jpg"))
    assert wait_until(lambda: daemon.SNAPSHOTS.empty() and logged)
    time.sleep(0.1)
    assert (tmp_path / "a.jpg").read_bytes() == b"jpg-a"
    assert len(logged) == 1 and logged[0].startswith("snapshot write error:")


def test_log_writes_each_line_at_once(monkeypatch):
    """print() writes the text and the newline apart: lines of the clip, snapshot and placeholder threads got
    glued together in the journal (and diagnose.py reads it line by line)."""
    import sys
    writes = []

    class Out:
        def write(self, text):
            writes.append(text)

        def flush(self):
            pass
    monkeypatch.setattr(sys, "stdout", Out())
    daemon.log("event saved")
    assert len(writes) == 1 and writes[0].endswith(" event saved\n") and writes[0].count("\n") == 1


def test_stream_clip_takes_the_finished_segments_of_its_time(tmp_path, monkeypatch):
    """Stream mode: the clip is the camera video from the segments that overlap its time, glued without
    re-encoding; the segment still being written (no next one yet) is left out."""
    import subprocess
    monkeypatch.setattr(daemon, "SEGMENT_DIR", str(tmp_path))
    for start in range(100, 122, 2):
        (tmp_path / f"seg_{start}.ts").write_bytes(f"<{start}>".encode())
    (tmp_path / "seg_x.ts").write_bytes(b"?")  # not a segment name
    fed = {}

    class FakeFfmpeg:
        def __init__(self, cmd, **kw):
            fed["cmd"], fed["env"], self.returncode = cmd, kw["env"], 0

        def communicate(self, timeout):
            fed["input"] = b"".join(Path(p).read_bytes() for p in fed["cmd"][fed["cmd"].index("-i") + 1][7:].split("|"))
            with open(fed["cmd"][-1], "wb") as f:  # an mp4 with its index, as ffmpeg writes it
                f.write((16).to_bytes(4, "big") + b"ftypisom" + b"\0" * 4 + (8).to_bytes(4, "big") + b"moov")
            return b"", b""
    monkeypatch.setattr(daemon.subprocess, "Popen", FakeFfmpeg)
    codec = {"name": "h264"}
    monkeypatch.setattr(daemon, "video_codec", lambda path: codec["name"])
    out = str(tmp_path / "clip.mp4.part")
    assert daemon.stream_clip(105, 111, out) == (4, 104)  # four segments, the first from 104
    # ffmpeg reads the segments itself (concat), they do not pass through the camera process memory
    assert fed["cmd"][fed["cmd"].index("-i") + 1].startswith("concat:" + str(tmp_path / "seg_104.ts") + "|")
    assert fed["input"] == b"<104><106><108><110>" and fed["env"] is daemon.CHILD_ENV
    assert fed["cmd"][fed["cmd"].index("-c") + 1] == "copy" and "-f" in fed["cmd"] and "hvc1" not in fed["cmd"]
    # H.265: the mp4 is tagged hvc1, the only tag Safari on iPhone plays
    codec["name"] = "hevc"
    daemon.stream_clip(105, 111, out)
    assert fed["cmd"][fed["cmd"].index("-tag:v") + 1] == "hvc1"
    (tmp_path / "seg_120.ts").unlink()
    (tmp_path / "seg_118.ts").unlink()
    class Clock:  # the wait for a segment that never comes passes at once
        now = 0.0

        def time(self):
            self.now += 5
            return self.now

        def sleep(self, seconds):
            pass
    monkeypatch.setattr(daemon, "time", Clock())
    with pytest.raises(RuntimeError):
        daemon.stream_clip(117, 125, out)  # only the unfinished segment 116 covers it
    assert subprocess.Popen is FakeFfmpeg


def test_segmenter_takes_the_sound_only_when_allowed():
    video = daemon.segmenter_cmd()
    assert "0:v:0" in video and "0:a:0?" not in video and video[video.index("-c") + 1] == "copy"
    cmd = daemon.segmenter_cmd(True)
    assert cmd[cmd.index("0:v:0") + 1:cmd.index("0:v:0") + 3] == ["-map", "0:a:0?"]  # optional: no sound is no error
    assert cmd[cmd.index("-c") + 1] == "copy" and "libx264" not in cmd and "aac" not in cmd


def test_video_codec_reads_the_first_line_of_ffprobe(monkeypatch):
    # an MPEG-TS stream belongs to a program too: ffprobe prints its codec twice, and the clip lost the hvc1 tag
    out = {"text": "hevc\nhevc\n"}
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda *a, **k: type("P", (), {"stdout": out["text"]})())
    assert daemon.video_codec("seg.ts") == "hevc"
    out["text"] = "h264\n"
    assert daemon.video_codec("seg.ts") == "h264"
    out["text"] = ""
    assert daemon.video_codec("seg.ts") == ""


def test_clip_sound_only_for_aac_with_the_flag_on(monkeypatch):
    import io
    import urllib.error
    answer = {"body": '{"codec_name":"h264"},{"codec_name":"aac"}'}
    asked = []

    def urlopen(url, timeout):
        asked.append((url, timeout))
        if isinstance(answer["body"], Exception):
            raise answer["body"]
        return io.BytesIO(answer["body"].encode())
    monkeypatch.setattr(daemon.urllib.request, "urlopen", urlopen)
    on, off = {"audio": True}, {"audio": False}
    assert daemon.clip_audio("yard", on) is True and asked[0][0].endswith("src=yard") and asked[0][1] <= 10
    asked.clear()
    assert daemon.clip_audio("yard", off) is False and daemon.clip_audio("yard", None) is False and not asked
    for body in ('{"codec_name":"h264"},{"codec_name":"pcma"}', '{"codec_name":"h264"},{"codec_name":"opus"}',
                 '{"codec_name":"h264"}', urllib.error.URLError("down"), TimeoutError()):
        answer["body"] = body
        assert daemon.clip_audio("yard", on) is False  # G.711, Opus, no sound, go2rtc not answering: video only


def test_stream_recorder_dies_with_the_camera_process(tmp_path):
    """The stream ffmpeg writes files, not a pipe: without PR_SET_PDEATHSIG it would outlive a camera process
    killed by the supervisor."""
    import signal
    import subprocess
    import sys
    import time
    pid_file = tmp_path / "child.pid"
    camera = subprocess.Popen([sys.executable, "-c", (
        "import subprocess, sys, time\n"
        f"sys.path.insert(0, {os.path.dirname(daemon.__file__)!r})\n"
        "import daemon\n"
        "p = subprocess.Popen(['sleep', '30'], preexec_fn=daemon.die_with_parent)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(30)\n")])
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        time.sleep(0.05)
    child = int(pid_file.read_text())
    camera.send_signal(signal.SIGKILL)
    camera.wait()
    for _ in range(100):
        try:
            os.kill(child, 0)
            with open(f"/proc/{child}/stat") as f:
                if f.read().split(") ")[1][0] == "Z":  # a zombie: dead, waiting to be reaped
                    break
        except (ProcessLookupError, FileNotFoundError):
            break
        time.sleep(0.05)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("the child outlived its parent")


def test_written_files_leave_the_page_cache(tmp_path, monkeypatch):
    """The cache of written snapshots and clips counts toward the service memory: it filled MemoryHigh at every
    snapshot. A written file is flushed and dropped from the cache (the gallery reads it from the card)."""
    import queue
    import threading
    import time
    dropped = []
    monkeypatch.setattr(daemon.os, "posix_fadvise", lambda fd, off, n, advice: dropped.append(advice))
    (tmp_path / "a.jpg").write_bytes(b"x")
    daemon.drop_cache(str(tmp_path / "a.jpg"), str(tmp_path / "missing.jpg"))
    assert dropped == [daemon.os.POSIX_FADV_DONTNEED]
    monkeypatch.setattr(daemon, "RAW_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "SNAPSHOTS", queue.Queue(maxsize=2))
    synced = []
    monkeypatch.setattr(daemon, "drop_cache", lambda *paths: synced.extend(paths))
    threading.Thread(target=daemon.write_snapshots, daemon=True).start()
    daemon.SNAPSHOTS.put(("b", b"jpg"))
    for _ in range(100):
        if synced:
            break
        time.sleep(0.02)
    assert synced == [str(tmp_path / "b.jpg")]
    monkeypatch.setattr(daemon, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "stream_clip", lambda start, end, path: open(path, "wb").close() or (4, 98))
    daemon.save_event(b"k1", "clip", (100.0, 113.0))
    assert synced[-2:] == [str(tmp_path / "clip.mp4"), str(tmp_path / "clip.jpg")]


def test_stream_recorder_recreates_its_buffer_directory(tmp_path, monkeypatch):
    """systemd-logind may wipe the user's files in /dev/shm when their last session ends: the stream buffer
    directory is created again before ffmpeg restarts, otherwise ffmpeg exits at once (code 254) forever."""
    seg_dir = tmp_path / "zorcam_rec_home"
    monkeypatch.setattr(daemon, "SEGMENT_DIR", str(seg_dir))
    monkeypatch.setattr(daemon, "RESTART_DELAY", 0)
    starts = []

    class Exited:
        returncode = 254

        def poll(self):
            return self.returncode

    def popen(cmd, **kwargs):
        starts.append(seg_dir.is_dir())
        if len(starts) == 1:
            seg_dir.rmdir()  # wiped while ffmpeg runs
        return Exited()

    def sleep(_seconds):
        if len(starts) >= 2:
            raise StopIteration
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        daemon.keep_segments()
    assert starts == [True, True]


def test_stall_timeout_follows_the_keyframe_interval(monkeypatch):
    """A camera with a keyframe every 10 s is not 'lost' after 15 s: three intervals, within [stall_timeout, 60]."""
    monkeypatch.setattr(daemon, "STALL_TIMEOUT", 15)
    assert daemon.stall_timeout(None) == 15
    assert daemon.stall_timeout(1.0) == 15
    assert daemon.stall_timeout(10.0) == 30
    assert daemon.stall_timeout(40.0) == daemon.STALL_MAX
    # the first frame of a connection: a loaded Pi needs tens of seconds to start ffmpeg and connect
    assert daemon.stall_timeout(None, first=True) == daemon.stall_timeout(4.0, first=True) == daemon.STALL_MAX


def test_reconnect_pause_grows_while_the_camera_fails():
    """3, 6, 12 ... 60 s between failed attempts; a connection that streamed a minute resets the pause."""
    delays, delay = [], daemon.RETRY_MIN
    for _ in range(7):
        delay = daemon.retry_delay(delay, 0)
        delays.append(delay)
    assert delays == [6, 12, 24, 48, 60, 60, 60]
    assert daemon.retry_delay(60, daemon.GOOD_RUN) == daemon.RETRY_MIN
    assert daemon.retry_delay(daemon.RETRY_MIN, 5) == 6


def test_waiting_for_a_frame_keeps_the_heartbeat(monkeypatch):
    """Up to a minute without frames (rare keyframes) must not look like a hung camera process to the supervisor:
    the status file is updated while waiting; the wait ends with queue.Empty after its time."""
    beats = []
    monkeypatch.setattr(daemon.STATUS, "update", lambda *a, **kw: beats.append(1))
    clock = [0.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])

    class Q:
        def get(self, timeout):
            clock[0] += timeout
            raise daemon.queue.Empty
    with pytest.raises(daemon.queue.Empty):
        daemon.next_frame(Q(), 30)
    assert clock[0] == pytest.approx(30) and len(beats) == 5  # every 5 s, not one 30 s block


def test_detector_falls_back_to_the_main_stream():
    """The second stream gives no frames 3 connections in a row (a wrong path, switched off in the camera): the
    detector uses the main stream for a while, then tries the second one again; one good run resets the count."""
    src = daemon.DetectorSource(True)
    assert src.sub(0)
    assert not src.result(True, 0, 1) and not src.result(True, 0, 2)
    assert not src.result(True, 30, 3)  # frames came: the count starts again
    assert not src.result(True, 0, 4) and not src.result(True, 0, 5) and src.result(True, 0, 6)
    assert not src.sub(7)  # on the main stream until it stops
    assert src.sub(6 + daemon.SUB_RETRY)  # or until the time is up
    assert not daemon.DetectorSource(False).sub(0)


def test_detector_leaves_the_main_stream_when_its_connection_ends():
    """A network blip silences both streams: three empty connections to the second one move the detector to the main
    one, and when that connection ends too it goes back to the second stream at once (the main one is 1080p: its
    decoding took the memory of the detector to the limit on 6.10)."""
    src = daemon.DetectorSource(True)
    assert not src.result(True, 0, 1) and not src.result(True, 0, 2) and src.result(True, 0, 3)
    assert not src.sub(4)
    assert not src.result(False, 0, 5)  # the main stream gave nothing, or stopped: the fallback is over
    assert src.sub(6) and src.fails == 0
    assert not src.result(True, 0, 7)  # three more empty connections are needed again
    # a main stream that runs well keeps the fallback while it runs
    src = daemon.DetectorSource(True)
    assert not src.result(True, 0, 1) and not src.result(True, 0, 2) and src.result(True, 0, 3)
    assert not src.sub(100)


def test_detector_reads_the_given_stream():
    cmd = daemon.detector_cmd("rtsp://127.0.0.1:8554/front.sub")
    assert cmd[cmd.index("-i") + 1] == "rtsp://127.0.0.1:8554/front.sub"
    assert daemon.detector_cmd()[daemon.detector_cmd().index("-i") + 1] == daemon.SOURCE_URL
    assert cmd[cmd.index("-map") + 1] == "0:v:0"  # a camera with "Sound in Live": the detector takes only the video


def test_supervisor_logs_the_memory_of_each_process(tmp_path, monkeypatch):
    """Every MEMORY_LOG_INTERVAL the journal gets the own memory of the supervisor, each camera process and its
    ffmpeg: what grows is seen in the log."""
    def proc(pid, ppid, cmd, kb):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (some name) S {ppid} 1 1")
        (d / "cmdline").write_bytes(b"\0".join(cmd) + b"\0")
        (d / "status").write_text(f"Name: x\nRssAnon:\t{kb} kB\nRssFile: 999 kB\n")
    me = os.getpid()
    proc(me, 1, [b"python3", b"daemon.py"], 20 * 1024)
    proc(10, me, [b"python3", b"daemon.py", b"--camera", b"cam1"], 40 * 1024)
    proc(11, 10, [b"ffmpeg", b"-i", b"x", b"-vcodec", b"mjpeg", b"-"], 22 * 1024)
    proc(12, 10, [b"ffmpeg", b"-f", b"segment", b"seg_%s.ts"], 9 * 1024)
    (tmp_path / "self").mkdir()

    class Worker:
        pid = 10
    line = daemon.memory_line({"cam1": (Worker(), 0.0)}, proc=str(tmp_path))
    assert line == "memory (anon, MB): supervisor 20 | cam1: python 40, detector 22, recorder 9 | total 91"
    assert daemon.anon_mb(99, proc=str(tmp_path)) == 0  # already gone


def test_camera_process_limits_malloc_arenas(monkeypatch):
    calls = []

    class Libc:
        def mallopt(self, param, value):
            calls.append((param, value))
    monkeypatch.setattr(daemon.ctypes, "CDLL", lambda name: Libc())
    daemon.limit_malloc()
    assert calls == [(-8, 2), (-3, 128 * 1024)]


# ---------------------------------------------------------------- the motion frame, the poster, the motion time

def gray(square=None, noise=(), value=255):
    """A small frame (160×90) of a dark scene with a bright square (x0, y0, x1, y1) and single bright pixels."""
    from PIL import Image
    img = Image.new("L", daemon.SAMPLE_SIZE, 30)
    for x in range(square[0], square[2]) if square else ():
        for y in range(square[1], square[3]):
            img.putpixel((x, y), value)
    for xy in noise:
        img.putpixel(xy, 255)
    return img


def test_motion_box_surrounds_what_changed():
    """A square moved: the box is the area where the two frames differ (old and new place); in fractions of the frame."""
    box = daemon.motion_box(gray((20, 20, 40, 40)), gray((30, 20, 50, 40)))
    assert box == [round(20 / 160, 3), round(20 / 90, 3), round(50 / 160, 3), round(40 / 90, 3)]
    # appeared out of nowhere: its own place
    assert daemon.motion_box(gray(), gray((100, 10, 120, 30))) == [0.625, round(10 / 90, 3), 0.75, round(30 / 90, 3)]
    assert all(0 <= v <= 1 for v in daemon.motion_box(gray(), gray((0, 0, 160, 50))))


def test_motion_box_ignores_noise_and_nothing_changed():
    still = gray((20, 20, 40, 40))
    assert daemon.motion_box(still, gray((20, 20, 40, 40))) is None
    # single pixels and a 2×2 speck are noise; a square moved next to them still has its box
    assert daemon.motion_box(still, gray((20, 20, 40, 40), noise=[(5, 5), (80, 60), (81, 60), (80, 61), (81, 61)])) is None
    box = daemon.motion_box(gray((20, 20, 40, 40), noise=[(5, 5)]), gray((30, 20, 50, 40), noise=[(150, 80)]))
    assert box[0] == round(20 / 160, 3) and box[2] == round(50 / 160, 3)
    # a small change in brightness (compression, a cloud) is under the threshold
    assert daemon.motion_box(gray((20, 20, 40, 40), value=255), gray((20, 20, 40, 40), value=240)) is None
    # the whole picture changed (light switched on): no box
    assert daemon.motion_box(gray(), gray((0, 0, 160, 90))) is None


def test_motion_time_in_the_clip_comes_from_the_first_segment(tmp_path, monkeypatch):
    """.json: the box at once (with the poster), then motion_at = trigger time - start of the first glued segment,
    written before the .mp4 appears; a clip without a box and time gets no .json."""
    monkeypatch.setattr(daemon, "VIDEO_DIR", str(tmp_path))
    seen = {}

    def clip(start, end, path):
        seen["json"] = (tmp_path / "ev.json").read_text()  # the poster's .json is there while the clip is glued
        seen["window"] = (start, end)
        with open(path, "wb") as f:
            f.write(b"mp4")
        return 4, 100
    monkeypatch.setattr(daemon, "stream_clip", clip)
    daemon.write_poster(b"poster", "ev", [0.1, 0.2, 0.3, 0.4])
    assert (tmp_path / "ev.jpg").read_bytes() == b"poster"
    assert json.loads((tmp_path / "ev.json").read_text()) == {"box": [0.1, 0.2, 0.3, 0.4], "motion_at": None}
    daemon.save_event(b"trigger", "ev", (101.0, 113.0), box=[0.1, 0.2, 0.3, 0.4], motion_wall=104.26)
    assert json.loads(seen["json"])["motion_at"] is None
    assert json.loads((tmp_path / "ev.json").read_text()) == {"box": [0.1, 0.2, 0.3, 0.4], "motion_at": 4.3}
    assert sorted(os.listdir(tmp_path)) == ["ev.jpg", "ev.json", "ev.mp4"]  # no leftovers of the temporary file
    # a late poster thread does not wipe the time
    daemon.write_poster(b"poster", "ev", [0.1, 0.2, 0.3, 0.4])
    assert json.loads((tmp_path / "ev.json").read_text())["motion_at"] == 4.3
    # no box: the time alone; neither: no file
    daemon.save_event(b"p", "t", (101.0, 113.0), motion_wall=99.0)  # the clock stepped back: not negative
    assert json.loads((tmp_path / "t.json").read_text()) == {"box": None, "motion_at": 0.0}
    daemon.save_event(b"p", "n", (101.0, 113.0))
    assert not (tmp_path / "n.json").exists()
    daemon.write_poster(b"p", "m")
    assert not (tmp_path / "m.json").exists()


def test_recover_clips_leaves_json_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "failed_frame", lambda poster: b"failed")
    box = lambda kind, data=b"": (8 + len(data)).to_bytes(4, "big") + kind + data  # noqa: E731
    (tmp_path / "good.jpg").write_bytes(b"jpg")
    (tmp_path / "good.json").write_text("{}")
    (tmp_path / "good.mp4").write_bytes(box(b"ftyp", b"isom") + box(b"moov", b"x" * 20))
    (tmp_path / "cut.jpg").write_bytes(b"jpg")
    (tmp_path / "cut.json").write_text("{}")
    (tmp_path / "cut.json.part").write_text("{")
    daemon.recover_clips(os.listdir(tmp_path))
    assert sorted(os.listdir(tmp_path)) == ["cut.failed", "cut.jpg", "cut.json", "good.jpg", "good.json", "good.mp4"]


class FakeFfmpeg:
    """What run_once starts: keyframes of the camera are JPEGs on its stdout."""

    def __init__(self, frames):
        self.stdout = io.BufferedReader(io.BytesIO(b"".join(frames)))
        self.stderr = io.BytesIO()

    def kill(self):
        pass

    def wait(self):
        pass


def jpeg_of(square=None, size=(320, 180)):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, (30, 30, 30))
    if square:
        ImageDraw.Draw(img).rectangle(square, fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95)
    return out.getvalue()


def run_frames(tmp_path, monkeypatch, frames, saved):
    """Run the detector over frames until their end; the clip of the event is "saved" by a stand-in for the glue."""
    monkeypatch.setattr(daemon, "VIDEO_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "STREAM", True)
    monkeypatch.setattr(daemon, "SAVE_FRAMES", False)
    monkeypatch.setattr(daemon, "MOTION_THRESHOLD", 0.01)
    monkeypatch.setattr(daemon, "notify_gallery", lambda name: None)
    monkeypatch.setattr(daemon, "set_error", lambda *a, **kw: None)
    monkeypatch.setattr(daemon.zorcam_config, "probe_camera", lambda *a: "")
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **kw: FakeFfmpeg(frames))
    daemon.STATUS.data["connected_since"] = None

    def clip(start, end, path):
        saved.append((start, end))
        with open(path, "wb") as f:
            f.write(b"mp4")
        return 2, start
    monkeypatch.setattr(daemon, "stream_clip", clip)
    daemon.run_once()
    for _ in range(100):  # the poster and the clip are written by threads
        if any(p.suffix == ".mp4" for p in tmp_path.iterdir()):
            break
        time.sleep(0.05)


def test_poster_is_the_trigger_frame_with_the_box_of_the_pair(tmp_path, monkeypatch):
    """Motion: the poster is the frame that triggered (the second of the compared pair); its box is the area that
    differs from the first frame of the pair. Nothing is drawn into the poster file."""
    still, trigger, after = jpeg_of(), jpeg_of((100, 60, 160, 120)), jpeg_of((140, 60, 200, 120))
    saved = []
    run_frames(tmp_path, monkeypatch, [still, still, trigger, after, after], saved)
    names = sorted(p.stem for p in tmp_path.glob("*.jpg"))
    assert len(names) == 1
    assert (tmp_path / (names[0] + ".jpg")).read_bytes() == trigger
    meta = json.loads((tmp_path / (names[0] + ".json")).read_text())
    x0, y0, x1, y1 = meta["box"]
    assert abs(x0 - 100 / 320) < 0.03 and abs(x1 - 160 / 320) < 0.03 and abs(y0 - 60 / 180) < 0.03 and abs(y1 - 120 / 180) < 0.03
    assert meta["motion_at"] is not None and len(saved) == 1


def test_box_shows_from_the_first_frame_of_the_pair(tmp_path, monkeypatch):
    """motion_wall (from when the gallery shows the mark and the box) is the clock of the first compared frame, the last
    one without the motion, not of the trigger frame or of the frame before the pair. The clip starts at least 1 s
    before it."""
    class Clock:  # the clock moves 10 s per keyframe; the rest is the real time module
        n = 0

        def time(self):
            return 1000.0 + 10 * self.n

        def __getattr__(self, name):
            return getattr(time, name)
    clock, real_next, calls = Clock(), daemon.next_frame, []

    def next_frame(q, wait):
        clock.n += 1
        return real_next(q, wait)
    monkeypatch.setattr(daemon, "time", clock)
    monkeypatch.setattr(daemon, "next_frame", next_frame)
    monkeypatch.setattr(daemon, "save_event", lambda *a, **kw: calls.append((a[2], kw["motion_wall"])))
    monkeypatch.setattr(daemon, "write_poster", lambda *a: None)
    still, trigger = jpeg_of(), jpeg_of((100, 60, 160, 120))
    run_frames(tmp_path, monkeypatch, [still, still, still, trigger], [])
    for _ in range(100):
        if calls:
            break
        time.sleep(0.05)
    # keyframes at 1010, 1020, 1030, 1040: the pair is 1030 and 1040 (trigger), the mark is at the first of it
    assert calls and calls[0][1] == 1030.0 and calls[0][0][0] == 1029.0


class FlakyQueue:
    """The snapshot queue of the writer thread; the call numbers in full_on find the SD card busy."""

    def __init__(self, full_on=()):
        self.items, self.calls, self.full_on = [], 0, set(full_on)

    def put_nowait(self, item):
        self.calls += 1
        if self.calls in self.full_on:
            raise queue.Full
        self.items.append(item)


def run_snapshots(monkeypatch, frames, snapshots, interval=0):
    """Run the detector over frames with snapshots on and clips off; every frame is due for a snapshot."""
    monkeypatch.setattr(daemon, "STREAM", False)
    monkeypatch.setattr(daemon, "SAVE_FRAMES", True)
    monkeypatch.setattr(daemon, "FRAME_INTERVAL", interval)
    monkeypatch.setattr(daemon, "MOTION_THRESHOLD", 0.01)
    monkeypatch.setattr(daemon, "SNAPSHOTS", snapshots)
    monkeypatch.setattr(daemon, "notify_gallery", lambda name: None)
    monkeypatch.setattr(daemon, "set_error", lambda *a, **kw: None)
    monkeypatch.setattr(daemon.zorcam_config, "probe_camera", lambda *a: "")
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **kw: FakeFfmpeg(frames))
    daemon.STATUS.data["connected_since"] = None
    daemon.run_once()
    return [name for name, _ in snapshots.items]


def test_snapshots_with_motion_get_the_mark_even_without_clips(monkeypatch):
    """A snapshot saved after the detector saw motion (the trigger and the pause after it) is <time>_m.jpg; the
    others keep the plain name. The motion is counted with clips switched off too."""
    still, trigger, after = jpeg_of(), jpeg_of((100, 60, 160, 120)), jpeg_of((140, 60, 200, 120))
    names = run_snapshots(monkeypatch, [still, still, trigger, after, after], FlakyQueue())
    assert [n.endswith("_m") for n in names] == [False, False, True, True, True]
    for n in names:  # the name stays what the gallery and the cloud parse
        when, motion = daemon.zorcam_config.parse_frame_name(n + ".jpg")
        assert motion == n.endswith("_m") and n.startswith(when.strftime("%Y%m%d_%H%M%S_"))


def test_motion_mark_waits_for_the_next_saved_snapshot(monkeypatch):
    """The SD card is busy at the trigger frame (no snapshot): the motion is not forgotten, the next saved snapshot
    has the mark; and the one after it, once the pause is over, does not."""
    still, trigger = jpeg_of(), jpeg_of((100, 60, 160, 120))
    names = run_snapshots(monkeypatch, [still, still, trigger, still], FlakyQueue(full_on={3}))
    assert [n.endswith("_m") for n in names] == [False, False, True]
    monkeypatch.setattr(daemon, "VIDEO_SECONDS", 0)  # the pause is over at once: the following frame is calm again
    names = run_snapshots(monkeypatch, [still, trigger, still, still], FlakyQueue(full_on={2}))
    assert [n.endswith("_m") for n in names] == [False, True, False]



# ---------------------------------------------------------------- motion zones

def cells_of(*painted):
    """144 cells (16×9, row by row) with the given (col, row) painted."""
    cells = ["0"] * 144
    for col, row in painted:
        cells[row * 16 + col] = "1"
    return "".join(cells)


def zone(mode, *painted, cells=None):
    return daemon.zone_mask({"mode": mode, "cells": cells or cells_of(*painted)})


def test_zone_cell_is_10_by_10_pixels_of_the_small_frame():
    mask, area = zone("include", (3, 2))
    assert mask.size == daemon.SAMPLE_SIZE and area == 100
    on = [(x, y) for y in range(90) for x in range(160) if mask.getpixel((x, y))]
    assert min(on) == (30, 20) and max(on) == (39, 29) and len(on) == 100
    mask, area = zone("exclude", (15, 8))  # the last cell: the bottom right corner
    assert area == 143 * 100 and mask.getpixel((159, 89)) == 0 and mask.getpixel((149, 79)) == 255
    assert mask.getpixel((150, 89)) == 0 and mask.getpixel((0, 0)) == 255


def test_no_zones_when_nothing_is_painted_or_the_entry_is_broken():
    assert daemon.zone_mask(None) is None and daemon.zone_mask({}) is None
    assert zone("exclude") is None and zone("include") is None  # empty mask: the mode does not matter
    assert daemon.zone_mask({"mode": "include", "cells": "1" * 100}) is None
    assert daemon.zone_mask({"mode": "nope", "cells": "1" * 144}) is None


def test_motion_score_without_zones_is_as_before():
    a, b = gray(), gray((0, 0, 40, 40))
    from PIL import ImageChops
    total = sum(i * c for i, c in enumerate(ImageChops.difference(a, b).histogram()))
    assert daemon.motion_score(a, b) == total / (255 * 160 * 90) == daemon.motion_score(a, b, None)
    assert daemon.motion_score(a, a) == 0


def test_motion_score_exclude_does_not_count_painted_cells():
    a, b = gray(), gray((0, 0, 20, 20), value=130)  # the change is in the cells (0..1, 0..1)
    plain = daemon.motion_score(a, b)
    assert plain > 0
    assert daemon.motion_score(a, b, zone("exclude", (0, 0), (1, 0), (0, 1), (1, 1))) == 0
    assert daemon.motion_score(a, b, zone("exclude", (5, 5))) > plain  # elsewhere painted: the area is smaller, the score bigger
    ratio = daemon.motion_score(a, b, zone("exclude", (5, 5))) / plain
    assert abs(ratio - 144 / 143) < 1e-9


def test_motion_score_include_counts_only_painted_cells():
    a, b = gray(), gray((0, 0, 20, 20), value=130)
    assert daemon.motion_score(a, b, zone("include", (10, 5))) == 0  # painted, but quiet there
    assert daemon.motion_score(a, b, zone("include", (0, 0), (1, 0), (0, 1), (1, 1))) > 0
    # a change in one of two painted cells (200 px < 5% of the frame): counted from 5%, the rest of the frame does not dilute it
    one = daemon.motion_score(a, gray((0, 0, 10, 10), value=130), zone("include", (0, 0), (9, 8)))
    assert one == pytest.approx(100 * 100 / (255 * 720))


def test_motion_score_area_is_not_smaller_than_5_percent_of_the_frame():
    a, b = gray(), gray((0, 0, 10, 10), value=130)  # 100 pixels differ by 100 levels
    changed = 100 * 100
    narrow = daemon.motion_score(a, b, zone("include", (0, 0)))  # one cell is 100 px < 5% of the frame (720 px)
    assert narrow == pytest.approx(changed / (255 * 0.05 * 160 * 90))
    wide_cells = [(c, r) for c in range(16) for r in range(3)]  # 48 cells = 4800 px > 5%
    wide = daemon.motion_score(a, b, zone("include", *wide_cells))
    assert wide == pytest.approx(changed / (255 * 4800))
    # everything excluded: nothing is counted, no division by zero
    assert daemon.motion_score(a, b, zone("exclude", cells="1" * 144)) == 0


def test_motion_box_stays_out_of_excluded_cells():
    a, b = gray((20, 20, 40, 40)), gray((30, 20, 50, 40))  # the box is x 20..50, y 20..40
    assert daemon.motion_box(a, b) == [0.125, round(20 / 90, 3), round(50 / 160, 3), round(40 / 90, 3)]
    left = [(c, r) for c in (0, 1) for r in range(9)]  # columns 0..1 (x < 20) are excluded: nothing changes
    assert daemon.motion_box(a, b, zone("exclude", *left)) == daemon.motion_box(a, b)
    cut = [(c, r) for c in (2, 3) for r in range(9)]  # x 20..39 excluded: the old place goes, the new one stays
    assert daemon.motion_box(a, b, zone("exclude", *cut)) == [0.25, round(20 / 90, 3), round(50 / 160, 3), round(40 / 90, 3)]
    only = [(c, r) for c in (2, 3) for r in (2, 3)]  # include: only x 20..39, y 20..39: the old place of the square
    assert daemon.motion_box(a, b, zone("include", *only)) == [0.125, round(20 / 90, 3), round(30 / 160, 3), round(40 / 90, 3)]
    assert daemon.motion_box(a, b, zone("include", (10, 8))) is None  # nothing changed in the painted cell


def test_zones_are_reread_only_when_the_file_changes(tmp_path, monkeypatch):
    import zorcam_config
    path = tmp_path / "motion_zones.json"
    monkeypatch.setattr(zorcam_config, "MOTION_ZONES_FILE", str(path))
    now = [100.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now[0])
    zones = daemon.Zones("front")
    assert zones.get() is None  # no file
    zorcam_config.save_zone("front", "include", cells_of((0, 0)))
    assert zones.get() is None  # not checked again so soon
    now[0] += daemon.ZONES_CHECK
    first = zones.get()
    assert first is not None and first[1] == 100
    now[0] += daemon.ZONES_CHECK
    assert zones.get() is first  # the file is the same: the mask is not rebuilt
    zorcam_config.save_zone("other", "include", cells_of((1, 1)))  # another camera's entry: this one is the same file change
    now[0] += daemon.ZONES_CHECK
    assert zones.get()[1] == 100
    zorcam_config.save_zone("front", "exclude", "0" * 144)
    now[0] += daemon.ZONES_CHECK
    assert zones.get() is None
    path.write_text("broken")
    now[0] += daemon.ZONES_CHECK
    assert zones.get() is None
