import string

import pytest

import zorcam_i18n as i18n


def fields(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_string_has_both_languages_with_the_same_placeholders():
    for key, entry in i18n.STRINGS.items():
        if key == "raw":
            continue
        assert set(entry) == set(i18n.LANGS), key
        assert fields(entry["en"]) == fields(entry["ru"]), key
        if key.startswith("js."):
            assert "\\" not in entry["en"] + entry["ru"], key


@pytest.mark.parametrize("header,lang", [
    (None, "en"),
    ("", "en"),
    ("ru-RU,ru;q=0.9,en-US;q=0.8", "ru"),
    ("en-US,en;q=0.9,ru;q=0.8", "en"),
    ("de-DE,de;q=0.9", "en"),
    ("de-DE,ru;q=0.5,en;q=0.3", "ru"),
    ("en;q=0.2,ru;q=0.8", "ru"),
    ("ru;q=0", "en"),
    ("RU", "ru"),
])
def test_device_language(header, lang):
    assert i18n.from_accept_language(header) == lang


def test_setting_wins_over_the_device():
    assert i18n.resolve("auto", "ru") == "ru"
    assert i18n.resolve("en", "ru") == "en"
    assert i18n.resolve("ru", None) == "ru"


def test_messages_translate_nested_arguments():
    msg = i18n.Msg("health.with_error", text=i18n.Msg("check.disk"), error=i18n.Raw("{not a field}"))
    assert msg.text("en") == "Disk space; {not a field}"
    assert str(msg) == msg.text("en")
    assert msg.text("ru").endswith("{not a field}") and msg.text("ru") != msg.text("en")
