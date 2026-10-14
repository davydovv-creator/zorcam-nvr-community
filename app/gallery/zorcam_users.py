#!/usr/bin/env python3
"""Zorcam gallery users and roles: /var/lib/zorcam-nvr/config/users.json (PBKDF2 password hashes only, mode 600).
Every user but admin has "cameras": IDs of the cameras this user can see (Settings -> Users); a camera added
later is closed to everyone until admin switches it on for them.

Roles:
  liveviewer - Live only: no recordings, settings or push
  viewer     - Live, videos and frames, motion push notifications
  admin      - everything: the same + Settings, cameras, /config and push notifications about failures;
               there is one administrator, and its name is admin

admin is created in the setup wizard of the gallery (the first visit after installation); the other users
are added on Settings -> Users, or with this command.

    sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_users.py list
    sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_users.py set NAME ROLE   # create, or change the password/role
    sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_users.py del NAME
"""
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading

USERS_FILE = os.environ.get("ZORCAM_USERS", os.path.join(os.environ.get("ZORCAM_STATE_DIR", "/var/lib/zorcam-nvr"), "config",
                                                      "users.json"))
ROLES = ("liveviewer", "viewer", "admin")
ADMIN = "admin"  # единственный администратор; у остальных — роль viewer или liveviewer
MAX_NAME = 32
ROLE_TITLES = {"liveviewer": "Live only", "viewer": "Live and video archive", "admin": "full access"}
ITERATIONS = 200_000


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def _read():
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load():
    return {n: v for n, v in _read().items() if isinstance(v, dict) and v.get("role") in ROLES}


def save(users):
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=1)
    os.replace(tmp, USERS_FILE)


def name_ok(name):
    """Имя пользователя: без пробелов, не длиннее MAX_NAME."""
    return bool(name) and len(name) <= MAX_NAME and not any(c.isspace() or not c.isprintable() for c in name)


def set_user(name, role, password):
    if role not in ROLES:
        raise ValueError(f"role must be one of: {', '.join(ROLES)}")
    if not name_ok(name):
        raise ValueError(f"the name must not be empty, contain spaces or be longer than {MAX_NAME} characters")
    if (role == "admin") != (name == ADMIN) or (role != "admin" and name.lower() == ADMIN):
        raise ValueError(f"the administrator is the one user named {ADMIN}; other users are viewer or liveviewer")
    if find(name) not in (None, name):
        raise ValueError(f"the user {find(name)} exists: names differ by more than letter case")
    if len(password) < 6:
        raise ValueError("the password must be at least 6 characters")
    _store(name, role, password)


def _store(name, role, password):
    users = load()
    salt = secrets.token_hex(16)
    # новый пользователь не видит ни одной камеры, пока admin их не включит; у существующего cameras сохраняется
    old = users.get(name) or ({} if role == "admin" else {"cameras": []})
    users[name] = {**old, "role": role, "salt": salt, "hash": _hash(password, salt)}
    save(users)


def set_role(name, role):
    """Роль viewer или liveviewer существующего пользователя (не admin) без смены пароля."""
    users = load()
    if name not in users or name == ADMIN or role not in ("viewer", "liveviewer"):
        raise ValueError("only a viewer or liveviewer can get another role, viewer or liveviewer")
    users[name]["role"] = role
    save(users)


def cameras_of(name):
    """IDs of the cameras the user can see (admin sees every camera without this list)."""
    ids = load().get(name, {}).get("cameras")
    return {c for c in ids if isinstance(c, str)} if isinstance(ids, list) else set()


def set_cameras(name, ids):
    users = load()
    if name not in users:
        return
    users[name]["cameras"] = sorted(ids)
    save(users)


_notify_lock = threading.Lock()


def notify_prefs(name):
    """Personal motion push settings: (all cameras on?, IDs of the cameras the user switched off). Default: all on."""
    user = load().get(name, {})
    off = user.get("notify_off")
    return user.get("notify_all", True) is not False, {c for c in off if isinstance(c, str)} if isinstance(off, list) else set()


def wants_push(name, cam_id):
    all_on, off = notify_prefs(name)
    return all_on and cam_id not in off


def set_notify(name, camera, on):
    """camera "all" is the general switch, otherwise a camera ID. Written at once (the page has no Save button)."""
    with _notify_lock:
        users = load()
        if name not in users:
            return False
        user = users[name]
        if camera == "all":
            user["notify_all"] = bool(on)
        else:
            off = {c for c in (user.get("notify_off") or []) if isinstance(c, str)}
            off.discard(camera) if on else off.add(camera)
            user["notify_off"] = sorted(off)
        save(users)
    return True


def forget_camera(cam_id):
    """A deleted camera: drop it from every user's list (a new camera with this ID is closed to all again)."""
    users = load()
    changed = False
    for u in users.values():
        if cam_id in (u.get("notify_off") or []):
            u["notify_off"] = [c for c in u["notify_off"] if c != cam_id]
            changed = True
        if cam_id in (u.get("cameras") or []):
            u["cameras"] = [c for c in u["cameras"] if c != cam_id]
            changed = True
    if changed:
        save(users)


def migrate_camera_access(camera_ids):
    """users.json of the version where new cameras were visible to all ("cameras_off", the closed ones):
    every user keeps seeing what they saw, as "cameras" (the open ones). Returns the migrated names."""
    users = load()
    done = []
    for name, u in users.items():
        if u["role"] != "admin" and "cameras" not in u:
            off = set(u.pop("cameras_off", None) or [])
            u["cameras"] = sorted(set(camera_ids) - off)
            done.append(name)
    if done:
        save(users)
    return done


def delete_user(name):
    users = load()
    if users.pop(name, None) is None:
        return False
    save(users)
    return True


def find(name):
    """The stored name of the user, looked up without regard to case (Slava, slava, SLAVA), or None."""
    users = load()
    if name in users:
        return name
    return next((n for n in users if n.casefold() == name.casefold()), None)


def verify(name, password):
    """The user's role if the password is right, otherwise None (the name is looked up without regard to case)."""
    stored = find(name)
    u = load().get(stored) if stored else None
    if not u:
        _hash(password, "00" * 16)  # the same response time for unknown names
        return None
    return u["role"] if hmac.compare_digest(_hash(password, u["salt"]), u["hash"]) else None


def role_of(name):
    u = load().get(name)
    return u["role"] if u else None


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if args[:1] == ["list"]:
            users = load()
            if not users:
                print("no users yet: open the gallery in a browser to run the setup wizard")
            for name, u in sorted(users.items()):
                cams = "every camera" if u["role"] == "admin" else ", ".join(sorted(cameras_of(name))) or "no cameras"
                print(f"{name:20} {u['role']:10} {ROLE_TITLES[u['role']]}; cameras: {cams}")
        elif args[:1] == ["set"] and len(args) == 3:
            p1 = getpass.getpass(f"Password for {args[1]}: ")
            p2 = getpass.getpass("Repeat: ")
            if p1 != p2:
                sys.exit("passwords do not match")
            set_user(args[1], args[2], p1)
            print(f"saved: {args[1]} ({args[2]}). Sessions this user already has stay signed in.")
            if args[2] != "admin" and not cameras_of(args[1]):
                print("this user sees no camera yet: switch the cameras on on Settings -> Users")
        elif args[:1] == ["del"] and len(args) == 2:
            print("deleted" if delete_user(args[1]) else "no such user")
        else:
            sys.exit(__doc__)
    except ValueError as e:
        sys.exit(f"error: {e}")
