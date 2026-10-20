import os
import subprocess
import sys

import pytest

import zorcam_users


@pytest.fixture(autouse=True)
def users_file(tmp_path, monkeypatch):
    monkeypatch.setattr(zorcam_users, "USERS_FILE", str(tmp_path / "users.json"))


def test_set_verify_delete():
    zorcam_users.set_user("bob", "viewer", "secret1")
    assert zorcam_users.verify("bob", "secret1") == "viewer"
    assert zorcam_users.verify("bob", "wrong") is None
    assert zorcam_users.verify("nobody", "secret1") is None
    assert zorcam_users.role_of("bob") == "viewer"
    zorcam_users.set_user("bob", "liveviewer", "secret2")  # change role and password
    assert zorcam_users.verify("bob", "secret1") is None
    assert zorcam_users.verify("bob", "secret2") == "liveviewer"
    assert zorcam_users.delete_user("bob") is True
    assert zorcam_users.delete_user("bob") is False
    assert zorcam_users.role_of("bob") is None


def test_camera_access_is_kept_and_forgotten_with_the_camera():
    zorcam_users.set_user("bob", "viewer", "secret1")
    assert zorcam_users.cameras_of("bob") == set()  # no camera until admin switches them on
    zorcam_users.set_cameras("bob", {"yard", "gate"})
    zorcam_users.set_user("bob", "viewer", "secret2")  # a new password keeps the camera access
    assert zorcam_users.cameras_of("bob") == {"yard", "gate"}
    zorcam_users.forget_camera("yard")  # the camera is deleted: a new one with this ID is closed again
    assert zorcam_users.cameras_of("bob") == {"gate"}


def test_old_camera_access_is_migrated():
    """users.json where new cameras were open to all ("cameras_off"): each user keeps seeing the same cameras."""
    zorcam_users.save({"admin": {"role": "admin", "salt": "00", "hash": "x"},
                      "bob": {"role": "viewer", "salt": "00", "hash": "x", "cameras_off": ["yard"]},
                      "guest": {"role": "liveviewer", "salt": "00", "hash": "x"},
                      "new": {"role": "viewer", "salt": "00", "hash": "x", "cameras": ["gate"]}})
    assert zorcam_users.migrate_camera_access(["front", "yard", "gate"]) == ["bob", "guest"]
    users = zorcam_users.load()
    assert users["bob"]["cameras"] == ["front", "gate"] and "cameras_off" not in users["bob"]
    assert users["guest"]["cameras"] == ["front", "gate", "yard"] and users["new"]["cameras"] == ["gate"]
    assert "cameras" not in users["admin"]
    assert zorcam_users.migrate_camera_access(["front", "yard", "gate", "garage"]) == []  # once only
    zorcam_users.set_user("cli", "viewer", "secret1")  # created on the Pi: no camera, and the migration keeps it so
    assert zorcam_users.migrate_camera_access(["front"]) == [] and zorcam_users.cameras_of("cli") == set()


def test_passwords_are_not_stored(tmp_path):
    zorcam_users.set_user("admin", "admin", "plain-password")
    text = (tmp_path / "users.json").read_text(encoding="utf-8")
    assert "plain-password" not in text
    assert oct((tmp_path / "users.json").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("name,role,password", [
    ("x", "root", "secret1"),
    ("x", "superuser", "secret1"),
    ("with space", "viewer", "secret1"),
    ("", "viewer", "secret1"),
    ("short", "viewer", "12345"),
    ("alice", "admin", "secret1"),   # the administrator is the one user named admin
    ("admin", "viewer", "secret1"),
    ("ADMIN", "liveviewer", "secret1"),
    ("x" * 33, "viewer", "secret1"),
])
def test_rejects_bad_input(name, role, password):
    with pytest.raises(ValueError):
        zorcam_users.set_user(name, role, password)


def test_only_known_roles_are_loaded(tmp_path):
    import json
    (tmp_path / "users.json").write_text(json.dumps({
        "v": {"role": "viewer", "salt": "00", "hash": "x"}, "old": {"role": "superuser", "salt": "00", "hash": "x"},
        "junk": "x"}), encoding="utf-8")
    assert {n: v["role"] for n, v in zorcam_users.load().items()} == {"v": "viewer"}


def test_names_are_found_without_regard_to_case():
    zorcam_users.set_user("Slava", "viewer", "secret1")
    assert zorcam_users.find("slava") == zorcam_users.find("SLAVA") == "Slava" and zorcam_users.find("nobody") is None
    assert zorcam_users.verify("sLaVa", "secret1") == "viewer" and zorcam_users.verify("slava", "wrong") is None
    with pytest.raises(ValueError):
        zorcam_users.set_user("slava", "viewer", "secret2")  # the same name in other letter case


def test_the_installer_mark_is_set_only_by_initial_and_cleared_by_any_other_password():
    zorcam_users.set_user("admin", "admin", "from-installer", initial=True)
    assert zorcam_users.is_initial("admin") and zorcam_users.load()["admin"]["initial"] is True
    assert zorcam_users.verify("admin", "from-installer") == "admin"
    zorcam_users.set_user("admin", "admin", "chosen-by-owner")  # any other way of setting the password
    assert not zorcam_users.is_initial("admin") and "initial" not in zorcam_users.load()["admin"]
    zorcam_users.set_user("admin", "admin", "from-installer", initial=True)
    zorcam_users.clear_initial("admin")  # Skip: the password stays, the question goes
    assert not zorcam_users.is_initial("admin") and zorcam_users.verify("admin", "from-installer") == "admin"
    zorcam_users.clear_initial("admin")  # nothing to clear: no write, no error
    zorcam_users.clear_initial("nobody")
    assert not zorcam_users.is_initial("nobody") and not zorcam_users.is_initial("")
    zorcam_users.set_user("bob", "viewer", "secret1")
    zorcam_users.set_cameras("bob", {"yard"})
    zorcam_users.set_user("bob", "viewer", "secret2", initial=True)  # what else the user has stays
    assert zorcam_users.cameras_of("bob") == {"yard"} and zorcam_users.load()["bob"]["initial"] is True


def cli(tmp_path, *args, stdin=""):
    """zorcam_users.py as the installer of a build runs it (sudo -u zorcam-nvr python3 …/zorcam_users.py …)."""
    env = {**os.environ, "ZORCAM_USERS": str(tmp_path / "users.json")}
    return subprocess.run([sys.executable, zorcam_users.__file__, *args], input=stdin, capture_output=True, text=True, env=env)


def test_cli_takes_the_password_from_stdin_and_marks_it_initial(tmp_path):
    secret = "installer-secret-1"
    done = cli(tmp_path, "set", "admin", "admin", "--initial", "--password-stdin", stdin=secret + "\n")
    assert done.returncode == 0 and "saved: admin (admin)" in done.stdout and "asks to change it" in done.stdout
    assert secret not in done.stdout + done.stderr  # the password is not echoed, and it was not an argument either
    assert zorcam_users.verify("admin", secret) == "admin" and zorcam_users.is_initial("admin")
    assert secret not in (tmp_path / "users.json").read_text(encoding="utf-8")
    # the flags in any order; CRLF and no newline at the end are fine; without --initial the mark goes
    done = cli(tmp_path, "set", "--password-stdin", "admin", "admin", stdin="owner-secret-2\r\n")
    assert done.returncode == 0 and "asks to change it" not in done.stdout
    assert zorcam_users.verify("admin", "owner-secret-2") == "admin" and not zorcam_users.is_initial("admin")
    assert cli(tmp_path, "set", "admin", "admin", "--password-stdin", "--initial", stdin="last-secret-3").returncode == 0
    assert zorcam_users.verify("admin", "last-secret-3") == "admin" and zorcam_users.is_initial("admin")
    done = cli(tmp_path, "list")
    assert "admin" in done.stdout and "last-secret-3" not in done.stdout


@pytest.mark.parametrize("args,stdin,message", [
    (("set", "admin", "admin", "--password-stdin"), "", "at least 6 characters"),  # nothing on stdin
    (("set", "admin", "admin", "--password-stdin"), "abc\n", "at least 6 characters"),
    (("set", "admin", "viewer", "--password-stdin"), "secret-1\n", "the administrator is the one user named admin"),
    (("set", "admin", "--password-stdin"), "secret-1\n", "set NAME ROLE"),  # a role is missing: the usage
    (("set", "admin", "admin", "--initial", "--stdin"), "secret-1\n", "set NAME ROLE"),  # an unknown flag
])
def test_cli_refuses_without_changing_anything(tmp_path, args, stdin, message):
    zorcam_users.set_user("admin", "admin", "kept-secret")
    done = cli(tmp_path, *args, stdin=stdin)
    assert done.returncode == 1 and message in done.stderr and "secret-1" not in done.stdout + done.stderr
    assert zorcam_users.verify("admin", "kept-secret") == "admin" and not zorcam_users.is_initial("admin")
