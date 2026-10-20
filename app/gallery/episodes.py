"""Эпизод движения камеры: общая логика «Умного дома Яндекса» (yandex.py) и MQTT (mqtt.py).

Эпизод начинается с первого сигнала детектора и держится, пока детектор не молчит HOLD секунд: сигнал приходит
на каждый сохранённый ролик, а наружу («движение» / «нет движения») нужен один переход на эпизод.
"""
import threading
import time

HOLD = 60  # секунд тишины детектора до «нет движения»


class Episodes:
    def __init__(self, hold=HOLD):
        self.hold = hold
        self.lock = threading.Lock()  # сигналы приходят из потоков запросов, tick — из фонового потока
        self.last_motion = {}  # камера -> время последнего сигнала детектора
        self.active = set()  # камеры с идущим эпизодом

    def signal(self, cam_id, now=None, start=True):
        """Сигнал детектора. True — начался новый эпизод (start=False: время сигнала запомнено, но эпизод не открыт)."""
        now = now or time.time()
        with self.lock:
            self.last_motion[cam_id] = now
            if cam_id in self.active or not start:
                return False
            self.active.add(cam_id)
            return True

    def tick(self, now=None):
        """Камеры, у которых эпизод кончился (детектор молчит hold секунд); они перестают быть активными."""
        now = now or time.time()
        with self.lock:
            quiet = sorted(c for c in self.active if now - self.last_motion.get(c, 0) >= self.hold)
            self.active.difference_update(quiet)
        return quiet

    def next_end(self):
        """Когда кончится ближайший из идущих эпизодов, если детектор промолчит; None — идущих нет."""
        with self.lock:
            return min((self.last_motion.get(c, 0) + self.hold for c in self.active), default=None)

    def is_active(self, cam_id):
        with self.lock:
            return cam_id in self.active

    def clear(self):
        with self.lock:
            self.active.clear()
            self.last_motion.clear()
