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
