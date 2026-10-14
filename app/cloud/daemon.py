#!/usr/bin/env python3
"""zorcam-nvr-cloud: the cloud connection of this Pi (only when the owner connected it in Settings).

Finishes the pairing (while the code is on the gallery page), keeps the tunnel to the cloud running (frpc) and
tells the cloud once a minute that the Pi is up. Not connected: it only waits, the Pi works on its own.
"""
import os
import subprocess
import sys
import time

sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))  # run from the repository
import zorcam_cloud as cloud  # noqa: E402
import zorcam_config  # noqa: E402

FRPC = os.environ.get("ZORCAM_FRPC", "/usr/local/bin/frpc")
VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gallery", "BUILD")
CHILD_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
HEALTH_EVERY = 3600  # отчёт о самочувствии в админку облака; первый — через 2 мин после подключения
HEALTH_FIRST = 120


def log(msg):
    sys.stdout.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}\n")
    sys.stdout.flush()


def version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().split()[0]
    except (OSError, IndexError):
        return ""


class Tunnel:
    """frpc as a child process: started with the current settings, restarted when they change or it exits."""

    def __init__(self):
        self.proc, self.config, self.started = None, None, 0.0

    def run(self, data):
        config = cloud.frpc_config(data)
        if self.proc and self.proc.poll() is None and config == self.config:
            return
        if self.proc and self.proc.poll() is None:
            self.stop()
        elif self.proc:
            log(f"tunnel: frpc exited with code {self.proc.returncode}, restarting it")
        if time.time() - self.started < 10:  # не чаще раза в 10 с, если frpc сразу падает
            return
        fd = os.open(cloud.FRPC_CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # the file may be left from before with other rights: it holds the tunnel token
        with os.fdopen(fd, "w") as f:
            f.write(config)
        self.proc = subprocess.Popen([FRPC, "-c", cloud.FRPC_CONFIG], stdin=subprocess.DEVNULL, env=CHILD_ENV)
        self.config, self.started = config, time.time()
        log(f"tunnel: frpc started for {data.get('url')}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            log("tunnel: frpc stopped")
        self.proc, self.config = None, None


def main():
    tunnel = Tunnel()
    zorcam_config.sd_notify("READY=1")
    last_beat, was = 0.0, None
    next_health = time.time() + HEALTH_FIRST
    while True:
        zorcam_config.sd_notify("WATCHDOG=1")
        st = cloud.state()
        try:
            if st == "pending":
                st = cloud.finish_pairing(version())
                if st == "paired":
                    log("paired with the cloud")
            if st == "paired":
                if time.time() - last_beat >= 60:
                    last_beat = time.time()
                    if not cloud.heartbeat(version()):
                        log("the cloud no longer knows this Pi (unlinked in the app): disconnecting")
                        cloud.remove()
                        st = "none"
                if st == "paired":
                    tunnel.run(cloud.load())
                if st == "paired" and time.time() >= next_health:
                    next_health = time.time() + HEALTH_EVERY
                    status = cloud.send_health(cloud.health_report(version()))
                    if status != 200:
                        log(f"health report: the cloud answered {status}")
            if st != "paired":
                tunnel.stop()
        except Exception as e:  # noqa: BLE001 — один сбой не должен останавливать сервис
            log(f"error: {e}")
        if st != was:
            log(f"state: {st}")
            was = st
        time.sleep(3 if st == "pending" else 5)


if __name__ == "__main__":
    main()
