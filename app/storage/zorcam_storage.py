#!/usr/bin/env python3
"""Записи на USB-диск (сервис zorcam-nvr-storage, от root).

Диск форматируется в ext4 и монтируется в /mnt/zorcam-nvr-disk; его папка recordings подключается (bind) поверх
/var/lib/zorcam-nvr/recordings — детектор и галерея пишут и читают там же, где всегда. Диска нет (выдернули,
отказал) — записи идут в ту же папку на SD-карте; диск вернулся —
сервис сам подключает его и переносит накопленное на SD. Галерея показывает проблему «Диск для записей».

Запускается раз в минуту (таймер), при загрузке — до детектора и галереи, и по заявке галереи (path-юнит):
галерея без прав root только кладёт заявку в /var/lib/zorcam-nvr/storage/request.json, здесь она проверяется
заново — форматируется только USB-диск, на котором нет системы. Что выбрано и что сейчас (disk.json, state.json) —
в папке root (/var/lib/zorcam-nvr-storage): сервисы zorcam-nvr её только читают. Всё, что может подложить
zorcam-nvr (ссылки в записях, в заявке), root не открывает по ссылке.

    zorcam_storage.py run       # заявка (если есть) и проверка подключения
    zorcam_storage.py list      # USB-диски, JSON
"""
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time

STATE_DIR = os.environ.get("ZORCAM_STATE_DIR", "/var/lib/zorcam-nvr")
ROOT_DIR = os.environ.get("ZORCAM_STORAGE_DIR", "/var/lib/zorcam-nvr-storage")  # root:zorcam-nvr 750
DISK_FILE = os.path.join(ROOT_DIR, "disk.json")  # какой диск выбран: uuid, модель, размер
STATE_FILE = os.path.join(ROOT_DIR, "state.json")  # что сейчас: для галереи
STORAGE_DIR = os.path.join(STATE_DIR, "storage")  # zorcam-nvr: сюда галерея кладёт заявку
REQUEST_FILE = os.path.join(STORAGE_DIR, "request.json")  # заявка галереи: use, connect (диск) или forget
RECORDINGS = os.path.join(STATE_DIR, "recordings")
MOUNT = os.environ.get("ZORCAM_STORAGE_MOUNT", "/mnt/zorcam-nvr-disk")
SD_VIEW = "/run/zorcam-nvr-sd-recordings"  # папка записей на SD, пока поверх неё подключён диск: перенос
SVC_USER = "zorcam-nvr"
SYSTEM_MOUNTS = ("/", "/boot", "/boot/firmware")  # диск с такой точкой монтирования — системный, не трогать
LSBLK = ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,MODEL,VENDOR,SERIAL,TRAN,TYPE,UUID,FSTYPE,LABEL,MOUNTPOINTS"]


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def run(cmd, check=True):
    """Команда; check=False — необязательный шаг: его ошибка (и отсутствие самой программы) не останавливает."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        if check:
            raise RuntimeError(f"{cmd[0]} is not installed") from None
        return ""
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def read_json(path):
    """JSON-файл; ссылка (её мог подложить zorcam-nvr) не открывается."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data, mode=0o644):
    """Через временный файл: создаётся заново (O_EXCL), не по ссылке, права — у открытого файла."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.fchmod(f.fileno(), mode)
        json.dump(data, f)
    os.replace(tmp, path)


def mountpoints(dev):
    return [m for m in (dev.get("mountpoints") or []) if m]


def usb_disks(lsblk_json):
    """USB-диски из вывода lsblk: целиком диск (TYPE disk, TRAN usb), без системных разделов (/, /boot) — Pi может
    загружаться и с USB. Мы сами смонтированный диск тоже в списке (mounted_here)."""
    disks = []
    for dev in (lsblk_json or {}).get("blockdevices", []):
        if dev.get("type") != "disk" or (dev.get("tran") or "") != "usb":
            continue
        parts = [dev] + list(dev.get("children") or [])
        mounts = [m for p in parts for m in mountpoints(p)]
        if any(m in SYSTEM_MOUNTS for m in mounts):
            continue
        disks.append({"path": dev["path"], "size": int(dev.get("size") or 0),
                      "model": " ".join(x for x in ((dev.get("vendor") or "").strip(), (dev.get("model") or "").strip())
                                        if x) or dev["name"],
                      "serial": (dev.get("serial") or "").strip(),
                      "uuids": [p["uuid"] for p in parts if p.get("uuid")],
                      # раздел ext4 (диск уже готовили для записей, в том числе на другой Zorcam): подключается
                      # без форматирования
                      "ext4": next(({"path": p["path"], "uuid": p["uuid"], "label": p.get("label") or ""}
                                    for p in parts if p.get("fstype") == "ext4" and p.get("uuid")), None),
                      "mounted_here": MOUNT in mounts})
    return disks


def list_disks():
    return usb_disks(json.loads(run(LSBLK)))


def set_state(phase, message="", **extra):
    """Состояние для галереи; то же самое не переписывается (проверка раз в минуту — не запись на SD каждую минуту)."""
    state = {"phase": phase, "message": message, **extra}
    old = read_json(STATE_FILE) or {}
    old.pop("time", None)
    if old != state:
        write_json(STATE_FILE, {**state, "time": time.time()})


def is_mount(path):
    return os.path.ismount(path)


def disk_present(uuid):
    return os.path.exists(f"/dev/disk/by-uuid/{uuid}")


def svc_ids(user=SVC_USER):
    try:
        pw = pwd.getpwnam(user)
        return pw.pw_uid, pw.pw_gid
    except KeyError:
        return None


def safe_dir(path, ids):
    """Папка для записей на диске: создать, если нет; ссылка (её мог подложить zorcam-nvr) — отказ."""
    try:
        os.mkdir(path, 0o750)
        if ids:
            os.lchown(path, *ids)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise RuntimeError(f"{path} is not a folder")


def move_tree(src, dst, ids=None):
    """Перенести записи с SD на диск: только обычные файлы и папки (ссылки — нет: root не читает по ним),
    недописанное (.part) остаётся, совпадающие имена не перезаписываются (копия с SD удаляется). Файл
    копируется сам, без следования ссылкам, и сразу получает владельца zorcam-nvr. Возвращает число файлов."""
    moved = 0
    for root, dirs, files in os.walk(src):  # по ссылкам на папки os.walk не заходит
        rel = os.path.relpath(root, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        safe_dir(target, ids)
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            src_path, dst_path = os.path.join(root, name), os.path.join(target, name)
            st = os.lstat(src_path)
            if name.endswith(".part") or not stat.S_ISREG(st.st_mode):
                continue
            if os.path.lexists(dst_path):
                os.remove(src_path)
                continue
            fd_in = os.open(src_path, os.O_RDONLY | os.O_NOFOLLOW)
            fd_out = os.open(dst_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, stat.S_IMODE(st.st_mode))
            with os.fdopen(fd_in, "rb") as fin, os.fdopen(fd_out, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
                if ids:
                    os.fchown(fout.fileno(), *ids)
            os.utime(dst_path, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)  # сроки хранения — по mtime
            os.remove(src_path)
            moved += 1
    return moved


def plain_dir(path):
    """Папка, а не ссылка: поверх ссылки root смонтировал бы диск куда она указывает."""
    return stat.S_ISDIR(os.lstat(path).st_mode)


def attach(disk):
    """Подключить выбранный диск: смонтировать, подключить его recordings поверх папки на SD, перенести накопленное
    на SD. Детектор после этого перезапускается: открытые им папки — уже на диске."""
    if not is_mount(MOUNT):
        os.makedirs(MOUNT, exist_ok=True)
        run(["mount", "-o", "noatime,commit=60", f"UUID={disk['uuid']}", MOUNT])
        log(f"mounted the disk {disk['model']} at {MOUNT}")
    on_disk = os.path.join(MOUNT, "recordings")
    if is_mount(RECORDINGS):
        return 0
    if not plain_dir(RECORDINGS):
        raise RuntimeError(f"{RECORDINGS} is not a folder")
    ids = svc_ids()
    safe_dir(on_disk, ids)
    os.makedirs(SD_VIEW, exist_ok=True)
    run(["mount", "--bind", RECORDINGS, SD_VIEW])  # вид на папку на SD, пока её закрывает диск
    try:
        run(["mount", "--bind", on_disk, RECORDINGS])
        log(f"recordings are on the disk now ({on_disk})")
        set_state("moving", disk=disk)
        moved = move_tree(SD_VIEW, on_disk, ids)
        if moved:
            log(f"moved {moved} files from the SD card to the disk")
    finally:
        run(["umount", SD_VIEW], check=False)
        try:
            os.rmdir(SD_VIEW)
        except OSError:
            pass
    run(["systemctl", "try-restart", "zorcam-nvr-capture"], check=False)
    return moved


def detach():
    """Перестать писать на диск: отключить его от папки записей и размонтировать; записи на диске остаются, новые
    идут на SD."""
    if is_mount(RECORDINGS):
        run(["umount", RECORDINGS])
    if is_mount(MOUNT):
        run(["umount", MOUNT])
    run(["systemctl", "try-restart", "zorcam-nvr-capture"], check=False)


def format_disk(path, disks):
    """Один раздел ext4 на весь диск. path — только из списка USB-дисков (заново по lsblk, не из заявки)."""
    disk = next((d for d in disks if d["path"] == path), None)
    if disk is None:
        raise RuntimeError(f"{path} is not a USB disk without the system")
    if disk["mounted_here"]:
        detach()
    for dev in json.loads(run(LSBLK)).get("blockdevices", []):  # смонтированное системой (автомонтирование) — снять
        if dev.get("path") == path:
            for part in dev.get("children") or []:
                for m in mountpoints(part):
                    run(["umount", m], check=False)
    run(["wipefs", "-a", path])
    p = subprocess.run(["sfdisk", "--quiet", path], input="label: dos\ntype=83\n", capture_output=True, text=True,
                       timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"sfdisk {path}: {p.stderr.strip()[-300:]}")
    run(["blockdev", "--rereadpt", path], check=False)
    part = None
    for _ in range(15):  # раздел появляется не сразу: ядро перечитывает таблицу, udev создаёт устройство
        run(["udevadm", "settle"], check=False)
        part = next((p["path"] for d in json.loads(run(LSBLK)).get("blockdevices", []) if d.get("path") == path
                     for p in d.get("children") or []), None)
        if part and os.path.exists(part):
            break
        time.sleep(1)
    if not part:
        raise RuntimeError(f"no partition on {path} after partitioning")
    run(["mkfs.ext4", "-F", "-q", "-L", "zorcam", "-m", "0", part])
    run(["udevadm", "settle"], check=False)
    uuid = run(["blkid", "-s", "UUID", "-o", "value", part]).strip()
    if not uuid:
        raise RuntimeError(f"no UUID of {part}")
    return {"uuid": uuid, "model": disk["model"], "size": disk["size"], "serial": disk["serial"]}


def connect_disk(path, disks):
    """Подключить без форматирования: только USB-диск без системы и только его раздел ext4."""
    disk = next((d for d in disks if d["path"] == path), None)
    if disk is None or not disk["ext4"]:
        raise RuntimeError(f"{path} is not a USB disk with an ext4 partition")
    for dev in json.loads(run(LSBLK)).get("blockdevices", []):  # смонтированное системой (автомонтирование) — снять
        if dev.get("path") == path:
            for part in dev.get("children") or []:
                for m in mountpoints(part):
                    if m != MOUNT:
                        run(["umount", m], check=False)
    return {"uuid": disk["ext4"]["uuid"], "model": disk["model"], "size": disk["size"], "serial": disk["serial"]}


def handle_request(req):
    action = (req or {}).get("action")
    if action == "connect":
        if read_json(DISK_FILE):
            detach()
        disk = connect_disk(req.get("disk"), list_disks())
        write_json(DISK_FILE, disk)
        log(f"the disk {disk['model']} (UUID {disk['uuid']}) is connected for the recordings, not formatted "
            f"(asked by {req.get('user', '?')})")
    elif action == "use":
        if read_json(DISK_FILE):
            detach()
        set_state("formatting", disk={"path": req.get("disk")})
        log(f"formatting {req.get('disk')} for the recordings (asked by {req.get('user', '?')})")
        disk = format_disk(req.get("disk"), list_disks())
        write_json(DISK_FILE, disk)
        log(f"the disk {disk['model']} (UUID {disk['uuid']}) is for the recordings now")
    elif action == "forget":
        detach()
        try:
            os.remove(DISK_FILE)
        except OSError:
            pass
        log("the recordings are on the SD card again; the disk keeps what was recorded on it")
    else:
        raise RuntimeError(f"unknown request: {action!r}")


def check():
    """Раз в минуту: выбранный диск подключён, если он вставлен."""
    disk = read_json(DISK_FILE)
    if not disk:
        set_state("sd")
        return
    if not disk_present(disk["uuid"]):
        if is_mount(RECORDINGS):  # выдернули на ходу: отключить, чтобы писать на SD, а не в пустоту
            run(["umount", "-l", RECORDINGS], check=False)
            run(["umount", "-l", MOUNT], check=False)
            run(["systemctl", "try-restart", "zorcam-nvr-capture"], check=False)
            log("the recordings disk is gone: recording to the SD card")
        set_state("missing", disk=disk)
        return
    attach(disk)
    set_state("disk", disk=disk)


def main():
    if sys.argv[1:2] == ["list"]:
        print(json.dumps(list_disks(), indent=1))
        return 0
    if sys.argv[1:2] != ["run"]:
        print(__doc__)
        return 2
    os.makedirs(ROOT_DIR, exist_ok=True)
    req = read_json(REQUEST_FILE)
    if os.path.lexists(REQUEST_FILE):
        os.remove(REQUEST_FILE)
    try:
        if req:
            handle_request(req)
        check()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
        log(f"error: {e}")
        set_state("error", str(e), disk=read_json(DISK_FILE))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
