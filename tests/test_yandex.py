"""app/gallery/yandex.py: the OAuth server for the Yandex skill and the motion sensor state, without HTTP."""
import base64

import yandex


def make(tmp_path):
    ya = yandex.Yandex(path=str(tmp_path / "yandex.json"), log=lambda m: None)
    ya.save_settings("skill", "sec", "tok")
    return ya


def test_redirect_only_to_yandex():
    ya = yandex.Yandex(path="/nonexistent/x.json")
    assert ya.valid_redirect("https://social.yandex.net/broker/redirect")
    for uri in ("http://social.yandex.net/", "https://social.yandex.net.evil.com/", "https://evil.com/?social.yandex.net",
                "https://user@social.yandex.net/", "https://social.yandex.net/\r\nSet-Cookie: x=1", "", None):
        assert not ya.valid_redirect(uri), uri


def test_token_grants_and_basic_auth(tmp_path):
    ya = make(tmp_path)
    code = ya.issue_code("https://social.yandex.net/r", "admin")
    basic = "Basic " + base64.b64encode(b"zorcam:sec").decode()
    assert ya.token({"grant_type": "password"}, basic) == (400, {"error": "unsupported_grant_type"})
    assert ya.token({"grant_type": "authorization_code", "code": code, "redirect_uri": "https://social.yandex.net/x"},
                    basic)[0] == 400  # another redirect address than the code was given for
    code = ya.issue_code("https://social.yandex.net/r", "admin")
    status, tok = ya.token({"grant_type": ["authorization_code"], "code": [code]}, basic)
    assert status == 200 and ya.check_access("Bearer " + tok["access_token"]) and not ya.check_access("Bearer x")
    assert isinstance(tok["expires_in"], int) and len(tok["access_token"]) < 2048
    assert tok["refresh_token"] not in (tmp_path / "yandex.json").read_text()  # only hashes on the disk
    assert ya.ready() and ya.status()["linked"]
    # the settings survive a restart, the secret is kept when the field is left empty
    again = yandex.Yandex(path=str(tmp_path / "yandex.json"))
    again.save_settings("skill2")
    assert again.data["client_secret"] == "sec" and again.data["skill_token"] == "tok" and again.ready()


def test_no_secret_no_tokens(tmp_path):
    ya = yandex.Yandex(path=str(tmp_path / "yandex.json"))
    code = ya.issue_code("https://social.yandex.net/r", "admin")
    assert ya.token({"grant_type": "authorization_code", "code": code, "client_id": "zorcam",
                     "client_secret": ""}) == (401, {"error": "invalid_client"})


def test_motion_without_link_is_not_sent(tmp_path):
    ya = make(tmp_path)  # keys saved, but Yandex has not linked yet
    ya.motion("front")
    assert ya.outbox.empty() and ya.state("front") == "not_detected"
