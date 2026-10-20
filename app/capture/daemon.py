#!/usr/bin/env python3
"""Детектор движения Zorcam (сервис zorcam-nvr-capture).

Без аргументов — надзиратель: запускает по процессу на каждую камеру из cameras.json (daemon.py --camera ID),
перезапускает упавшие и зависшие, пингует systemd watchdog; при изменении настроек выходит, systemd
перезапускает всё с новыми. Отдельный процесс на камеру: зависшее подключение одной камеры не держит остальные.
"""
import collections
import ctypes
import io
import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))  # запуск из репозитория
import zorcam_config  # noqa: E402
from zorcam_i18n import Msg  # noqa: E402

CFG = zorcam_config.load()
CAMERA_ID = sys.argv[sys.argv.index("--camera") + 1] if "--camera" in sys.argv[:-1] else None  # None — надзиратель
CAM = zorcam_config.get_camera(CAMERA_ID) if CAMERA_ID else None
# адрес и логин из cameras.json, пароль из secrets.env; только для запроса DESCRIBE к самой камере
RTSP_URL = zorcam_config.rtsp_url(CAM) if CAM else ""
SOURCE_URL = zorcam_config.local_rtsp_url(CAMERA_ID or "")  # через go2rtc: пароль не попадает в командную строку ffmpeg
# второй, облегчённый поток камеры (640×360): по нему работает детектор — и снимки, постеры, ролики из ключевых
# кадров; Live и «Поток» берут основной. Декодировать кадр в разы дешевле: 5 камер помещаются в память Pi 3
HAS_SUB = bool(CAM and CAM["rtsp_sub_url"])
SUB_URL = zorcam_config.local_rtsp_url(CAMERA_ID or "", sub=True)
SUB_FAILS = 3  # столько подключений подряд без кадров по второму потоку — детектор переходит на основной
SUB_RETRY = 1800  # сек: через столько снова пробует второй поток
CONFIG_SIG = zorcam_config.signature()  # изменились настройки или камеры — выйти, systemd перезапустит с новыми
VIDEO_SECONDS = CAM["clip_seconds"] if CAM else 10
SAVE_VIDEOS = CAM["save_videos"] if CAM else True  # выключено — движение и push остаются, роликов нет
SAVE_FRAMES = CAM["save_frames"] if CAM else True
# ролик — видео камеры как есть (полная частота кадров), склеенное без перекодирования из кусков, которые
# отдельный ffmpeg всё время пишет в /dev/shm; на Pi ролики не кодируются
STREAM = SAVE_VIDEOS and bool(CAM)
SEGMENT_DIR = f"/dev/shm/zorcam_rec_{CAMERA_ID}"
SEGMENT_SECONDS = 2  # длина куска: ffmpeg режет по ключевым кадрам, не короче этого
PRE_SECONDS = 3  # «Поток»: ролик начинается раньше движения
# куски хранятся столько секунд: ролик склеивается, когда он закончился, из кусков с его начала (и с запасом)
SEGMENT_KEEP = PRE_SECONDS + VIDEO_SECONDS + 4 * SEGMENT_SECONDS + 20
PART = ".part"  # ролик, который ещё пишется
# ролик, который не удалось сохранить: вместо .mp4 — метка <имя>.failed, на постере — надпись (всегда по-английски);
# на Pi ничего не кодируется
FAILED = ".failed"
FAILED_TEXT = "Could not save the video"
# склейка роликов — с низшим приоритетом: детекторам и Live процессор нужнее
ENCODE_NICE = 19
KEYFRAME_WINDOW = 20  # по стольким последним ключевым кадрам считается их частота (в «Статусе»)
MOTION_THRESHOLD = CFG.get("capture", "motion_threshold")
STALL_TIMEOUT = max(3, CFG.get("capture", "stall_timeout"))  # нет кадров — переподключение
STALL_MAX = 60  # сек: порог «нет кадров» у камеры с редкими ключевыми кадрами — не больше
RETRY_MIN, RETRY_MAX = 3, 60  # сек: пауза перед переподключением; растёт вдвое после каждой неудачной попытки
GOOD_RUN = 60  # сек: столько кадры шли — подключение удачное, пауза снова RETRY_MIN
RTSP_TIMEOUT = max(1, CFG.get("capture", "rtsp_timeout"))
# на SD-карту — не чаще одного кадра за столько секунд; движение и ролики — по всем ключевым кадрам в памяти
FRAME_INTERVAL = CAM["frames_interval"] if CAM else 5
SNAPSHOTS = queue.Queue(maxsize=2)  # снимки в очереди на запись; карта не успевает — новые пропускаются

RAW_DIR, VIDEO_DIR = zorcam_config.data_dirs(CAMERA_ID or "")  # /var/lib/zorcam-nvr/recordings/<id>, <id>_videos

GALLERY_MOTION_URL = "http://127.0.0.1:80/internal/motion"  # галерея рассылает push-уведомления
SAMPLE_SIZE = (160, 90)
# рамка области движения на постере: пиксель малого кадра «изменился», если разница не меньше BOX_THRESHOLD (из 255);
# рамка на весь кадр (смена освещения, переключение ИК-подсветки) ничего не показывает — её нет
BOX_THRESHOLD = 28
BOX_LUT = [255 if v >= BOX_THRESHOLD else 0 for v in range(256)]
BOX_WHOLE = 0.95
ZONES_CHECK = 3  # сек: как часто процесс камеры смотрит, не изменился ли motion_zones.json
ZONE_MIN_AREA = 0.05  # доля кадра: порог движения считается от площади считаемых клеток, но не от меньшей
# состояние детектора камеры для галереи (мониторинг, страница «Настройки»), в оперативной памяти
STATUS_FILE = zorcam_config.capture_status_file(CAMERA_ID or "")
STATUS_INTERVAL = 5
DEBUG = os.environ.get("ZORCAM_DEBUG") == "1"  # app/debug.sh on: предупреждения ffmpeg и статистика раз в минуту
HUNG_AFTER = 60  # процесс камеры не обновлял файл состояния столько секунд — перезапустить
RESTART_DELAY = 5
MEMORY_LOG_INTERVAL = 1800  # сек: надзиратель пишет в журнал память процессов камер и их ffmpeg
CHILD_ENV = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}  # watchdog пингует только надзиратель


def log(msg):
    """Строка в журнал одной записью: print пишет текст и перевод строки отдельно, и строки потоков
    (кодирование роликов, запись снимков, заглушки) склеивались."""
    who = f" [{CAMERA_ID}]" if CAMERA_ID else ""
    sys.stdout.write(f"[{datetime.now().isoformat(timespec='seconds')}]{who} {msg}\n")
    sys.stdout.flush()


def limit_malloc():
    """Память процесса — назад системе: glibc заводит арену на каждый поток (до 8 на ядро), а порог mmap поднимает
    после первого освобождённого большого блока — дальше кадры и куски роликов оседают в аренах, которые не
    возвращаются, и anon процессов камер рос часами. Две арены и постоянный порог: большие блоки — mmap, и сразу
    назад. Только для Python: ffmpeg получает своё окружение и распределитель как есть."""
    try:
        libc = ctypes.CDLL(None)
        libc.mallopt(-8, 2)  # M_ARENA_MAX
        libc.mallopt(-3, 128 * 1024)  # M_MMAP_THRESHOLD: заданный порог не растёт
    except (OSError, AttributeError):
        pass


def anon_mb(pid, proc="/proc"):
    """Своя (анонимная) память процесса в МБ — то, что растёт при утечке; общие библиотеки не считаются."""
    try:
        with open(f"{proc}/{pid}/status") as f:
            for line in f:
                if line.startswith("RssAnon:"):
                    return round(int(line.split()[1]) / 1024)
    except (OSError, ValueError, IndexError):
        pass
    return 0


def children(proc="/proc"):
    """{родитель: [(pid, роль)]} всех процессов; роль ffmpeg — по его командной строке."""
    found = collections.defaultdict(list)
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        try:
            with open(f"{proc}/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            with open(f"{proc}/{pid}/cmdline", "rb") as f:
                cmd = f.read().split(b"\0")
        except (OSError, ValueError, IndexError):
            continue
        role = ("recorder" if b"segment" in cmd else "detector" if b"mjpeg" in cmd else
                os.path.basename(cmd[0].decode(errors="replace")) or "?")
        found[ppid].append((int(pid), role))
    return found


def memory_line(workers, proc="/proc"):
    """Строка журнала: память надзирателя, каждого процесса камеры и его ffmpeg — видно, что из них растёт."""
    tree = children(proc)
    total = anon_mb(os.getpid(), proc)
    parts = [f"supervisor {total}"]
    for cam_id, (worker, _started) in sorted(workers.items()):
        mine = [("python", worker.pid)] + [(role, pid) for pid, role in tree.get(worker.pid, [])]
        sizes = [(role, anon_mb(pid, proc)) for role, pid in mine]
        total += sum(mb for _, mb in sizes)
        parts.append(f"{cam_id}: " + ", ".join(f"{role} {mb}" for role, mb in sizes))
    return "memory (anon, MB): " + " | ".join(parts) + f" | total {total}"


def stall_timeout(gap, first=False):
    """Сколько ждать кадра до переподключения: stall_timeout, но не меньше трёх интервалов между ключевыми кадрами
    камеры (gap — последний измеренный; у камеры с I-кадром раз в 10 с иначе любая задержка сети — «нет кадров»).
    first — кадров в этом подключении ещё не было: STALL_MAX. На нагруженной Pi ffmpeg детектора запускается
    и подключается к go2rtc десятки секунд (после загрузки — 26 с), и за 15 с первый кадр не успевал:
    переподключение по кругу, камера без кадров по полчаса, хотя Live работает."""
    if first:
        return STALL_MAX
    return min(STALL_MAX, max(STALL_TIMEOUT, round(3 * gap))) if gap else STALL_TIMEOUT


def retry_delay(delay, streamed):
    """Пауза перед следующим подключением: RETRY_MIN после удачного (кадры шли GOOD_RUN), иначе вдвое дольше
    прошлой, до RETRY_MAX — недоступная камера не плодит ffmpeg и проверки камеры каждые несколько секунд."""
    return RETRY_MIN if streamed >= GOOD_RUN else min(RETRY_MAX, delay * 2)


class Status:
    """Файл состояния для галереи и пинги watchdog для systemd."""

    def __init__(self):
        self.data = {"pid": os.getpid(), "camera": CAMERA_ID, "started": time.time(), "state": "starting",
                     "last_frame": None, "keyframe_gap": None, "detector_stream": "main",
                     "connected_since": None, "reconnects": [], "last_error": None, "last_error_at": None,
                     "config_errors": [e.to_dict() for e in CFG.errors],
                     "last_error_msg": None}
        self.written = 0.0

    def update(self, force=False, **kw):
        """Файл состояния — ещё и признак жизни для надзирателя (HUNG_AFTER)."""
        if os.getppid() != PARENT_PID:  # надзиратель погиб (например, SIGKILL от watchdog) — не оставаться сиротой
            log("the supervisor is gone, exiting")
            os._exit(0)
        self.data.update(kw)
        now = time.time()
        if force or now - self.written >= STATUS_INTERVAL:
            day_ago = now - 86400
            self.data["reconnects"] = [t for t in self.data["reconnects"] if t > day_ago]
            tmp = STATUS_FILE + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(self.data, f)
                os.replace(tmp, STATUS_FILE)
            except OSError as e:
                log(f"status write error: {e}")
            self.written = now


PARENT_PID = os.getppid()
STATUS = Status()


def small_gray(jpg_bytes):
    with Image.open(io.BytesIO(jpg_bytes)) as img:
        img.draft("L", SAMPLE_SIZE)  # JPEG декодируется сразу в 1/8 размера — в разы дешевле полного декода
        return img.convert("L").resize(SAMPLE_SIZE)


def zone_mask(zone):
    """Зоны движения камеры ({"mode", "cells"} из motion_zones.json) → (маска «L» 160×90: 255 — клетка считается,
    площадь считаемых клеток в пикселях) или None, если зон нет: нет закрашенных клеток или битая запись.
    Клетка сетки 16×9 — 10×10 пикселей малого кадра: маска строится из 16×9 увеличением без сглаживания."""
    if not zone or not zorcam_config.valid_zone(zone.get("mode"), zone.get("cells")) or "1" not in zone["cells"]:
        return None
    painted = zone["mode"] == "include"  # в include считаются закрашенные клетки, в exclude — остальные
    grid = Image.new("L", (zorcam_config.ZONE_COLS, zorcam_config.ZONE_ROWS))
    grid.putdata([255 if (c == "1") == painted else 0 for c in zone["cells"]])
    cell_w, cell_h = SAMPLE_SIZE[0] // zorcam_config.ZONE_COLS, SAMPLE_SIZE[1] // zorcam_config.ZONE_ROWS
    counted = zone["cells"].count("1" if painted else "0")
    return grid.resize(SAMPLE_SIZE, Image.NEAREST), counted * cell_w * cell_h


class Zones:
    """Маска зон процесса камеры: motion_zones.json проверяется не чаще раза в ZONES_CHECK секунд (по mtime и размеру),
    маска перестраивается только при смене файла. Нет файла, нет камеры или битая запись — зон нет."""

    def __init__(self, cam_id):
        self.cam_id, self.key, self.checked, self.zone = cam_id, False, 0.0, None

    def get(self):
        now = time.monotonic()
        if now - self.checked < ZONES_CHECK:
            return self.zone
        self.checked = now
        try:
            st = os.stat(zorcam_config.MOTION_ZONES_FILE)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key != self.key:
            self.key = key
            zone = zone_mask(zorcam_config.load_zones().get(self.cam_id)) if key else None
            if zone or self.zone:
                log("motion zones: " + (f"{zone[1] // 100} of {zorcam_config.ZONE_COLS * zorcam_config.ZONE_ROWS} cells count"
                                        if zone else "none"))
            self.zone = zone
        return self.zone


def motion_score(prev_small, curr_small, zone=None):
    """Доля изменений между малыми кадрами (0..1). С зоной (zone_mask) — только по считаемым клеткам, от их площади,
    но не меньше ZONE_MIN_AREA кадра: узкая зона не делает детектор чувствительнее в разы."""
    diff = ImageChops.difference(prev_small, curr_small)
    area = SAMPLE_SIZE[0] * SAMPLE_SIZE[1]
    if zone:
        diff = ImageChops.multiply(diff, zone[0])
        area = max(zone[1], ZONE_MIN_AREA * area)
    hist = diff.histogram()
    total = sum(i * c for i, c in enumerate(hist))
    return total / (255 * area)


def motion_box(before, after, zone=None):
    """Область движения между двумя малыми кадрами: [x0, y0, x1, y1] в долях кадра (0..1, до 3 знаков) или None.
    Изменившиеся пиксели (BOX_THRESHOLD), очищенные от одиночного шума открытием 3×3 (MinFilter, затем MaxFilter:
    пятно меньше трёх пикселей пропадает, остальные не меняются), и их общая граница."""
    mask = ImageChops.difference(before, after).point(BOX_LUT)
    if zone:  # рамка не заходит в клетки, которые не считаются
        mask = ImageChops.multiply(mask, zone[0])
    bbox = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3)).getbbox()
    if not bbox:
        return None
    w, h = SAMPLE_SIZE
    x0, y0, x1, y1 = bbox
    if x1 - x0 >= BOX_WHOLE * w and y1 - y0 >= BOX_WHOLE * h:
        return None
    return [round(x0 / w, 3), round(y0 / h, 3), round(x1 / w, 3), round(y1 / h, 3)]


def keyframe_rate(times):
    """Ключевых кадров в секунду по времени их прихода: 1 / медиана интервалов (устойчиво к рывкам сети)."""
    times = list(times)
    gaps = sorted(b - a for a, b in zip(times[:-1], times[1:], strict=True))
    gap = gaps[len(gaps) // 2] if gaps else 0
    return round(1 / gap, 2) if gap > 0 else None


def mp4_complete(path):
    """Есть ли в .mp4 индекс (moov): без него ролик не открывается — ffmpeg не успел дописать файл."""
    try:
        with open(path, "rb") as f:
            pos = 0
            for _ in range(32):  # верхний уровень: ftyp, moov, mdat… — по заголовкам, без чтения данных
                f.seek(pos)
                head = f.read(8)
                if len(head) < 8:
                    return False
                size, kind = struct.unpack(">I4s", head)
                if kind == b"moov":
                    return True
                if size == 1:  # 64-битный размер следом
                    size = struct.unpack(">Q", f.read(8))[0]
                if size < 8:  # 0 — «до конца файла»: moov после него уже нет
                    return False
                pos += size
    except (OSError, struct.error):
        pass
    return False


def failed_frame(poster):
    """Постер, затемнённый, с надписью FAILED_TEXT по центру — кадр заглушки и её обложка."""
    img = Image.eval(Image.open(io.BytesIO(poster)).convert("RGB"), lambda v: v * 2 // 5)
    size = max(12, img.height // 10)  # читается и на превью 320×180 в сетке
    ImageDraw.Draw(img).text((img.width / 2, img.height / 2), FAILED_TEXT, font=ImageFont.load_default(size),
                             fill="white", anchor="mm")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def drop_cache(*paths):
    """Записанный файл — на карту и из кэша страниц. Кэш записанных снимков и роликов ядро засчитывает сервису:
    он дорастал до MemoryHigh (при каждом снимке), и ядро, освобождая его при медленной SD-карте, тормозило
    детекторы. Файл нужен только галерее и только при просмотре — она прочитает его с карты."""
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fdatasync(fd)  # грязные страницы не выбрасываются, пока не записаны
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        finally:
            os.close(fd)


def save_failed(poster, name):
    """Ролик не сохранился: постер с надписью FAILED_TEXT (обложка в сетке «Видео») и метка <имя>.failed — галерея
    показывает его как несохранённый, а не «готовится». Недописанный .mp4 удаляется."""
    poster_path = os.path.join(VIDEO_DIR, name + ".jpg")
    for path in (os.path.join(VIDEO_DIR, name + ".mp4"), os.path.join(VIDEO_DIR, name + ".mp4" + PART)):
        try:
            os.remove(path)
        except OSError:
            pass
    with open(poster_path + PART, "wb") as f:
        f.write(failed_frame(poster))
    os.replace(poster_path + PART, poster_path)
    open(os.path.join(VIDEO_DIR, name + FAILED), "w").close()
    drop_cache(poster_path)
    log(f"clip not saved: {name}")


def write_meta(name, box, motion_at):
    """<имя>.json рядом с постером — для галереи: рамка области движения на постере (box — доли кадра, или null) и
    время движения в ролике (motion_at — секунды от его начала, или null: ролик ещё не склеен). Десятки байт;
    через временный файл, как постер."""
    path = os.path.join(VIDEO_DIR, name + ".json")
    try:
        with open(path + PART, "w") as f:
            json.dump({"box": box, "motion_at": motion_at}, f)
        os.replace(path + PART, path)
    except OSError as e:
        log(f"meta error for {name}: {e}")


def write_poster(frame, name, box=None):
    """Постер ролика — кадр сразу после срабатывания: пока ролик пишется и кодируется, галерея показывает его
    вместо ролика. Через временный файл: галерея не прочитает недописанный JPEG. Есть рамка — и <имя>.json
    (если save_event его уже записал, с временем движения, он не перезаписывается)."""
    path = os.path.join(VIDEO_DIR, name + ".jpg")
    try:
        with open(path + PART, "wb") as f:  # недописанный остаток уберёт recover_clips
            f.write(frame)
        os.replace(path + PART, path)
        drop_cache(path)
    except OSError as e:
        log(f"poster error for {name}: {e}")
        return
    if box and not os.path.exists(os.path.join(VIDEO_DIR, name + ".json")):
        write_meta(name, box, None)


def save_event(poster, name, window, box=None, motion_wall=None, poster_thread=None):
    """Ролик движения: видео камеры за window — (начало, конец) по часам — из кусков потока, без перекодирования.
    Постер (poster) — кадр срабатывания, уже записан (write_poster; не вышло — пишется здесь); ролик пишется
    во временный файл и переименовывается в .mp4 только целиком (до этого
    галерея показывает постер с надписью «готовится»). motion_wall — с какого момента по часам показывать рамку (первый
    кадр сравнения): в <имя>.json
    оно записывается как секунды от начала ролика (раньше .mp4: открытый ролик уже с метаданными). poster_thread —
    поток, пишущий постер: поток оборвался сразу после кадра постера, и запасной постер не должен затереть его.
    Не вышло (нет кусков: запись потока не работала) — заглушка с надписью; процесс остановили посреди склейки —
    её сделает recover_clips."""
    poster_path = os.path.join(VIDEO_DIR, name + ".jpg")
    video_path = os.path.join(VIDEO_DIR, name + ".mp4")
    try:
        if poster_thread is not None:
            poster_thread.join(10)
        if not os.path.exists(poster_path):  # при движении записать не удалось
            with open(poster_path, "wb") as f:
                f.write(poster)
        try:
            n, first = stream_clip(window[0], window[1], video_path + PART)
            motion_at = None if motion_wall is None else round(max(0.0, motion_wall - first), 1)
            if box or motion_at is not None:
                write_meta(name, box, motion_at)
            os.replace(video_path + PART, video_path)
            drop_cache(video_path, poster_path)
            log(f"event saved: {name}.mp4 (stream, {n} segments)")
            return
        except Exception as e:
            log(f"stream clip error for {name}: {e}")
        save_failed(poster, name)
    except Exception as e:
        log(f"placeholder error for {name}: {e}")
        for path in (video_path + PART, poster_path):
            try:
                os.remove(path)
            except OSError:
                pass


def recover_clips(names):
    """Ролики, которые прошлый процесс камеры не дописал (его остановили посреди склейки, или файл без индекса), —
    несохранённые: постер с надписью и метка. names — содержимое папки роликов при запуске процесса: работает
    в фоне, пока детектор уже смотрит, и новые ролики этого процесса сюда не попадают."""
    present = set(names)
    for n in names:
        path = os.path.join(VIDEO_DIR, n)
        if n.endswith(PART):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        name = n[:-4]
        if (not n.endswith(".jpg") or name + FAILED in present
                or (name + ".mp4" in present and mp4_complete(path[:-4] + ".mp4"))):
            continue
        log(f"unfinished clip {name}.mp4")
        try:
            with open(path, "rb") as f:
                poster = f.read()
            save_failed(poster, name)
        except Exception as e:
            log(f"placeholder error for {name}: {e}")


def write_snapshots():
    """Поток записи снимков на SD-карту: медленная карта (секунды на запись) не задерживает детектор и push
    о движении, а ошибка записи (например, нет места) не обрывает подключение к камере."""
    error = None
    while True:
        name, frame = SNAPSHOTS.get()
        path = os.path.join(RAW_DIR, name + ".jpg")
        t0 = time.monotonic()
        try:
            with open(path, "wb") as f:
                f.write(frame)
            error = None
        except OSError as e:
            if e.strerror != error:  # строка на причину (например, нет места), а не на каждый снимок
                log(f"snapshot write error: {e}")
            error = e.strerror
        if DEBUG and time.monotonic() - t0 > 1:  # медленная SD-карта — первый признак будущего зависания
            log(f"debug: slow write {name}.jpg: {time.monotonic() - t0:.1f} s")
        drop_cache(path)


def die_with_parent():
    """preexec_fn: ffmpeg завершается вместе с процессом камеры, даже убитым надзирателем (SIGKILL); иначе
    записывающий в файлы ffmpeg остался бы работать сам по себе."""
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    if os.getppid() == 1:  # процесс камеры умер ещё до prctl
        os._exit(1)


def segment_start(name):
    """Время начала куска по его имени seg_<секунды с 1970>.ts, или None."""
    m = re.fullmatch(r"seg_(\d+)\.ts", name)
    return int(m.group(1)) if m else None


def segments():
    """[(начало, путь)] кусков потока по времени."""
    try:
        names = os.listdir(SEGMENT_DIR)
    except OSError:
        return []
    return sorted((segment_start(n), os.path.join(SEGMENT_DIR, n)) for n in names if segment_start(n) is not None)


GO2RTC_PROBE_URL = "http://127.0.0.1:1984/api/streams?video=all&audio=all&src="  # + ID камеры: кодеки её потока
AUDIO_CODECS = ("aac", "pcma", "pcmu", "opus", "pcm", "mp3")


def clip_audio(cam_id=None, cam=None):
    """Брать ли звук камеры в ролики: «Звук в Live и роликах» включён и камера отдаёт AAC (по go2rtc). Только AAC: G.711 нет
    в MPEG-TS кусков, а iPhone не играет его в mp4; Opus не берём — осторожность. Любая ошибка — ролики без звука."""
    cam_id, cam = cam_id or CAMERA_ID, cam or CAM
    if not cam or not cam.get("audio") or not cam_id:
        return False
    try:
        with urllib.request.urlopen(GO2RTC_PROBE_URL + urllib.parse.quote(cam_id), timeout=3) as resp:
            text = resp.read(200_000).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    codecs = re.findall(r'"codec_name":\s*"([a-z0-9_]+)"', text)
    return next((c for c in codecs if c in AUDIO_CODECS), None) == "aac"


def segmenter_cmd(audio=False):
    """ffmpeg «Потока»: видео камеры от go2rtc без перекодирования (и звук AAC, если audio) — куски MPEG-TS по
    SEGMENT_SECONDS с временем начала в имени. «0:a:0?» — звука в потоке может не оказаться, это не ошибка."""
    return [
        "ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", str(RTSP_TIMEOUT * 1_000_000),
        "-i", SOURCE_URL, "-map", "0:v:0", *(["-map", "0:a:0?"] if audio else []), "-c", "copy",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-strftime", "1",
        os.path.join(SEGMENT_DIR, "seg_%s.ts"),
    ]


def keep_segments():
    """Поток записи «Потока»: держит ffmpeg, который режет видео камеры на куски в /dev/shm (кольцевой буфер
    в памяти, SD-карта не изнашивается), перезапускает его, если он завершился, и удаляет куски старше SEGMENT_KEEP."""
    shutil.rmtree(SEGMENT_DIR, ignore_errors=True)
    os.makedirs(SEGMENT_DIR, exist_ok=True)
    proc, started = None, 0.0
    while True:
        if (proc is None or proc.poll() is not None) and time.time() - started >= RESTART_DELAY:
            if proc is not None:
                log(f"stream recorder exited with code {proc.returncode}, restarting it")
            if not os.path.isdir(SEGMENT_DIR):
                # папку в /dev/shm может удалить не детектор (systemd-logind при выходе пользователя из последнего
                # сеанса чистит его файлы в /dev/shm): без неё ffmpeg сразу завершается (код 254, ENOENT)
                log(f"stream buffer {SEGMENT_DIR} is gone, creating it again")
                os.makedirs(SEGMENT_DIR, exist_ok=True)
            audio = clip_audio()  # при каждом запуске заново: кодек или флаг камеры могли смениться
            if audio:
                log("stream recorder: the camera's sound goes into the clips")
            proc = subprocess.Popen(segmenter_cmd(audio), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=CHILD_ENV, preexec_fn=die_with_parent)
            started = time.time()
        now = time.time()
        for start, path in segments():
            if now - start > SEGMENT_KEEP:
                try:
                    os.remove(path)
                except OSError:
                    pass
        time.sleep(2)


def video_codec(path):
    """Кодек видео куска (h264, hevc) по ffprobe, или "" если не удалось."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15, env=CHILD_ENV)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # в MPEG-TS поток есть и в программе, и в списке потоков: ffprobe печатает кодек дважды («hevc\nhevc»)
    lines = p.stdout.split()
    return lines[0] if lines else ""


def stream_clip(start, end, out_path):
    """«Поток»: ролик с start до end (время по часам) из кусков без перекодирования. Кусок, в котором end, готов,
    когда ffmpeg начал следующий; куски MPEG-TS склеиваются подряд, ffmpeg переупаковывает их в mp4.
    Возвращает (число кусков, начало первого по часам): ролик начинается с него, от него считается время
    движения в ролике. Имя куска — целые секунды, поэтому начало на 0–1 с раньше настоящего."""
    deadline = time.time() + 3 * SEGMENT_SECONDS + 5
    while True:
        segs = segments()
        if (segs and segs[-1][0] > end) or time.time() > deadline:
            break
        time.sleep(0.5)
    # только законченные куски (у которых есть следующий), пересекающиеся с [start, end]
    picked = [(s, path) for (s, path), (nxt, _) in zip(segs, segs[1:], strict=False) if nxt > start and s < end]
    if not picked:
        raise RuntimeError("no stream segments for the clip (the stream recorder is not running?)")
    # куски склеивает сам ffmpeg (протокол concat), не Python: мегабайты ролика в памяти процесса камеры на каждое
    # движение — блоки, которые glibc потом не отдаёт системе, и память детектора росла до MemoryMax (OOM за ~9 ч)
    source = "concat:" + "|".join(path for _, path in picked)
    # H.265 в mp4 ffmpeg по умолчанию помечает hev1 — Safari и iPhone такой ролик не играют, только hvc1
    tag = ["-tag:v", "hvc1"] if video_codec(picked[0][1]) == "hevc" else []
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "mpegts", "-i", source, "-c", "copy", *tag,
         "-movflags", "+faststart", "-f", "mp4", out_path],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=CHILD_ENV,
        preexec_fn=lambda: os.nice(ENCODE_NICE),
    )
    try:
        _, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError("stream clip: ffmpeg did not finish in 120 s") from None
    if proc.returncode != 0 or not mp4_complete(out_path):
        raise RuntimeError(err.decode(errors="replace")[-300:] or "stream clip: no video")
    return len(picked), picked[0][0]


def notify_gallery(name):
    try:
        headers = {"Content-Type": "application/json"}
        token = zorcam_config.internal_token()  # общий секрет с галереей: без него она не принимает сигнал
        if token:
            headers["X-Zorcam-Internal"] = token
        req = urllib.request.Request(
            GALLERY_MOTION_URL, data=json.dumps({"name": name, "camera": CAMERA_ID}).encode(),
            headers=headers, method="POST",
        )
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:
        log(f"notify gallery error: {e}")


def clean_error(text):
    """Строка ошибки ffmpeg без служебных префиксов и без пароля камеры."""
    text = re.sub(r"\[[^\]]* @ 0x[0-9a-f]+\] ", "", text)
    text = re.sub(r"(rtsps?://[^:/@\s]*):[^@\s]*@", r"\1:••••@", text)
    return re.sub(r"\?timeout=\d+", "", text)


def read_stderr(stderr, tail):
    for line in iter(stderr.readline, b""):
        text = clean_error(line.decode(errors="replace").strip())
        if text and text not in tail:
            tail.append(text)


def read_frames(stdout, frame_queue):
    buf = b""
    while True:
        chunk = stdout.read1(65536)  # сколько уже есть: read(65536) ждал бы 64 КБ, и кадр приходил бы с опозданием
        if not chunk:
            try:
                frame_queue.put(None, timeout=5)
            except queue.Full:
                pass
            return
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start == -1:
                buf = buf[-1:]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end == -1:
                buf = buf[start:]
                break
            end += 2
            try:
                frame_queue.put(buf[start:end], timeout=STALL_TIMEOUT)
            except queue.Full:  # основной цикл уже не читает (переподключается) — выходим
                return
            buf = buf[end:]


def detector_cmd(source=SOURCE_URL):
    """ffmpeg детектора: ключевые кадры камеры (source — поток go2rtc) — JPEG в stdout."""
    return [
        "ffmpeg", "-loglevel", "warning" if DEBUG else "error",
        "-rtsp_transport", "tcp", "-timeout", str(RTSP_TIMEOUT * 1_000_000),
        # без анализа потока при подключении и без многопоточного декодера: иначе ffmpeg всё время отстаёт
        # от камеры на ~1,4 с (прочитанное при анализе) и ещё на 4 кадра (очередь потоков декодера)
        "-probesize", "32", "-analyzeduration", "0", "-flags", "low_delay",
        "-skip_frame", "nokey", "-i", source, "-map", "0:v:0",  # только видео: у камеры со звуком go2rtc отдаёт и его
        # JPEG — в один поток: на 4 ядрах кодировщик иначе заводит поток с буферами кадра 1080p на каждое ядро
        # (93 МБ вместо 50 на процесс), а на 1–5 кадров в секунду одного потока хватает
        "-fps_mode", "passthrough", "-threads", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "-",
    ]


RUN = {"streamed": 0.0}  # сколько секунд шли кадры в последнем подключении (run_once)


def next_frame(frames_q, wait):
    """Следующий кадр не дольше wait секунд (иначе queue.Empty). Ждёт частями, обновляя файл состояния:
    ожидание до STALL_MAX иначе выглядело бы для надзирателя как зависание (HUNG_AFTER)."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return frames_q.get(timeout=max(0.05, min(5, deadline - time.monotonic())))
        except queue.Empty:
            if time.monotonic() >= deadline:
                raise
            STATUS.update()


class DetectorSource:
    """Какой поток смотрит детектор: второй (если задан), пока он даёт кадры; SUB_FAILS подключений подряд без кадров —
    основной на SUB_RETRY секунд (поток не тот или выключен в камере), затем снова второй. Основной — запасной, пока он идёт:
    оборвалось его соединение (общий сбой сети или камеры — молчат оба потока), детектор сразу возвращается ко второму,
    а не сидит полчаса на 1080p: на нём память детектора растёт до предела (6.10: 361 МБ вместо 208, счётчик `high`)."""

    def __init__(self, has_sub):
        self.has_sub, self.fails, self.main_until = has_sub, 0, 0.0

    def sub(self, now):
        return self.has_sub and now >= self.main_until

    def result(self, used_sub, streamed, now):
        """После подключения: streamed — сколько секунд шли кадры. Возвращает True, если детектор перешёл на основной."""
        if not used_sub:
            if self.main_until:  # запасной основной поток закончился: следующее подключение — снова ко второму
                self.fails, self.main_until = 0, 0.0
            return False
        self.fails = 0 if streamed else self.fails + 1
        if self.fails < SUB_FAILS:
            return False
        self.fails, self.main_until = 0, now + SUB_RETRY
        return True


SOURCE = DetectorSource(HAS_SUB)
ZONES = Zones(CAMERA_ID)


def run_once(sub=False):
    log("connecting to RTSP stream" + (" (the second stream)" if sub else ""))
    STATUS.update(force=True, state="connecting", connected_since=None, detector_stream="sub" if sub else "main")
    proc = subprocess.Popen(
        detector_cmd(SUB_URL if sub else SOURCE_URL),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=CHILD_ENV,
    )
    frames_q = queue.Queue(maxsize=50)
    err_tail = collections.deque(maxlen=5)
    threading.Thread(target=read_frames, args=(proc.stdout, frames_q), daemon=True).start()
    err_reader = threading.Thread(target=read_stderr, args=(proc.stderr, err_tail), daemon=True)
    err_reader.start()

    prev_small = None
    last_saved = 0.0
    motion_seen = False  # движение с прошлого сохранённого снимка
    stats = {"frames": 0, "saved": 0, "skipped": 0, "max_score": 0.0, "since": time.time()}
    recording = False
    rec_poster = None  # постер ролика (кадр после срабатывания, до него — кадр срабатывания) и заглушка, если ролик не сохранится
    rec_box = None  # рамка движения на постере
    rec_thread = None  # поток записи постера
    rec_name = None
    rec_started = 0.0
    rec_wall = 0.0  # время срабатывания по часам: куски «Потока» названы по нему
    rec_motion = 0.0  # с какого момента ролика галерея показывает рамку: первый кадр сравнения (ещё без движения)
    walls = collections.deque(maxlen=2)  # время по часам двух последних кадров: первый и второй кадр сравнения
    arrivals = collections.deque(maxlen=KEYFRAME_WINDOW)  # время прихода последних ключевых кадров

    reason = None
    try:
        while True:
            wait = stall_timeout(STATUS.data["keyframe_gap"], first=STATUS.data["connected_since"] is None)
            try:
                frame = next_frame(frames_q, wait)
            except queue.Empty:
                reason = Msg("capture.stall", seconds=wait)
                log(f"no frames for {wait} s, reconnecting")
                return
            if frame is None:
                reason = Msg("capture.ended" if STATUS.data["connected_since"] else "capture.connect_failed")
                log("stream ended")
                return

            now = datetime.now()
            t_frame = time.monotonic()
            arrivals.append(t_frame)
            walls.append(time.time())
            if STATUS.data["connected_since"] is None:
                log("streaming")
                STATUS.update(force=True, state="streaming", connected_since=time.time(), last_frame=time.time(),
                              keyframe_fps=None)
            else:
                fps = keyframe_rate(arrivals)
                # интервал запоминается и между подключениями: после обрыва порог «нет кадров» — тот же
                STATUS.update(last_frame=time.time(), keyframe_fps=fps,
                              **({"keyframe_gap": round(1 / fps, 2)} if fps and len(arrivals) >= 5 else {}))
            name = zorcam_config.frame_name(now)
            stats["frames"] += 1
            if DEBUG and time.time() - stats["since"] >= 60:
                log(f"debug: {stats['frames']} keyframes, {stats['saved']} saved, {stats['skipped']} skipped "
                    f"(SD card busy), queue {frames_q.qsize()}, max motion {stats['max_score']:.4f}, recording {recording}")
                stats.update(frames=0, saved=0, skipped=0, max_score=0.0, since=time.time())

            try:
                curr_small = small_gray(frame)
            except Exception as e:
                log(f"decode error: {e}")
                curr_small = None

            moving = recording  # пауза после срабатывания — движение продолжается
            if recording:  # ролик — clip_seconds реального времени; без сохранения роликов — только пауза детектора
                if t_frame - rec_started >= VIDEO_SECONDS:
                    if STREAM:
                        window = (min(rec_wall - PRE_SECONDS, rec_motion - 1), time.time())
                        threading.Thread(target=save_event, args=(rec_poster, rec_name, window),
                                         kwargs={"box": rec_box, "motion_wall": rec_motion, "poster_thread": rec_thread},
                                         daemon=True).start()
                    recording = False
                    rec_poster = rec_box = rec_thread = None
            elif prev_small is not None and curr_small is not None:
                zone = ZONES.get()
                try:
                    score = motion_score(prev_small, curr_small, zone)
                except Exception as e:
                    log(f"motion_score error: {e}")
                    score = 0.0
                stats["max_score"] = max(stats["max_score"], score)
                moving = score >= MOTION_THRESHOLD
                if moving:
                    log(f"motion score={score:.4f}, recording {name}")
                    threading.Thread(target=notify_gallery, args=(name,), daemon=True).start()
                    recording = True
                    rec_poster = frame if STREAM else None
                    rec_name = name
                    rec_started = t_frame
                    rec_wall = walls[-1]
                    # отметка и рамка (разница второго кадра с первым) — с первого кадра: движение началось после него
                    rec_motion = walls[0]
                    rec_box = None
                    if STREAM:  # постер — кадр срабатывания (второй), рамка — где он отличается от первого (prev_small)
                        try:
                            rec_box = motion_box(prev_small, curr_small, zone)
                        except Exception as e:
                            log(f"motion box error: {e}")
                        rec_thread = threading.Thread(target=write_poster, args=(frame, rec_name, rec_box), daemon=True)
                        rec_thread.start()

            # снимок на SD раз в FRAME_INTERVAL; если с прошлого сохранённого снимка детектор видел движение — с меткой
            # _m (таймлапс в облаке рисует по ней оранжевые отметки); кадры для этого заново не разбираются
            motion_seen = motion_seen or moving
            if SAVE_FRAMES and time.time() - last_saved >= FRAME_INTERVAL:
                try:
                    SNAPSHOTS.put_nowait((zorcam_config.frame_name(now, motion_seen), frame))  # пишет write_snapshots
                    last_saved = time.time()
                    motion_seen = False
                    stats["saved"] += 1
                except queue.Full:  # карта ещё пишет прошлые снимки
                    stats["skipped"] += 1

            prev_small = curr_small
    finally:
        since = STATUS.data["connected_since"]
        RUN["streamed"] = time.time() - since if since else 0.0
        if recording and STREAM:  # обрыв посреди ролика: сохранить записанное (нет кадра после срабатывания — постер из него)
            window = (min(rec_wall - PRE_SECONDS, rec_motion - 1), time.time())
            threading.Thread(target=save_event, args=(rec_poster, rec_name, window),
                             kwargs={"box": rec_box, "motion_wall": rec_motion, "poster_thread": rec_thread},
                             daemon=True).start()
        proc.kill()
        proc.wait()
        err_reader.join(1)  # последние строки ffmpeg: после kill у stderr конец, обычно сразу
        # ffmpeg видит только go2rtc: причину на стороне камеры (401, недоступна) узнаём у неё самой
        detail = " | ".join(err_tail)
        camera = zorcam_config.probe_camera(zorcam_config.rtsp_url(CAM, sub=True) if sub else RTSP_URL, RTSP_TIMEOUT)
        if camera:
            detail = f"camera: {camera}" + (f" | {detail}" if detail else "")
        set_error(reason or Msg("capture.error"), clean_error(detail), reconnect=True)
        if err_tail:
            log(f"ffmpeg: {' | '.join(err_tail)}")


def set_error(reason, detail="", reconnect=False):
    """Последняя ошибка камеры для галереи: last_error — по-английски (журнал), last_error_msg — для перевода."""
    if reconnect:
        STATUS.data["reconnects"].append(time.time())
    STATUS.update(force=True, state="reconnecting", connected_since=None, last_error_at=time.time(),
                  last_error=f"{reason}: {detail}" if detail else str(reason),
                  last_error_msg={"reason": reason.to_dict(), "detail": detail})


def run_camera():
    """Процесс одной камеры: подключение через go2rtc, детектор, ролики; при обрыве — переподключение."""
    if CAM is None or not CAM.get("enabled", True):
        log("the camera is no longer in cameras.json or is switched off, exiting")
        return
    limit_malloc()  # до первых потоков: арены заводятся при их первых выделениях памяти
    log(f"camera {CAM['name']}: detector stream={'second' if HAS_SUB else 'main'} "
        f"clip={VIDEO_SECONDS}s save_videos={SAVE_VIDEOS} save_frames={SAVE_FRAMES} "
        f"threshold={MOTION_THRESHOLD} stall={STALL_TIMEOUT}s "
        f"frames_interval={FRAME_INTERVAL}s")
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(VIDEO_DIR, exist_ok=True)
    if SAVE_FRAMES:
        threading.Thread(target=write_snapshots, daemon=True).start()
    if STREAM:
        threading.Thread(target=keep_segments, daemon=True).start()
    # заглушки недописанных роликов — в фоне: подключение не ждёт их (и чужих роликов в очереди на кодирование),
    # а надзиратель не примет долгую починку за зависание процесса камеры
    try:
        names = os.listdir(VIDEO_DIR)
    except OSError:
        names = []
    threading.Thread(target=recover_clips, args=(names,), daemon=True).start()
    delay = RETRY_MIN
    while True:
        RUN["streamed"] = 0.0
        sub = SOURCE.sub(time.time())
        try:
            run_once(sub)
        except Exception as e:
            log(f"capture loop error: {e}")
            set_error(Msg("capture.error"), str(e))
        if SOURCE.result(sub, RUN["streamed"], time.time()):
            log(f"the second stream gave no frames {SUB_FAILS} times, the detector uses the main stream "
                f"for {SUB_RETRY // 60} min")
            STATUS.data["keyframe_gap"] = None  # у основного потока свой интервал ключевых кадров
            delay = RETRY_MIN
            continue
        delay = retry_delay(delay, RUN["streamed"])
        if delay > RETRY_MIN:
            log(f"next connection attempt in {delay} s")
        for _ in range(delay):  # пауза перед переподключением, не переставая обновлять файл состояния
            time.sleep(1)
            STATUS.update()


def stop_workers(workers):
    for proc, _started in workers.values():
        if proc.poll() is None:
            proc.terminate()
    for proc, _started in workers.values():
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def supervise():
    """Надзиратель: процесс на каждую камеру, перезапуск упавших и зависших, пинги watchdog."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemctl stop: остановить и процессы камер (finally)
    for e in CFG.errors:
        log(f"config: {e}")
    cams = [c for c in zorcam_config.load_cameras() if c.get("enabled", True)]  # выключенная камера: процесса нет
    ids = {c["id"] for c in cams}
    streams = {c["id"] for c in cams if c["save_videos"]}
    for name in os.listdir("/dev/shm") if os.path.isdir("/dev/shm") else []:  # состояние удалённых камер
        m = re.fullmatch(r"zorcam_capture_(.+)\.json", name)
        if m and m.group(1) not in ids:
            try:
                os.remove(os.path.join("/dev/shm", name))
            except OSError:
                pass
        m = re.fullmatch(r"zorcam_rec_(.+)", name)  # буфер «Потока» камеры, которая его больше не пишет
        if m and m.group(1) not in streams:
            shutil.rmtree(os.path.join("/dev/shm", name), ignore_errors=True)
    if cams:
        log(f"cameras: {', '.join(sorted(ids))}")
    else:
        log("no cameras yet: add them in the gallery (setup wizard or Settings)")
    workers = {}  # id -> (процесс, когда запущен)
    memory_logged = time.time() - MEMORY_LOG_INTERVAL + 300  # первая строка — через 5 мин, когда всё подключилось
    try:
        while True:
            zorcam_config.sd_notify("WATCHDOG=1")
            if time.time() - memory_logged >= MEMORY_LOG_INTERVAL:
                memory_logged = time.time()
                try:
                    log(memory_line(workers))
                except OSError as e:
                    log(f"memory: {e}")
            if os.environ.get("INVOCATION_ID") and zorcam_config.signature() != CONFIG_SIG:
                log("zorcam.conf, cameras.json or secrets.env changed, restarting")
                return
            now = time.time()
            for cam in cams:
                proc, started = workers.get(cam["id"], (None, 0.0))
                if proc is not None and proc.poll() is None:
                    try:
                        beat = os.path.getmtime(zorcam_config.capture_status_file(cam["id"]))
                    except OSError:
                        beat = started
                    if now - max(beat, started) > HUNG_AFTER:
                        log(f"camera {cam['id']} does not respond for {HUNG_AFTER} s, restarting it")
                        proc.kill()
                        proc.wait()
                    continue
                if proc is not None and now - started < RESTART_DELAY:
                    continue
                if proc is not None:
                    log(f"camera {cam['id']} process exited with code {proc.returncode}, restarting it")
                workers[cam["id"]] = (subprocess.Popen([sys.executable, os.path.abspath(__file__), "--camera", cam["id"]],
                                                       env=CHILD_ENV), now)
            time.sleep(2)
    finally:
        stop_workers(workers)


if __name__ == "__main__":
    if CAMERA_ID:
        run_camera()
    else:
        supervise()
