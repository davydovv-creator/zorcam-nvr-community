"""The recordings disk (app/storage/zorcam_storage.py, run as root): which disks may be used, moving the recordings
from the SD card, requests of the gallery, the state when the disk is gone."""
import json

import pytest

import zorcam_storage

LSBLK = {"blockdevices": [
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "size": 32000000000, "tran": None, "type": "disk", "children": [
        {"name": "mmcblk0p1", "path": "/dev/mmcblk0p1", "type": "part", "mountpoints": ["/boot/firmware"]},
        {"name": "mmcblk0p2", "path": "/dev/mmcblk0p2", "type": "part", "mountpoints": ["/"]}]},
    {"name": "sda", "path": "/dev/sda", "size": 256000000000, "vendor": "Samsung ", "model": "T7 ", "serial": "S1",
     "tran": "usb", "type": "disk", "mountpoints": [None], "children": [
         {"name": "sda1", "path": "/dev/sda1", "type": "part", "fstype": "ext4", "uuid": "u-1", "label": "zorcam",
          "mountpoints": ["/mnt/zorcam-nvr-disk"]}]},
    {"name": "sdb", "path": "/dev/sdb", "size": 64000000000, "vendor": None, "model": "Flash", "serial": "S2",
     "tran": "usb", "type": "disk", "mountpoints": [None], "children": [
         {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "exfat", "uuid": "u-2", "mountpoints": [None]}]},
    {"name": "sdc", "path": "/dev/sdc", "size": 120000000000, "model": "Boot SSD", "tran": "usb", "type": "disk",
     "children": [{"name": "sdc2", "path": "/dev/sdc2", "type": "part", "fstype": "ext4", "uuid": "u-3",
                   "mountpoints": ["/"]}]},
]}


def test_only_usb_disks_without_the_system():
    """The SD card and a USB disk the Pi boots from are never offered; an ext4 disk can be connected as it is."""
    disks = zorcam_storage.usb_disks(LSBLK)
    assert [d["path"] for d in disks] == ["/dev/sda", "/dev/sdb"]
    sda, sdb = disks
    assert sda["model"] == "Samsung T7" and sda["ext4"] == {"path": "/dev/sda1", "uuid": "u-1", "label": "zorcam"}
    assert sda["mounted_here"] and sda["uuids"] == ["u-1"]
    assert sdb["model"] == "Flash" and sdb["ext4"] is None and not sdb["mounted_here"]
    assert zorcam_storage.usb_disks({}) == []


def test_requests_are_checked_again(monkeypatch):
    """A request of the gallery names a disk: here it must be a USB disk without the system, else nothing is run."""
    ran = []
    monkeypatch.setattr(zorcam_storage, "run", lambda cmd, check=True: ran.append(cmd) or "")
    disks = zorcam_storage.usb_disks(LSBLK)
    for path in ("/dev/mmcblk0", "/dev/sdc", "/dev/sdz"):
        with pytest.raises(RuntimeError):
            zorcam_storage.format_disk(path, disks)
    with pytest.raises(RuntimeError):
        zorcam_storage.connect_disk("/dev/sdb", disks)  # exFAT: only formatting
    assert ran == []
    with pytest.raises(RuntimeError):
        zorcam_storage.handle_request({"action": "rm -rf"})


def test_recordings_move_from_the_sd_card(tmp_path):
    """Moving to the disk: file by file, a clip still being written (.part) stays, a name already on the disk is
    not overwritten (the SD copy goes)."""
    sd, disk = tmp_path / "sd", tmp_path / "disk"
    (sd / "cam1_videos").mkdir(parents=True)
    (sd / "cam1").mkdir()
    (sd / "cam1" / "a.jpg").write_bytes(b"a")
    (sd / "cam1_videos" / "b.mp4").write_bytes(b"b")
    (sd / "cam1_videos" / "c.mp4.part").write_bytes(b"c")
    (disk / "cam1").mkdir(parents=True)
    (disk / "cam1" / "a.jpg").write_bytes(b"old")
    secret = tmp_path / "shadow"
    secret.write_bytes(b"secret")
    (sd / "cam1" / "link.jpg").symlink_to(secret)  # planted by a compromised service: root must not read it
    (sd / "evil").symlink_to(tmp_path, target_is_directory=True)
    assert zorcam_storage.move_tree(str(sd), str(disk)) == 1
    assert (disk / "cam1_videos" / "b.mp4").read_bytes() == b"b" and (disk / "cam1" / "a.jpg").read_bytes() == b"old"
    assert not (sd / "cam1" / "a.jpg").exists() and (sd / "cam1_videos" / "c.mp4.part").exists()
    assert not (disk / "cam1" / "link.jpg").exists() and not (disk / "evil").exists()
    (disk / "cam2").symlink_to(tmp_path / "elsewhere", target_is_directory=True)  # a link instead of a folder on the disk
    (sd / "cam2").mkdir()
    (sd / "cam2" / "x.jpg").write_bytes(b"x")
    with pytest.raises(RuntimeError):
        zorcam_storage.move_tree(str(sd), str(disk))


def test_state_files_are_not_written_through_links(tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep")
    path = tmp_path / "state.json"
    (tmp_path / f"state.json.{__import__('os').getpid()}.tmp").symlink_to(target)
    zorcam_storage.write_json(str(path), {"a": 1})
    assert target.read_text() == "keep" and json.loads(path.read_text()) == {"a": 1}
    link = tmp_path / "request.json"
    link.symlink_to(path)
    assert zorcam_storage.read_json(str(link)) is None


def test_a_missing_disk_means_the_sd_card(tmp_path, monkeypatch):
    """No disk chosen: the SD card. A chosen disk that is gone: state "missing" (the gallery shows the problem) and,
    if it was unplugged while connected, the folder is freed so that the recordings go to the SD card."""
    monkeypatch.setattr(zorcam_storage, "DISK_FILE", str(tmp_path / "disk.json"))
    monkeypatch.setattr(zorcam_storage, "STATE_FILE", str(tmp_path / "state.json"))
    ran = []
    monkeypatch.setattr(zorcam_storage, "run", lambda cmd, check=True: ran.append(cmd) or "")
    zorcam_storage.check()
    assert json.loads((tmp_path / "state.json").read_text())["phase"] == "sd" and ran == []
    zorcam_storage.write_json(str(tmp_path / "disk.json"), {"uuid": "u-1", "model": "T7", "size": 1, "serial": ""})
    monkeypatch.setattr(zorcam_storage, "disk_present", lambda uuid: False)
    monkeypatch.setattr(zorcam_storage, "is_mount", lambda path: path == zorcam_storage.RECORDINGS)
    zorcam_storage.check()
    assert json.loads((tmp_path / "state.json").read_text())["phase"] == "missing"
    assert ["umount", "-l", zorcam_storage.RECORDINGS] in ran
    before = (tmp_path / "state.json").stat().st_mtime_ns
    zorcam_storage.check()  # the same state is not written again (no SD write every minute)
    assert (tmp_path / "state.json").stat().st_mtime_ns == before


def mountinfo_line(n, path, options):
    return f"{n} 1 8:1 /recordings {path} {options} shared:1 - ext4 /dev/sda1 rw,commit=60\n"


def fake_mounts(tmp_path, monkeypatch, mounted):
    """mounted: {path: options}; the `mount` and `umount` commands change the fake /proc/self/mountinfo."""
    info = tmp_path / "mountinfo"
    ran = []

    def write():
        info.write_text("".join(mountinfo_line(i, p, o) for i, (p, o) in enumerate(mounted.items())))

    def run(cmd, check=True):
        ran.append(cmd)
        if cmd[0] == "mount":
            opts = cmd[2].split(",") if cmd[1] == "-o" else []
            if "remount" in opts:
                mounted[cmd[-1]] = "rw,relatime," + ",".join(o for o in opts if o not in ("remount", "bind"))
            elif cmd[1] == "--bind":
                mounted[cmd[-1]] = "rw,relatime"
            else:
                mounted[cmd[-1]] = "rw," + ",".join(opts)
            write()
        return ""
    write()
    monkeypatch.setattr(zorcam_storage, "MOUNTINFO", str(info))
    monkeypatch.setattr(zorcam_storage, "run", run)
    monkeypatch.setattr(zorcam_storage, "is_mount", lambda path: path in mounted)
    return ran


def test_mount_options_come_from_mountinfo(tmp_path, monkeypatch):
    fake_mounts(tmp_path, monkeypatch, {"/mnt/d": "rw,nosuid,nodev,noexec,noatime", "/mnt/e": "rw,relatime"})
    assert zorcam_storage.mount_options("/mnt/d") == {"rw", "nosuid", "nodev", "noexec", "noatime"}
    assert zorcam_storage.mount_options("/mnt/e") == {"rw", "relatime"}
    assert zorcam_storage.mount_options("/mnt/none") is None


def test_the_disk_is_mounted_without_devices_setuid_and_exec(tmp_path, monkeypatch):
    """A disk prepared by somebody else may hold a device file of the SD card or a setuid program: the disk and the
    recordings folder on top of the SD card are mounted with nodev,nosuid,noexec."""
    monkeypatch.setattr(zorcam_storage, "MOUNT", str(tmp_path / "disk"))
    monkeypatch.setattr(zorcam_storage, "RECORDINGS", str(tmp_path / "recordings"))
    monkeypatch.setattr(zorcam_storage, "SD_VIEW", str(tmp_path / "sd"))
    monkeypatch.setattr(zorcam_storage, "svc_ids", lambda user=None: None)
    monkeypatch.setattr(zorcam_storage, "set_state", lambda *a, **k: None)
    (tmp_path / "recordings").mkdir()
    mounted = {}
    ran = fake_mounts(tmp_path, monkeypatch, mounted)
    monkeypatch.setattr(zorcam_storage, "is_mount", lambda path: path in mounted and path != str(tmp_path / "sd"))
    zorcam_storage.attach({"uuid": "u-1", "model": "T7"})
    disk_mount = next(c for c in ran if c[:2] == ["mount", "-o"] and "UUID=u-1" in c)
    assert {"nodev", "nosuid", "noexec"} <= set(disk_mount[2].split(","))
    assert ["mount", "-o", "remount,bind,nodev,nosuid,noexec", str(tmp_path / "recordings")] in ran
    assert ran.index(["mount", "--bind", str(tmp_path / "disk" / "recordings"), str(tmp_path / "recordings")]) \
        < ran.index(["mount", "-o", "remount,bind,nodev,nosuid,noexec", str(tmp_path / "recordings")])
    assert {"nodev", "nosuid", "noexec"} <= zorcam_storage.mount_options(str(tmp_path / "recordings"))


def test_a_disk_mounted_by_an_older_version_is_remounted_once(tmp_path, monkeypatch):
    mount, rec = zorcam_storage.MOUNT, zorcam_storage.RECORDINGS
    ran = fake_mounts(tmp_path, monkeypatch, {mount: "rw,noatime", rec: "rw,relatime"})
    zorcam_storage.attach({"uuid": "u-1", "model": "T7"})
    assert ran == [["mount", "-o", "remount,nodev,nosuid,noexec", mount],
                   ["mount", "-o", "remount,bind,nodev,nosuid,noexec", rec]]
    del ran[:]
    zorcam_storage.attach({"uuid": "u-1", "model": "T7"})  # the check every minute: nothing to do
    assert ran == []


def test_disks_are_listed_without_the_device_nodes():
    """The gallery runs with PrivateDevices: lsblk reads /sys, but may find no node in /dev and leave PATH empty."""
    bare = json.loads(json.dumps(LSBLK))
    for dev in bare["blockdevices"]:
        dev.pop("path", None)
        for part in dev.get("children") or []:
            part.pop("path", None)
    assert zorcam_storage.usb_disks(bare) == zorcam_storage.usb_disks(LSBLK)
