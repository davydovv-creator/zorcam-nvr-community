"""Shared test setup: a temporary state folder instead of /var/lib/zorcam-nvr (and a temporary HOME), so Zorcam modules
(paths are computed at import) never touch the real config, recordings and the detector state in /dev/shm."""
import os
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="zorcam-test-home-")
os.makedirs(os.path.join(HOME, "config"))
os.environ["HOME"] = HOME
os.environ["ZORCAM_STATE_DIR"] = HOME
os.environ["ZORCAM_STORAGE_DIR"] = os.path.join(HOME, "storage-root")  # the recordings disk service (root)
for var in ("INVOCATION_ID", "NOTIFY_SOCKET", "ZORCAM_CONFIG", "ZORCAM_SECRETS", "ZORCAM_USERS", "ZORCAM_CAMERAS"):
    os.environ.pop(var, None)  # runners under systemd have INVOCATION_ID: the gallery would try to restart itself

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "app")
for sub in ("common", "gallery", "capture", "storage"):
    sys.path.insert(0, os.path.join(SRC, sub))

import zorcam_config  # noqa: E402

# detector state files in the temporary folder too: on a Pi /dev/shm holds the real ones
SHM = os.path.join(HOME, "shm")
os.makedirs(SHM)
zorcam_config.capture_status_file = lambda cam_id: os.path.join(SHM, f"zorcam_capture_{cam_id}.json")

# a fake ONVIF camera (tests/test_onvif.py) for the gallery tests too
from test_onvif import fake_camera, ptz_camera  # noqa: E402,F401
