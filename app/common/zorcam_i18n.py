"""User interface languages of Zorcam: English and Russian.

Every visible string of the gallery, the Settings and /config pages, health messages and push notifications
lives here as STRINGS[key] = {"en": ..., "ru": ...}. Values are str.format templates.
Configuration files, install scripts and logs are always in English and do not use this module.
"""

LANGS = ("en", "ru")
DEFAULT = "en"
LANG_NAMES = {"en": "English", "ru": "Русский"}


def normalize(code):
    """'ru-RU' -> 'ru'; None for unsupported languages."""
    code = (code or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGS else None


def from_accept_language(header):
    """The first supported language of the browser (the device language), by q-value; English otherwise."""
    choices = []
    for i, part in enumerate((header or "").split(",")):
        fields = part.strip().split(";")
        q = 1.0
        for f in fields[1:]:
            f = f.strip()
            if f.startswith("q="):
                try:
                    q = float(f[2:])
                except ValueError:
                    q = 0.0
        lang = normalize(fields[0])
        if lang and q > 0:
            choices.append((-q, i, lang))
    return min(choices)[2] if choices else DEFAULT


def resolve(setting, accept_language=None):
    """setting from zorcam.conf ([language] language): auto, en or ru."""
    return normalize(setting) or from_accept_language(accept_language)


def t(lang, msg_key, /, **kw):
    entry = STRINGS[msg_key]
    text = entry.get(lang) or entry[DEFAULT]
    if not kw:
        return text
    return text.format(**{k: render(v, lang) for k, v in kw.items()})


def render(value, lang):
    """A message argument: Msg, a function of the language, or a plain value."""
    if isinstance(value, Msg):
        return value.text(lang)
    if callable(value):
        return value(lang)
    return value


class Msg:
    """A message translated when shown: Msg("cfg.not_a_number").text("ru").
    str(msg) is English, for logs and command-line output."""

    def __init__(self, msg_key, /, **kw):
        self.key, self.kw = msg_key, kw

    def text(self, lang):
        return t(lang, self.key, **self.kw)

    def __str__(self):
        return self.text(DEFAULT)

    def __eq__(self, other):
        return isinstance(other, Msg) and (self.key, self.kw) == (other.key, other.kw)

    def __hash__(self):
        return hash((self.key, tuple(sorted((k, str(v)) for k, v in self.kw.items()))))

    def __repr__(self):
        return f"Msg({self.key!r}, {self.kw!r})"

    def to_dict(self):
        """For JSON (the capture status file). Nested messages stay translatable; other arguments become text."""
        def value(v):
            if isinstance(v, Msg) and not isinstance(v, Raw):
                return {"msg": v.to_dict()}
            return v if isinstance(v, (int, float, str)) else str(render(v, DEFAULT))
        return {"key": self.key, "kw": {k: value(v) for k, v in self.kw.items()}}

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, dict) and data.get("key") in STRINGS:
            kw = {k: cls.from_dict(v["msg"]) if isinstance(v, dict) and "msg" in v else v
                  for k, v in (data.get("kw") or {}).items()}
            return cls(data["key"], **kw)
        return Raw(str(data))


class Raw(Msg):
    """Text that is not translated (ffmpeg errors, file paths, values)."""

    def __init__(self, text):
        super().__init__("raw", text=str(text))

    def text(self, lang):
        return self.kw["text"]


# Russian descriptions of zorcam.conf parameters; the English ones are in zorcam_config.PARAMS.
PARAM_RU = {
    ("language", "language"): "Язык интерфейса и push-уведомлений по умолчанию — на устройствах, где язык не выбран "
                              "(язык выбирается на каждом устройстве, на странице «Настройки»): auto — язык устройства "
                              "(браузера), en или ru",
    ("capture", "motion_threshold"): "Порог чувствительности анализа движения (0.02 = 2%). Меньше — чувствительнее",
    ("capture", "stall_timeout"): "Нет кадров от камеры дольше, с — переподключиться (не меньше трёх интервалов ключевых кадров)",
    ("capture", "rtsp_timeout"): "Таймаут сетевых операций RTSP в ffmpeg, с",
    ("live", "webrtc_fallback_seconds"): "Если WebRTC не соединился за столько секунд, плеер включает HLS",
    ("live", "hls_idle_timeout"): "HLS-поток останавливается, если плеер не запрашивает его столько секунд",
    ("notifications", "motion_cooldown"): "Уведомления о движении не чаще, чем раз в столько секунд",
    ("notifications", "health"): "Push-уведомления о неисправностях и о восстановлении",
    ("notifications", "health_repeat_minutes"): "Напоминать о неустранённой неисправности раз в столько минут "
                                                "(0 — не напоминать)",
    ("health", "check_interval"): "Проверка состояния системы раз в столько секунд",
    ("health", "no_frames_seconds"): "Неисправность, если от камеры нет кадров дольше, с",
    ("health", "min_free_disk_mb"): "Если свободного места меньше, МБ, самые старые записи удаляются (push администраторам)",
    ("health", "max_cpu_temp"): "Неисправность, если температура процессора выше, °C",
    ("gallery", "videos_per_page"): "Роликов на странице во вкладке «Видео»: 10, 30 или 60",
    ("gallery", "session_days"): "Срок входа без повторного ввода пароля, дней (продлевается при каждом заходе)",
    ("gallery", "environment"): "Какой это стенд: prod, stage или test. На стендах не prod в приложении виден значок "
                                "TEST / STAGE, а облако по умолчанию — того же стенда (cloud.stage.zorcam.ru, "
                                "cloud.test.zorcam.ru)",
}


def param_desc(lang, section, key, desc_en):
    return PARAM_RU.get((section, key), desc_en) if lang == "ru" else desc_en


STRINGS = {
    "raw": {"en": "{text}"},

    # ---- zorcam.conf: sections and validation (zorcam_config)
    "section.language": {"en": "Language", "ru": "Язык"},
    "section.capture": {"en": "Capture and motion detection", "ru": "Захват и детекция движения"},
    "section.live": {"en": "Live", "ru": "Live"},
    "section.notifications": {"en": "Notifications", "ru": "Уведомления"},
    "section.health": {"en": "Monitoring", "ru": "Мониторинг"},
    "section.gallery": {"en": "Gallery", "ru": "Галерея"},
    "cfg.allowed": {"en": "allowed: {values}", "ru": "допустимо: {values}"},
    "cfg.yes_no": {"en": "expected yes or no", "ru": "ожидается yes или no"},
    "cfg.positive_list": {"en": "expected positive numbers separated by commas",
                          "ru": "ожидаются положительные числа через запятую"},
    "cfg.not_a_number": {"en": "expected a number", "ru": "ожидается число"},
    "cfg.camera_id": {"en": "expected Latin letters, digits, - and _ (up to 32, not ending with _videos)",
                      "ru": "ожидаются латинские буквы, цифры, - и _ (до 32, без _videos в конце)"},
    "cfg.camera_id_taken": {"en": "a camera with this ID already exists", "ru": "камера с таким ID уже есть"},
    "cfg.camera_name": {"en": "expected a name of 1 to 40 characters", "ru": "ожидается название от 1 до 40 символов"},
    "cfg.rtsp_url": {"en": "expected rtsp://address:port/path without login and password",
                     "ru": "ожидается rtsp://адрес:порт/путь без логина и пароля"},
    "cfg.onvif_url": {"en": "expected http://address:port/path (empty: no pan/tilt/zoom buttons)",
                      "ru": "ожидается http://адрес:порт/путь (пусто: кнопок поворота и зума нет)"},
    "cfg.clip_seconds": {"en": "expected a whole number from 2 to 120", "ru": "ожидается целое число от 2 до 120"},
    "cfg.frames_interval": {"en": "expected a whole number from 1 to 3600", "ru": "ожидается целое число от 1 до 3600"},
    "cfg.days": {"en": "expected a whole number from 1 to 365", "ru": "ожидается целое число от 1 до 365"},
    "cfg.host": {"en": "expected an IP address or a host name", "ru": "ожидается IP-адрес или имя хоста"},
    "cfg.negative": {"en": "negative value", "ru": "отрицательное значение"},
    "cfg.unreadable": {"en": "file not read: {error}", "ru": "файл не прочитан: {error}"},
    "cfg.unknown": {"en": "[{section}] {key}: unknown parameter, ignored",
                    "ru": "[{section}] {key}: неизвестный параметр, пропущен"},
    "cfg.bad_value": {"en": "[{section}] {key} = {raw}: {error}; using the default",
                      "ru": "[{section}] {key} = {raw}: {error}; взято значение по умолчанию"},

    # ---- common
    "back": {"en": "Back", "ru": "Назад"},
    "brand.nvr": {"en": "Zorcam.nvr", "ru": "Зоркам.nvr"},
    "brand.cloud": {"en": "Zorcam.cloud", "ru": "Зоркам.cloud"},
    "brand.tip_nvr": {"en": "Viewing directly on the device", "ru": "Просмотр напрямую с устройства"},
    "brand.tip_cloud": {"en": "Viewing through the Zorcam cloud", "ru": "Просмотр через облако Zorcam"},
    "not_found": {"en": "not found", "ru": "не найдено"},
    "yes": {"en": "yes", "ru": "да"},
    "no": {"en": "no", "ru": "нет"},
    "units.b": {"en": "B", "ru": "Б"},
    "units.kb": {"en": "KB", "ru": "КБ"},
    "units.mb": {"en": "MB", "ru": "МБ"},
    "units.gb": {"en": "GB", "ru": "ГБ"},
    "dur.sec": {"en": "{n} s", "ru": "{n} с"},
    "dur.min": {"en": "{n} min", "ru": "{n} мин"},
    "dur.hm": {"en": "{h} h {m} min", "ru": "{h} ч {m} мин"},

    # ---- login
    "login.title": {"en": "sign in", "ru": "вход"},
    "login.user": {"en": "Username", "ru": "Логин"},
    "login.password": {"en": "Password", "ru": "Пароль"},
    "login.submit": {"en": "Sign in", "ru": "Войти"},
    "login.locked": {"en": "Too many attempts. Please wait a minute.", "ru": "Слишком много попыток. Подождите минуту."},
    "login.wrong": {"en": "Wrong username or password", "ru": "Неверный логин или пароль"},

    # ---- main page
    "tab.live": {"en": "Live", "ru": "Live"},
    "tab.records": {"en": "Records", "ru": "Записи"},
    "tab.videos": {"en": "Video", "ru": "Видео"},
    "tab.all": {"en": "Timelapse", "ru": "Timelapse"},
    "live.columns": {"en": "{n} per row", "ru": "{n} в ряд"},
    "live.reload": {"en": "Reconnect the cameras", "ru": "Переподключить камеры"},
    "live.embed_unavailable": {"en": "Camera unavailable", "ru": "Камера недоступна"},
    "logout": {"en": "Sign out", "ru": "Выйти"},
    "settings": {"en": "Settings", "ru": "Настройки"},
    "bell": {"en": "Motion notifications", "ru": "Уведомления о движении"},
    "pager.older": {"en": "‹ Earlier", "ru": "‹ Ранее"},
    "pager.newer": {"en": "Later ›", "ru": "Вперёд ›"},
    "pager.info": {"en": "{first}–{last} of {total} · page {page}/{pages}",
                   "ru": "{first}–{last} из {total} · стр. {page}/{pages}"},
    "videos.empty": {"en": "No motion videos", "ru": "Нет видео с движением"},
    "videos.preparing": {"en": "Preparing video…", "ru": "Видео готовится…"},
    "watch.motion": {"en": "Motion", "ru": "Движение"},
    "watch.jump": {"en": "Go to the motion", "ru": "К моменту движения"},
    "tl.play": {"en": "Play", "ru": "Пуск"},
    "tl.pause": {"en": "Pause", "ru": "Пауза"},
    "tl.position": {"en": "Position", "ru": "Положение"},
    "tl.marks": {"en": "Motion marks", "ru": "Отметки движения"},
    "tl.speed": {"en": "Speed", "ru": "Скорость"},
    "tl.realtime": {"en": "× real time", "ru": "× реального времени"},  # после числа: «75× real time»
    "tl.loading": {"en": "Loading…", "ru": "Загрузка…"},
    "tl.empty": {"en": "No snapshots for this period", "ru": "Нет снимков за этот период"},
    "tl.error": {"en": "Could not load the timelapse", "ru": "Не удалось загрузить таймлапс"},
    "tl.video": {"en": "Video", "ru": "Видео"},
    "cam.tabs_prev": {"en": "Earlier cameras", "ru": "Камеры слева"},
    "cam.tabs_next": {"en": "Later cameras", "ru": "Камеры справа"},
    "tl.all_videos": {"en": "Go to videos", "ru": "Перейти в видео"},
    "tl.video_open": {"en": "Open the video of this motion", "ru": "Открыть видео этого движения"},
    "tl.keys": {"en": "Keyboard: Space plays or pauses, ← → step a frame (Shift: 10), Home and End jump to the ends, "
                      "[ and ] go to the previous and next day",
                "ru": "Клавиатура: пробел — пуск и пауза, ← → — кадр назад и вперёд (Shift — на 10), Home и End — к краям, "
                      "[ и ] — предыдущий и следующий день"},
    "tl.days": {"en": "Days", "ru": "Дни"},
    "tl.days_earlier": {"en": "Earlier days", "ru": "Более ранние дни"},
    "tl.days_later": {"en": "Later days", "ru": "Более поздние дни"},
    "tl.day_frames": {"en": "snapshots:", "ru": "снимков:"},  # дальше число: в JS-строках нет подстановки {n}
    "tl.day_clips": {"en": "videos:", "ru": "роликов:"},
    "tl.day_no_clips": {"en": "no videos", "ru": "нет роликов"},
    "tl.day_none": {"en": "no snapshots", "ru": "нет снимков"},

    # ---- iPhone: how to add the app to the Home Screen (shown in Safari only)
    "app.install": {"en": "Zorcam app", "ru": "Приложение Zorcam"},
    "app.step1": {"en": "In Safari, tap Share", "ru": "В Safari нажмите «Поделиться»"},
    "app.step2": {"en": "Scroll down and tap Add to Home Screen", "ru": "Пролистайте вниз и выберите «На экран Домой»"},
    "app.step3": {"en": "Tap Add, open Zorcam from the Home Screen and sign in to the app",
                  "ru": "Нажмите «Добавить», откройте Zorcam с экрана «Домой» и авторизуйтесь в приложении"},
    "app.close": {"en": "Close", "ru": "Закрыть"},

    # ---- main page scripts (inserted into JavaScript as JSON strings)
    "js.connecting": {"en": "Connecting…", "ru": "Подключение…"},
    "js.unsaved": {"en": "The changes are not saved. Leave without saving?",
                   "ru": "Изменения не сохранены. Уйти без сохранения?"},
    "js.tap_to_play": {"en": "Tap to play", "ru": "Нажмите, чтобы запустить"},
    "js.tap_to_play_note": {"en": "The phone does not start video by itself (for example, in Low Power Mode): "
                                  "tap anywhere to start Live.",
                            "ru": "Телефон не запускает видео сам (например, в режиме энергосбережения): "
                                  "коснитесь экрана, чтобы включить Live."},
    "js.reconnecting": {"en": "Reconnecting…", "ru": "Переподключение…"},
    "js.connecting_hls": {"en": "Connecting (HLS)…", "ru": "Подключение (HLS)…"},
    "js.no_player": {"en": "This browser cannot play the stream", "ru": "Браузер не поддерживает трансляцию"},
    "js.stream_error": {"en": "Stream error", "ru": "Ошибка трансляции"},
    "js.player_failed": {"en": "Could not load the player", "ru": "Не удалось загрузить плеер"},
    "js.note_hls": {"en": "HLS (a few seconds of delay)", "ru": "HLS (задержка несколько секунд)"},
    "js.note_webrtc": {"en": "WebRTC (minimal delay)", "ru": "WebRTC (задержка минимальная)"},
    "js.buffering": {"en": "Buffering…", "ru": "Буферизация…"},
    "js.push_https": {"en": "Notifications work over HTTPS only. Open Zorcam at its https://… address.",
                      "ru": "Уведомления работают только по HTTPS. Откройте Zorcam по адресу https://…."},
    "js.push_iphone": {"en": "On iPhone, notifications work in the app: Share → Add to Home Screen, "
                             "then open Zorcam from the Home Screen.",
                       "ru": "На iPhone уведомления работают в приложении: «Поделиться» → «На экран Домой», "
                             "затем откройте Zorcam с экрана Домой."},
    "js.push_unsupported": {"en": "This browser does not support push notifications.",
                            "ru": "Этот браузер не поддерживает push-уведомления."},
    "js.push_wait": {"en": "Wait a couple of seconds and try again.", "ru": "Подождите пару секунд и попробуйте ещё раз."},
    "js.push_off_confirm": {"en": "Turn off motion notifications on this device?",
                            "ru": "Выключить уведомления о движении на этом устройстве?"},
    "js.push_denied": {"en": "Notifications are blocked. Allow them in Settings → Notifications → Zorcam.",
                       "ru": "Уведомления запрещены. Разрешите их в настройках: Настройки → Уведомления → Zorcam."},
    "js.push_no_crypto": {"en": "python3-cryptography is not installed on the server",
                          "ru": "на сервере не установлен python3-cryptography"},
    "js.push_failed": {"en": "Could not turn on notifications: ", "ru": "Не удалось включить уведомления: "},

    # ---- push notifications
    "push.enabled": {"en": "Motion notifications are on", "ru": "Уведомления о движении включены"},
    "push.motion": {"en": "Motion", "ru": "Движение"},
    "push.problem": {"en": "{text}", "ru": "{text}"},
    "push.still": {"en": "still {text} (since {since})", "ru": "всё ещё {text} (с {since})"},
    "push.recovered": {"en": "back to normal: {text}", "ru": "снова в норме — {text}"},

    # ---- monitoring
    "st.frames": {"en": "Motion detector", "ru": "Детектор движения"},
    "check.frames_of": {"en": "{name}: motion detector", "ru": "{name}: детектор движения"},
    "check.capture": {"en": "Capture service", "ru": "Сервис захвата"},
    "check.go2rtc": {"en": "Live service", "ru": "Сервис Live"},
    "check.disk": {"en": "Disk space", "ru": "Место на диске"},
    "check.storage": {"en": "Recordings disk", "ru": "Диск для записей"},
    "health.storage_disk": {"en": "connected: {model}", "ru": "подключён: {model}"},
    "health.storage_missing": {"en": "{model} is not connected: recording to the SD card",
                               "ru": "{model} не подключён: записи идут на SD-карту"},
    "storage.title": {"en": "Recordings disk", "ru": "Диск для записей"},
    "storage.sd": {"en": "Recordings are on the SD card", "ru": "Записи — на SD-карте"},
    "storage.usb": {"en": "Recordings are on the USB disk {model}, {size}", "ru": "Записи — на USB-диске {model}, {size}"},
    "storage.missing": {"en": "The disk {model} is not connected: recording to the SD card until it is back",
                        "ru": "Диск {model} не подключён: записи идут на SD-карту, пока он не вернётся"},
    "storage.free": {"en": "{free} free", "ru": "свободно {free}"},
    "storage.busy": {"en": "In progress…", "ru": "Выполняется…"},
    "storage.busy_formatting": {"en": "Formatting the disk…", "ru": "Форматирование диска…"},
    "storage.busy_moving": {"en": "Moving the recordings from the SD card to the disk…",
                            "ru": "Перенос записей с SD-карты на диск…"},
    "storage.error": {"en": "Could not do it: {error}", "ru": "Не получилось: {error}"},
    "storage.ext4": {"en": "ext4: can be connected without formatting", "ru": "ext4: можно подключить без форматирования"},
    "storage.none": {"en": "No USB disk. Plug in a flash drive or a disk (a USB SSD is best; a hard disk needs its own "
                             "power or a powered hub: the device's USB ports may not start it)",
                     "ru": "USB-дисков нет. Подключите флешку или диск (лучше USB-SSD; жёсткому диску нужно своё питание "
                           "или хаб с питанием: USB-портов устройства может не хватить, чтобы его раскрутить)"},
    "storage.format": {"en": "Format {model} and record to it", "ru": "Отформатировать {model} и записывать на него"},
    "storage.format_q": {"en": "Everything on {model} will be erased: the disk is formatted for the recordings "
                                "(ext4). The recordings on the SD card move to it. Format?",
                         "ru": "Всё на диске {model} будет стёрто: он форматируется для записей (ext4). Записи с "
                               "SD-карты перенесутся на него. Форматировать?"},
    "storage.connect": {"en": "Connect {model} without formatting", "ru": "Подключить {model} без форматирования"},
    "storage.connect_q": {"en": "Record to {model} as it is (its files stay)? The recordings on the SD card move to it.",
                          "ru": "Записывать на {model} как есть (его файлы останутся)? Записи с SD-карты перенесутся на него."},
    "storage.forget": {"en": "Disconnect the disk", "ru": "Отключить диск"},
    "storage.forget_q": {"en": "Stop recording to {model}? What is recorded on it stays there, new recordings go to the "
                                "SD card. After this the disk can be unplugged.",
                         "ru": "Перестать записывать на {model}? Записанное на нём останется, новые записи пойдут на "
                               "SD-карту. После этого диск можно вынуть."},
    "check.temp": {"en": "CPU temperature", "ru": "Температура CPU"},
    "check.memory": {"en": "Detector memory", "ru": "Память детектора"},
    "health.last_frame": {"en": "last frame {ago} ago", "ru": "последний кадр {ago} назад"},
    "health.no_frames_for": {"en": "no frames for {ago}", "ru": "нет кадров {ago}"},
    "health.no_frames_yet": {"en": "no frames yet", "ru": "кадров ещё не было"},
    "health.with_error": {"en": "{text}; {error}", "ru": "{text}; {error}"},
    "health.running": {"en": "running", "ru": "работает"},
    "health.free": {"en": "{free} free", "ru": "свободно {free}"},
    "health.memory_ok": {"en": "enough", "ru": "хватает"},
    "health.memory_short": {
        "en": "short: the kernel slowed the detectors down {n} times in {minutes} min, they lose frames. "
              "Too many cameras for this device: lower their video resolution or switch some off",
        "ru": "не хватает: ядро притормозило детекторы {n} раз за {minutes} мин, они теряют кадры. "
              "Камер больше, чем тянет это устройство: уменьшите им разрешение видео или отключите часть"},
    "health.event_problem": {"en": "Last problem: {title}", "ru": "Последняя проблема: {title}"},
    "health.event_recovered": {"en": "Recovered: {title}", "ru": "Восстановлено: {title}"},
    "health.event_disk_cleanup": {"en": "Oldest recordings removed", "ru": "Удалены самые старые записи"},
    "health.disk_cleanup": {
        "en": "less than {limit} free: the oldest recordings are removed automatically ({removed} files, {freed}), "
              "{free} free now. Keep recordings for fewer days or take snapshots less often",
        "ru": "свободно меньше {limit}: самые старые записи удаляются автоматически (файлов: {removed}, {freed}), "
              "сейчас свободно {free}. Сократите сроки хранения или делайте снимки реже"},

    # ---- capture errors (written by zorcam-nvr-capture to its status file)
    "capture.stall": {"en": "no frames for {seconds} s", "ru": "нет кадров {seconds} с"},
    "capture.ended": {"en": "stream ended", "ru": "поток прервался"},
    "capture.connect_failed": {"en": "could not connect", "ru": "не удалось подключиться"},
    "capture.error": {"en": "error", "ru": "ошибка"},
    "capture.with_detail": {"en": "{reason}: {detail}", "ru": "{reason}: {detail}"},

    # ---- Settings page
    "settings.title": {"en": "Settings", "ru": "Настройки"},
    "settab.general": {"en": "General", "ru": "Общие"},
    "settab.cameras": {"en": "Cameras", "ru": "Камеры"},
    "settab.status": {"en": "Status", "ru": "Статус"},
    "settab.users": {"en": "Users", "ru": "Польз."},
    "settab.notify": {"en": "Alerts", "ru": "Уведомления"},
    "notify.section": {"en": "Motion notifications", "ru": "Уведомления о движении"},
    "notify.all": {"en": "All cameras", "ru": "Все камеры"},
    "notify.none": {"en": "No cameras are open to you", "ru": "Нет доступных вам камер"},
    "notify.error": {"en": "Could not save the switch, try again",
                     "ru": "Не удалось сохранить переключатель, попробуйте ещё раз"},
    "users.rights": {"en": "Role", "ru": "Роль"},
    "users.cameras": {"en": "Camera access", "ru": "Доступ к камерам"},
    "users.new_title": {"en": "New user", "ru": "Новый пользователь"},
    "cams.added_closed": {"en": "The camera {name} is added. Only admin sees it: switch it on for users on Settings → Users.",
                          "ru": "Камера «{name}» добавлена. Её видит только admin: включите её пользователям в «Польз.»."},
    "users.new_tip": {"en": "Viewer: Live, Videos, Timelapse and motion notifications. Live: Live only. "
                            "Password: at least 6 characters.",
                      "ru": "Viewer — Live, «Видео», Timelapse и уведомления о движении. Live — только Live. "
                            "Пароль — не короче 6 символов."},
    "users.add": {"en": "Add the user", "ru": "Добавить пользователя"},
    "users.bad_name": {"en": "the name must not be empty, contain spaces or be longer than {n} characters; "
                             "admin is the administrator",
                       "ru": "имя не должно быть пустым, с пробелами или длиннее {n} символов; admin — это администратор"},
    "users.delete": {"en": "Delete the user", "ru": "Удалить пользователя"},
    "users.confirm_delete": {"en": "Delete the user {name}? They will no longer be able to sign in.",
                             "ru": "Удалить пользователя {name}? Он больше не сможет войти."},
    "users.name_taken": {"en": "this name is already taken", "ru": "это имя уже занято"},
    "users.saved": {"en": "Saved: {name}", "ru": "Сохранено: {name}"},
    "role.liveviewer": {"en": "Live", "ru": "Live"},
    "role.viewer": {"en": "Viewer", "ru": "Viewer"},
    "role.admin": {"en": "Admin", "ru": "Admin"},
    "users.devices": {"en": "Device users", "ru": "Пользователи устройства"},
    "account.signed_in": {"en": "Signed in as {user}", "ru": "Вы вошли как {user}"},
    "st.section.community": {"en": "Zorcam NVR status", "ru": "Состояние Zorcam NVR"},
    "st.section.box": {"en": "Zorcam Box status", "ru": "Состояние Zorcam Box"},
    "st.cameras": {"en": "Camera status", "ru": "Состояние камер"},
    "st.version": {"en": "Version", "ru": "Версия"},
    "st.environment": {"en": "Environment", "ru": "Стенд"},
    "st.edition": {"en": "Edition", "ru": "Редакция"},
    "st.device": {"en": "Device", "ru": "Устройство"},
    "st.ram": {"en": "RAM {size}", "ru": "ОЗУ {size}"},
    "st.ram_gb": {"en": "{n} GB", "ru": "{n} ГБ"},
    "st.ram_mb": {"en": "{n} MB", "ru": "{n} МБ"},
    "st.timezone": {"en": "Time zone", "ru": "Часовой пояс"},
    "st.tz_pending": {"en": "The device is set to {zone} now: it applies after a restart of Zorcam",
                      "ru": "На устройстве уже выставлен {zone}: он заработает после перезапуска Zorcam"},
    "edition.community": {"en": "Zorcam NVR (free)", "ru": "Zorcam NVR (бесплатная)"},
    "edition.box": {"en": "Zorcam Box", "ru": "Zorcam Box"},
    "st.check": {"en": "Check", "ru": "Проверка"},
    "st.not_yet": {"en": "not run yet", "ru": "ещё не выполнялась"},
    "st.summary": {"en": "Summary", "ru": "Итог"},
    "st.all_ok": {"en": "all good", "ru": "всё в порядке"},
    "st.problems": {"en": "problems: {n}", "ru": "проблем: {n}"},
    "st.ok": {"en": "OK: ", "ru": "OK: "},
    "st.problem": {"en": "PROBLEM: ", "ru": "ПРОБЛЕМА: "},
    "st.since": {"en": " · since {since}", "ru": " · с {since}"},
    "st.detector": {"en": "Detector state", "ru": "Состояние детектора"},
    "st.codec": {"en": "Camera codec", "ru": "Кодек камеры"},
    "st.codec_sound": {"en": "sound {codec}", "ru": "звук {codec}"},
    "st.codec_h265": {"en": "If you can, switch the camera to H.264 in its settings",
                      "ru": "По возможности переключите в настройках камеры на H.264"},
    "st.state.streaming": {"en": "receiving frames", "ru": "получает кадры"},
    "st.state.connecting": {"en": "connecting", "ru": "подключается"},
    "st.state.reconnecting": {"en": "reconnecting", "ru": "переподключается"},
    "st.state.starting": {"en": "starting", "ru": "запускается"},
    "st.keyframes": {"en": "Keyframes per second", "ru": "Ключевых кадров в секунду"},
    "st.reconnects": {"en": "Reconnects in 24 h", "ru": "Переподключений за сутки"},
    "st.detector_stream": {"en": "Detector stream", "ru": "Поток детектора"},
    "st.stream.sub": {"en": "second (low resolution)", "ru": "второй (облегчённый)"},
    "st.stream.main": {"en": "main: the second stream gives no frames", "ru": "основной: второй поток не даёт кадров"},
    "st.last_error": {"en": "Last camera error", "ru": "Последняя ошибка камеры"},
    "st.last_check": {"en": "Last check", "ru": "Последняя проверка"},
    "st.ago": {"en": "{ago} ago", "ru": "{ago} назад"},
    "cf.error": {"en": "Error in zorcam.conf", "ru": "Ошибка в zorcam.conf"},
    "rec.section": {"en": "Recordings", "ru": "Записи"},
    "rec.delete": {"en": "Delete", "ru": "Удалить"},
    "rec.delete_all": {"en": "Delete all", "ru": "Удалить все"},
    "rec.confirm_all": {"en": "Are you sure? All snapshots and motion videos of {name} will be deleted. This cannot be undone.",
                        "ru": "Вы уверены? Будут удалены все снимки и ролики камеры {name}. Это нельзя отменить."},
    "rec.confirm_frames": {"en": "Are you sure? All snapshots of {name} will be deleted. This cannot be undone.",
                           "ru": "Вы уверены? Будут удалены все снимки камеры {name}. Это нельзя отменить."},
    "rec.confirm_videos": {"en": "Are you sure? All motion videos of {name} will be deleted. This cannot be undone.",
                           "ru": "Вы уверены? Будут удалены все ролики камеры {name}. Это нельзя отменить."},
    "lang.auto": {"en": "Auto: device language", "ru": "Авто — язык устройства"},
    "lang.pick": {"en": "Language", "ru": "Язык"},

    # ---- /config page
    "cfgpage.title": {"en": "Change settings", "ru": "Изменение настроек"},
    "cfgpage.intro": {"en": "Changes are written to <code>/var/lib/zorcam-nvr/config/zorcam.conf</code>. After saving, the "
                            "detector and the gallery restart by themselves; Live pauses for a few seconds. Cameras are "
                            "changed on the Settings page.",
                      "ru": "Изменения записываются в <code>/var/lib/zorcam-nvr/config/zorcam.conf</code>. "
                            "После сохранения детектор и галерея перезапустятся сами, Live прервётся на несколько секунд. "
                            "Камеры меняются на странице «Настройки»."},
    "cfgpage.errors": {"en": "Fix the fields marked in red: {n}. Nothing was saved.",
                       "ru": "Исправьте поля, отмеченные красным: {n}. Ничего не сохранено."},
    "cfgpage.password_placeholder": {"en": "keep", "ru": "не менять"},
    "cfgpage.cancel": {"en": "Cancel", "ru": "Отмена"},
    "cfgpage.save": {"en": "Save", "ru": "Сохранить"},
    "cfgpage.no_changes": {"en": "No changes.", "ru": "Изменений нет."},
    "cfgpage.saved_title": {"en": "Saved", "ru": "Сохранено"},
    "cfgpage.saved": {"en": "Settings saved. The detector and the gallery are restarting; the Settings page opens in "
                            "a few seconds.",
                      "ru": "Настройки сохранены. Детектор и галерея перезапускаются, через несколько секунд откроется "
                            "страница «Настройки»."},
    "cfgpage.lang.auto": {"en": "auto: device language", "ru": "auto — язык устройства"},

    # ---- cameras (Settings, camera form)
    "cams.section": {"en": "Cameras", "ru": "Камеры"},
    "cams.none": {"en": "No cameras yet.", "ru": "Камер пока нет."},
    "cams.no_access": {"en": "No camera is available to you.", "ru": "Нет доступных камер."},
    "cams.add": {"en": "Add a camera", "ru": "Добавить камеру"},
    "cams.edit": {"en": "Change", "ru": "Изменить"},
    "cam.section": {"en": "Camera", "ru": "Камера"},
    "cam.new_title": {"en": "New camera", "ru": "Новая камера"},
    "cloud.title": {"en": "Zorcam cloud", "ru": "Облако Zorcam"},
    "cloud.intro": {"en": "Optional: open this device from anywhere without port forwarding, several devices in one app, "
                          "sign-in with Yandex ID. Recordings stay on the device; without the cloud everything works as now.",
                    "ru": "По желанию: открывать устройство откуда угодно без проброса портов, несколько устройств в одном "
                          "приложении, вход через Яндекс ID. Записи остаются на устройстве; без облака всё работает как сейчас."},
    "cloud.server": {"en": "Cloud address", "ru": "Адрес облака"},
    "cloud.connect": {"en": "Connect", "ru": "Подключить"},
    "cloud.connecting": {"en": "Connecting…", "ru": "Подключаем…"},
    "cloud.enter_code": {"en": "Sign in to the cloud app and enter this code (valid 10 minutes):",
                         "ru": "Войдите в облачное приложение и введите этот код (действует 10 минут):"},
    "cloud.cancel": {"en": "Cancel", "ru": "Отменить"},
    "cloud.cancel_q": {"en": "Cancel connecting to the cloud?", "ru": "Отменить подключение к облаку?"},
    "cloud.expired": {"en": "The code expired: connect again", "ru": "Код устарел: подключите ещё раз"},
    "cloud.paired": {"en": "Connected; this device in the cloud:", "ru": "Подключено; это устройство в облаке:"},
    "cloud.disconnect": {"en": "Disconnect from the cloud", "ru": "Отвязать от облака"},
    "cloud.disconnect_q": {"en": "Disconnect this device from the cloud? At home everything keeps working.",
                           "ru": "Отвязать это устройство от облака? Дома всё продолжит работать."},
    "cloud.why.crypto": {"en": "python3-cryptography is not installed", "ru": "не установлен python3-cryptography"},
    "cloud.why.address": {"en": "the address must start with https://", "ru": "адрес должен начинаться с https://"},
    "cloud.why.http": {"en": "the cloud answered HTTP {status}", "ru": "облако ответило HTTP {status}"},
    "cloud.why.offline": {"en": "no connection to the cloud", "ru": "нет связи с облаком"},
    "cloud.failed": {"en": "Could not connect to the cloud: {why}", "ru": "Не удалось подключиться к облаку: {why}"},
    "ya.title": {"en": "Yandex Smart Home", "ru": "Умный дом Яндекса"},
    "ya.short": {"en": "Cameras as motion sensors in the Yandex app (Alice): scenarios on motion",
                 "ru": "Камеры как датчики движения в «Доме с Алисой»: сценарии на движение"},
    "ya.open": {"en": "Set up", "ru": "Настроить"},
    "ya.linked": {"en": "Linked to Yandex", "ru": "Связано с Яндексом"},
    "ya.linked_since": {"en": "Linked to Yandex since {since}", "ru": "Связано с Яндексом с {since}"},
    "ya.not_linked": {"en": "Not linked", "ru": "Не связано"},
    "ya.last": {"en": "Last notification: {result}", "ru": "Последнее уведомление: {result}"},
    "ya.intro": {"en": "Every camera becomes a motion sensor in the Yandex app (Alice); hang scenarios on it: the "
                       "speaker says who is at the door, the light turns on. No video goes to Yandex. Yandex "
                       "reaches Zorcam through the Zorcam cloud, at this device's address in it.",
                 "ru": "Каждая камера становится датчиком движения в «Доме с Алисой», на него вешаются сценарии: "
                       "колонка говорит «кто-то у двери», включается свет. Видео в Яндекс не передаётся. Яндекс "
                       "обращается к Zorcam через облако Zorcam, по адресу этого устройства в нём."},
    "ya.unlink": {"en": "Unlink from Yandex", "ru": "Отвязать от Яндекса"},
    "ya.unlink_q": {"en": "Unlink Zorcam from Yandex? The sensors will stop working in the Yandex app.",
                    "ru": "Отвязать Zorcam от Яндекса? Датчики перестанут работать в «Доме с Алисой»."},
    "ya.skill_section": {"en": "1. Skill on dialogs.yandex.ru", "ru": "1. Навык на dialogs.yandex.ru"},
    "ya.skill_intro": {"en": "On dialogs.yandex.ru/developer/smart-home create a smart home skill, access: private. "
                             "Enter these values (Backend and Account linking), make up an application secret, "
                             "save and press Publish.",
                       "ru": "На dialogs.yandex.ru/developer/smart-home создайте навык умного дома, тип доступа — "
                             "приватный. Впишите эти значения (Backend и «Связка аккаунтов»), придумайте секрет "
                             "приложения, сохраните и нажмите «Опубликовать»."},
    "ya.need_cloud": {"en": "Yandex Smart Home works through the Zorcam cloud: connect this device to it first "
                            "(Settings → General → Zorcam cloud). The skill settings are kept if you disconnect later.",
                      "ru": "«Умный дом Яндекса» работает через облако Zorcam: сначала подключите к нему это устройство "
                            "(«Настройки» → «Общие» → «Облако Zorcam»). Настройки навыка сохраняются, "
                            "если позже отключить облако."},
    "ya.back": {"en": "Back to General settings", "ru": "Назад в «Общие»"},
    "ya.endpoint": {"en": "Backend: Endpoint URL", "ru": "Backend: Endpoint URL"},
    "ya.client_id": {"en": "Application ID", "ru": "Идентификатор приложения"},
    "ya.auth_url": {"en": "Authorization URL", "ru": "URL авторизации"},
    "ya.token_url": {"en": "Token URL", "ru": "URL для получения токена"},
    "ya.refresh_url": {"en": "Token refresh URL", "ru": "URL для обновления токена"},
    "ya.keys_section": {"en": "2. Skill keys", "ru": "2. Ключи навыка"},
    "ya.secret": {"en": "Application secret", "ru": "Секрет приложения"},
    "ya.secret_hint": {"en": "The same as in Account linking of the skill", "ru": "Тот же, что в «Связке аккаунтов» навыка"},
    "ya.skill_id": {"en": "Skill ID", "ru": "ID навыка"},
    "ya.skill_id_hint": {"en": "On the skill page: General settings", "ru": "На странице навыка: «Общие сведения»"},
    "ya.skill_token": {"en": "OAuth token for notifications", "ru": "OAuth-токен для уведомлений"},
    "ya.skill_token_hint": {"en": 'Sign in with the account that owns the skill and open <a href="{url}" target="_blank" '
                                  'rel="noopener">this link</a>: Yandex shows the token',
                            "ru": 'Войдите аккаунтом владельца навыка и откройте <a href="{url}" target="_blank" '
                                  'rel="noopener">эту ссылку</a> — Яндекс покажет токен'},
    "ya.keep": {"en": "set — leave empty to keep", "ru": "задан — оставьте пустым"},
    "ya.saved": {"en": "Saved", "ru": "Сохранено"},
    "ya.next_section": {"en": "3. Link and scenario", "ru": "3. Связка и сценарий"},
    "ya.next": {"en": "In the Yandex app (Alice): + → Smart home device → your skill → Link to Yandex, sign in as "
                      "admin. The cameras appear as motion sensors. Then Scenarios → + → When: device → a camera → "
                      "Motion detected; Then: speaker → say a phrase. The sensor reports motion once per event and "
                      "\"no motion\" a minute after the detector goes quiet.",
                "ru": "В «Доме с Алисой»: + → «Устройство умного дома» → ваш навык → «Привязать к Яндексу», войдите "
                      "как admin. Камеры появятся как датчики движения. Затем «Сценарии» → + → «Если»: устройство → "
                      "камера → «Движение»; «То»: колонка → произнести фразу. Датчик сообщает о движении один раз на "
                      "эпизод и «нет движения» через минуту тишины детектора."},
    "ya.auth_title": {"en": "Link Zorcam to Yandex", "ru": "Связать Zorcam с Яндексом"},
    "ya.auth_text": {"en": "Yandex asks for access to the motion sensors of Zorcam (the cameras' names and motion; "
                           "no video). Sign in as admin to allow it.",
                     "ru": "Яндекс просит доступ к датчикам движения Zorcam (названия камер и движение, без видео). "
                           "Войдите как admin, чтобы разрешить."},
    "ya.allow": {"en": "Allow", "ru": "Разрешить"},
    "ya.auth_wrong": {"en": "Wrong name or password, or not the admin", "ru": "Неверное имя или пароль, или это не admin"},
    "onvif.find": {"en": "Find cameras in the network (ONVIF)", "ru": "Найти камеры в сети (ONVIF)"},
    "onvif.title": {"en": "Cameras in the network", "ru": "Камеры в сети"},
    "onvif.intro": {"en": "Cameras that answered an ONVIF search in the home network. Enter the login and password of "
                          "a camera: Zorcam reads its stream addresses and fills in the camera form.",
                    "ru": "Камеры, которые ответили на поиск ONVIF в домашней сети. Введите логин и пароль камеры: "
                          "Zorcam получит адреса её потоков и заполнит форму камеры."},
    "onvif.none": {"en": "No camera answered the ONVIF search. Check that ONVIF is on in the camera settings (some cameras "
                         "keep it off or want a separate ONVIF user) and that the camera is in the same network as the device "
                         "(a camera in another subnet: enter the subnet below); or enter the camera manually.",
                   "ru": "Ни одна камера не ответила на поиск ONVIF. Проверьте, что ONVIF включён в настройках камеры "
                         "(у некоторых он выключен или нужен отдельный пользователь ONVIF) и что камера в той же сети, что "
                         "устройство (камера в другой подсети — укажите подсеть ниже); или введите камеру вручную."},
    "onvif.added": {"en": "already added", "ru": "уже добавлена"},
    "onvif.add": {"en": "Add", "ru": "Добавить"},
    "onvif.again": {"en": "Search again", "ru": "Искать снова"},
    "onvif.searching": {"en": "Searching for cameras…", "ru": "Ищем камеры…"},
    "onvif.reading": {"en": "Asking the camera…", "ru": "Спрашиваем камеру…"},
    "onvif.manual": {"en": "Enter manually", "ru": "Ввести вручную"},
    "onvif.subnet": {"en": "Search", "ru": "Поиск"},
    "onvif.subnet_label": {"en": "Another subnet", "ru": "Другая подсеть"},
    "onvif.subnet_hint": {"en": "optional: cameras behind the router in another subnet, e.g. 192.168.2.0 (home network "
                                "only, up to 254 addresses); the search asks each of its addresses. Kept on this device",
                          "ru": "необязательно: камеры за роутером в другой подсети, например 192.168.2.0 (только "
                                "домашняя сеть, до 254 адресов); поиск спросит каждый её адрес. Запоминается на этом "
                                "устройстве"},
    "onvif.subnet_bad": {"en": "«{subnet}» is not a home subnet of up to 254 addresses (like 192.168.2.0): searched "
                               "only the network of the device",
                         "ru": "«{subnet}» — не домашняя подсеть до 254 адресов (как 192.168.2.0): поиск только "
                               "в сети устройства"},
    "onvif.found": {"en": "Found via ONVIF: the main stream {main}, the additional one {sub}. Check the fields and save.",
                    "ru": "Найдено по ONVIF: основной поток {main}, дополнительный {sub}. Проверьте поля и сохраните."},
    "onvif.found_one": {"en": "Found via ONVIF: the stream {main}; the camera has no lower-resolution stream. Check the "
                              "fields and save.",
                        "ru": "Найдено по ONVIF: поток {main}; облегчённого потока у камеры нет. Проверьте поля "
                              "и сохраните."},
    "onvif.failed": {"en": "ONVIF: {error}. Check the login and password, or fill in the address manually.",
                     "ru": "ONVIF: {error}. Проверьте логин и пароль или впишите адрес вручную."},
    "onvif.err.network": {"en": "the camera does not answer ({detail})", "ru": "камера не отвечает ({detail})"},
    "onvif.err.http": {"en": "the camera answered HTTP {detail}", "ru": "камера ответила HTTP {detail}"},
    "onvif.err.auth": {"en": "the camera does not accept the login and password ({detail}). Many cameras have a separate "
                             "ONVIF user: Hikvision — Network → Advanced → Integration Protocol, Dahua — ONVIF user, "
                             "Tapo — the camera account in the app",
                       "ru": "камера не приняла логин и пароль ({detail}). У многих камер для ONVIF отдельный "
                             "пользователь: Hikvision — Сеть → Расширенные → Протоколы интеграции, Dahua — пользователь "
                             "ONVIF, Tapo — «аккаунт камеры» в приложении"},
    "onvif.err.reply": {"en": "not an ONVIF reply", "ru": "ответ не похож на ONVIF"},
    "onvif.err.fault": {"en": "the camera refused: {detail}", "ru": "камера отказала: {detail}"},
    "onvif.err.no_rtsp": {"en": "the camera gave no RTSP address", "ru": "камера не дала RTSP-адрес"},
    "onvif.err.no_profiles": {"en": "the camera has no video profiles", "ru": "у камеры нет видеопрофилей"},
    "onvif.err.address": {"en": "not a home network address", "ru": "адрес не из домашней сети"},
    "onvif.password_kept": {"en": "the password entered for ONVIF", "ru": "пароль, введённый для ONVIF"},
    "cam.edit_title": {"en": "Camera {name}", "ru": "Камера {name}"},
    "cam.label.id": {"en": "Camera ID", "ru": "ID камеры"},
    "cam.label.name": {"en": "Camera name", "ru": "Название камеры"},
    "cam.label.rtsp_url": {"en": "RTSP address", "ru": "RTSP-адрес"},
    "cam.label.rtsp_user": {"en": "Login", "ru": "Логин"},
    "cam.hint.rtsp_user": {"en": "without a password the camera is used without authentication",
                           "ru": "без пароля камера подключается без авторизации"},
    "cam.label.onvif_url": {"en": "ONVIF address", "ru": "Адрес ONVIF"},
    "cam.hint.onvif_url": {
        "en": "Optional, needed for the pan, tilt and zoom buttons in Live",
        "ru": "Необязательно, требуется для кнопок поворота и зума в Live"},
    "cam.label.rtsp_sub_url": {"en": "Additional stream", "ru": "Дополнительный поток"},
    "cam.hint.rtsp_sub_url": {
        "en": "Recommended: the camera's additional lower-resolution stream, for motion detection. It lowers the load "
              "on the device, the video quality does not suffer. Hikvision: …/Streaming/Channels/102, Dahua: …&subtype=1",
        "ru": "Желательно: укажите дополнительный поток камеры с более низким разрешением, для детекции движения. "
              "Это снизит нагрузку на устройство, качество видео не пострадает. "
              "Hikvision: …/Streaming/Channels/102, Dahua: …&subtype=1"},
    "cam.probe_sub": {"en": "additional address: {error}", "ru": "дополнительный адрес: {error}"},
    "cams.off": {"en": "switched off", "ru": "выключена"},
    "cams.no_frame": {"en": "No frame", "ru": "Нет кадра"},
    "zones.link": {"en": "Motion zones", "ru": "Зоны движения"},
    "zones.title": {"en": "Motion zones: {name}", "ru": "Зоны движения: {name}"},
    "zones.intro": {"en": "Paint the cells of the frame with a finger: a tap or a stroke switches them. "
                          "Painted cells are either left out of motion detection or the only ones counted.",
                    "ru": "Закрасьте клетки кадра пальцем: касание или протяжка переключает их. "
                          "Закрашенное либо исключается из поиска движения, либо только оно и учитывается."},
    "zones.mode": {"en": "Painted cells", "ru": "Закрашенное"},
    "zones.mode.exclude": {"en": "Leave out", "ru": "Исключить закрашенное"},
    "zones.mode.include": {"en": "Count only them", "ru": "Только закрашенное"},
    "zones.grid": {"en": "Frame grid", "ru": "Сетка кадра"},
    "zones.clear": {"en": "Clear", "ru": "Очистить"},
    "zones.all_painted": {"en": "The whole frame is left out: motion will not be detected.",
                          "ru": "Исключён весь кадр: движение не будет обнаруживаться."},
    "zones.note": {"en": "Applies within a few seconds, the camera is not restarted. "
                         "Without painted cells the whole frame is watched.",
                   "ru": "Применяется в течение нескольких секунд, камера не перезапускается. "
                         "Без закрашенных клеток наблюдается весь кадр."},
    "cams.working": {"en": "working", "ru": "работает"},
    "cams.broken": {"en": "not working", "ru": "не работает"},
    "cam.label.enabled": {"en": "Camera on", "ru": "Камера включена"},
    "cam.label.audio": {"en": "Sound in Live and clips", "ru": "Звук в Live и роликах"},
    "cam.hint.audio": {"en": "as the camera sends it, never re-encoded: WebRTC plays G.711 and Opus, HLS and clips take AAC",
                       "ru": "как его отдаёт камера, без перекодирования: WebRTC играет G.711 и Opus, HLS и ролики берут AAC"},
    "ptz.group": {"en": "Camera movement", "ru": "Управление камерой"},
    "ptz.no_onvif": {"en": "Set the ONVIF address in the camera settings",
                     "ru": "Укажите адрес ONVIF в настройках камеры"},
    "ptz.toggle": {"en": "Camera control", "ru": "Управление"},
    "ptz.left": {"en": "Turn left", "ru": "Повернуть влево"},
    "ptz.right": {"en": "Turn right", "ru": "Повернуть вправо"},
    "ptz.up": {"en": "Tilt up", "ru": "Наклонить вверх"},
    "ptz.down": {"en": "Tilt down", "ru": "Наклонить вниз"},
    "ptz.in": {"en": "Zoom in", "ru": "Приблизить"},
    "ptz.out": {"en": "Zoom out", "ru": "Отдалить"},
    "js.ptz_failed": {"en": "The camera did not respond", "ru": "Камера не ответила"},
    "live.sound_on": {"en": "Turn the sound on", "ru": "Включить звук"},
    "live.sound_off": {"en": "Turn the sound off", "ru": "Выключить звук"},
    "cam.label.notify_motion": {"en": "Motion notifications", "ru": "Уведомления о движении"},
    "cam.label.save_videos": {"en": "Save motion videos", "ru": "Сохранять видео движения"},
    "cam.label.clip_seconds": {"en": "Video length, seconds", "ru": "Длина видео, секунд"},
    "cam.label.videos_days": {"en": "Keep videos, days", "ru": "Хранить видео, дней"},
    "cam.label.save_frames": {"en": "Save snapshots", "ru": "Сохранять снимки"},
    "cam.label.frames_interval": {"en": "Snapshot interval, seconds", "ru": "Периодичность снимков, секунд"},
    "cam.label.frames_days": {"en": "Keep snapshots, days", "ru": "Хранить снимки, дней"},
    "cam.password": {"en": "Password", "ru": "Пароль"},
    "cam.password_none": {"en": "no authentication", "ru": "без авторизации"},
    "cam.probe_failed": {"en": "The camera did not accept the connection: {error}. Check the address, login and "
                               "password and enter the password again. To save the camera anyway (for example, it is "
                               "switched off now), tick the box below the form.",
                         "ru": "Камера не приняла подключение: {error}. Проверьте адрес, логин и пароль и введите "
                               "пароль ещё раз. Чтобы всё равно сохранить камеру (например, она сейчас выключена), "
                               "отметьте флажок под формой."},
    "cam.force": {"en": "Save anyway, without checking the camera", "ru": "Сохранить без проверки камеры"},
    "cam.tips_title": {"en": "Recommended camera settings", "ru": "Рекомендуемые настройки камеры"},
    "cam.tips": {"en": "Set these in the camera's own web interface (or its app):<ul>"
                       "<li><b>Main stream H.264</b> (not H.265: browsers cannot show it over WebRTC); sound G.711 (PCMA) for "
                       "Live only, AAC for Live and clips, or off.</li>"
                       "<li><b>1920×1080 or lower, 25 fps</b>, bitrate 2–4 Mbit/s.</li>"
                       "<li><b>I-frame interval (GOP) 10</b>: 25 ÷ 10 = <b>2.5 keyframes/s</b>. The detector "
                       "analyses keyframes only: the more of them, the sooner it notices motion, but with more than "
                       "5 keyframes/s a device may not keep up with several cameras. The rate "
                       "the camera actually sends is on Settings → Status.</li>"
                       "<li>A fixed IP address of the camera (a DHCP reservation on the router).</li></ul>",
                 "ru": "Задаются в веб-интерфейсе самой камеры (или в её приложении):<ul>"
                       "<li><b>Основной поток H.264</b> (не H.265 — браузеры не показывают его по WebRTC); звук "
                       "G.711 (PCMA) только для Live, AAC — для Live и роликов, или выключен.</li>"
                       "<li><b>1920×1080 или меньше, 25 fps</b>, битрейт 2–4 Мбит/с.</li>"
                       "<li><b>Интервал I-кадров (GOP) 10</b>: 25 ÷ 10 = <b>2.5 ключевых кадра/с</b>. Детектор "
                       "анализирует только ключевые кадры: чем их больше, тем быстрее он замечает движение, но "
                       "больше 5 ключевых кадров/с устройство может не успевать обрабатывать для нескольких "
                       "камер. Сколько их на самом деле присылает камера — в «Настройки» → «Статус».</li>"
                       "<li>Постоянный IP-адрес камеры (резервирование DHCP на роутере).</li></ul>"},
    "cam.delete_section": {"en": "Delete the camera", "ru": "Удаление камеры"},
    "cam.delete_note": {"en": "Its recordings can be kept (they are deleted by the storage periods) or deleted right away.",
                        "ru": "Её записи можно оставить (они удалятся по срокам хранения) или удалить сразу."},
    "cam.delete": {"en": "Delete, keep recordings", "ru": "Удалить, записи оставить"},
    "cam.delete_all": {"en": "Delete with recordings", "ru": "Удалить вместе с записями"},
    "cam.confirm_delete": {"en": "Delete the camera {name}? Its recordings stay until the storage periods remove them.",
                           "ru": "Удалить камеру {name}? Её записи останутся, пока их не удалят сроки хранения."},
    "cam.confirm_delete_all": {"en": "Delete the camera {name} and all its frames and videos? This cannot be undone.",
                               "ru": "Удалить камеру {name} и все её кадры и ролики? Это нельзя отменить."},

    # ---- setup wizard (the first visit after installation)
    "setup.title": {"en": "Zorcam setup", "ru": "Настройка Zorcam"},
    "setup.step": {"en": "Step {n} of 3", "ru": "Шаг {n} из 3"},
    "setup.intro": {"en": "Welcome! Three steps: general settings, users, cameras. Everything can be changed later "
                          "on the Settings page.",
                    "ru": "Добро пожаловать! Три шага: общие настройки, пользователи, камеры. Всё можно изменить "
                          "позже на странице «Настройки»."},
    "setup.general": {"en": "General settings", "ru": "Общие настройки"},
    "setup.lang_label": {"en": "Language", "ru": "Язык"},
    "setup.other": {"en": "Other settings (the defaults suit most installations)",
                    "ru": "Другие настройки (значения по умолчанию подходят большинству)"},
    "setup.next": {"en": "Next", "ru": "Далее"},
    "setup.saved": {"en": "Settings saved. The gallery is restarting; the next step opens in a few seconds.",
                    "ru": "Настройки сохранены. Галерея перезапускается, через несколько секунд откроется следующий шаг."},
    "setup.users_intro": {"en": "The administrator password, at least 6 characters. Other users (Live and the gallery, "
                                "or Live only) are added later on Settings → Users.",
                          "ru": "Пароль администратора — не короче 6 символов. Остальных пользователей (Live и галерея "
                                "или только Live) добавите потом в «Настройки» → «Польз.»."},
    "setup.user_title.admin": {"en": "Administrator", "ru": "Администратор"},
    "setup.user_note.admin": {"en": "Everything: Live, recordings, settings, cameras, notifications about failures.",
                              "ru": "Всё: Live, записи, настройки, камеры, уведомления о неисправностях."},
    "setup.repeat": {"en": "repeat the password", "ru": "повторите пароль"},
    "setup.mismatch": {"en": "the passwords do not match", "ru": "пароли не совпадают"},
    "setup.short": {"en": "the password must be at least 6 characters", "ru": "пароль должен быть не короче 6 символов"},
    "setup.ready": {"en": "Ready to add a camera? You need its RTSP address, login and password.",
                    "ru": "Готовы добавить камеру? Понадобятся её RTSP-адрес, логин и пароль."},
    "setup.add_yes": {"en": "Yes, add a camera", "ru": "Да, добавить камеру"},
    "setup.add_later": {"en": "Later (Settings → Add a camera)", "ru": "Позже («Настройки» → «Добавить камеру»)"},
    "setup.added": {"en": "added", "ru": "добавлена"},
    "setup.more": {"en": "Add one more camera?", "ru": "Добавить ещё одну камеру?"},
    "setup.add_more": {"en": "Add another camera", "ru": "Добавить ещё камеру"},
    "setup.finish": {"en": "Finish: open Live", "ru": "Готово — открыть Live"},
    "setup.local_only": {"en": "The setup is only available from the home network: open Zorcam at the device's local "
                               "address (for example, http://192.168.1.10/).",
                         "ru": "Первичная настройка доступна только из домашней сети: откройте Zorcam по локальному "
                               "адресу устройства (например, http://192.168.1.10/)."},

    # ---- access
    "denied.title": {"en": "Access denied", "ru": "Нет доступа"},
    "denied.text": {"en": "This page is not available for your account.",
                    "ru": "Эта страница недоступна для вашей учётной записи."},
}
