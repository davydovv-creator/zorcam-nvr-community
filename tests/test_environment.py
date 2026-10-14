"""The stand of the device (prod, stage, test): the `environment` parameter of zorcam.conf sets the default cloud address,
the badge and the name in the app header, the page title and the app name under the Home Screen icon.
The value is read when the gallery starts, so each stand is a separate interpreter with its own config."""
import json
import os
import subprocess
import sys

import pytest

from conftest import REPO, SRC

PROBE = r"""
import json, sys
import app
cloud = app.cloud_section("en", "x")
main = app.render("live", role="admin", user="admin")
settings = app.render_settings("admin", "en", "admin", csrf="x", tab="status")
print(json.dumps({"name": app.APP_NAME, "cloud": app.CLOUD_DEFAULT, "cloud_form": cloud, "main": main, "settings": settings,
                  "tabbar": app.tab_bar("en", "admin", "live", user="admin")}))
"""


def stand(tmp_path, env=None, cloud_url=None, cloud=None):
    """The gallery module imported as on a device with `environment = <env>` (None: the line is absent)."""
    state = tmp_path / "state"
    (state / "config").mkdir(parents=True)
    if cloud:
        (state / "config" / "cloud.json").write_text(json.dumps(cloud), encoding="utf-8")
    if env:
        (state / "config" / "zorcam.conf").write_text(f"[gallery]\nenvironment = {env}\n", encoding="utf-8")
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("ZORCAM_")}
    run_env.update(HOME=str(tmp_path), ZORCAM_STATE_DIR=str(state),
                   ZORCAM_STORAGE_DIR=str(tmp_path / "storage"), PYTHONPATH=os.pathsep.join(
                       os.path.join(SRC, d) for d in ("common", "gallery", "storage")))
    if cloud_url:
        run_env["ZORCAM_CLOUD_URL"] = cloud_url
    out = subprocess.run([sys.executable, "-c", PROBE], env=run_env, cwd=REPO, check=True, capture_output=True, text=True)
    return json.loads(out.stdout.splitlines()[-1])


@pytest.mark.parametrize("env,name,cloud,badge", [
    (None, "Zorcam", "https://cloud.zorcam.ru", ""),
    ("prod", "Zorcam", "https://cloud.zorcam.ru", ""),
    ("stage", "Zorcam stage", "https://cloud.stage.zorcam.ru", "STAGE"),
    ("test", "Zorcam test", "https://cloud.test.zorcam.ru", "TEST"),
])
def test_environment_sets_cloud_badge_and_title(tmp_path, env, name, cloud, badge):
    got = stand(tmp_path, env)
    assert got["name"] == name and got["cloud"] == cloud
    assert f'value="{cloud}"' in got["cloud_form"]  # Settings -> General: the default address of the cloud
    for page in ("main", "settings"):
        html = got[page]
        head = html[:html.index("</head>")]
        assert f"<title>{name} " in head and f'<meta name="apple-mobile-web-app-title" content="{name}">' in head
        body = html[html.index("</head>"):]
        marker = '<span class="envbadge">'
        if badge:
            assert f'</button>{marker}{badge}</span></span>' in body, page
        else:
            assert marker not in body and '<span class="appname"><span>Zorcam.nvr</span><button' in body, page
    # the sidebar of a laptop: the name and the same badge, no second header
    assert got["tabbar"].count('class="brand"') == 1
    assert (f'</button><span class="envbadge">{badge}</span></div>' in got["tabbar"]) == bool(badge)
    # Settings -> Status shows the stand only when it is not prod
    status_row = '<div class="k">Environment</div>'
    assert (status_row in got["settings"]) == bool(badge)
    if badge:
        assert f'{status_row}<div class="v ">{env}</div>' in got["settings"]


def test_cloud_url_variable_beats_the_environment(tmp_path):
    got = stand(tmp_path, "test", cloud_url="https://cloud.example.test")
    assert got["cloud"] == "https://cloud.example.test"
    assert 'value="https://cloud.example.test"' in got["cloud_form"]


def test_saved_cloud_address_stays(tmp_path):
    """An address already saved by an earlier pairing is not replaced by the default of the stand."""
    got = stand(tmp_path, "test", cloud={"server": "https://cloud.zorcam.ru", "device_id": "dev1234567"})
    assert got["cloud"] == "https://cloud.test.zorcam.ru"  # the default of the stand ...
    assert 'value="https://cloud.zorcam.ru"' in got["cloud_form"]  # ... but the form keeps the saved address
    paired = stand(tmp_path / "paired", "test", cloud={"server": "https://cloud.zorcam.ru", "device_id": "dev1234567",
                                                      "paired": True, "url": "https://abc.zorcam.ru"})
    assert "https://abc.zorcam.ru" in paired["cloud_form"] and "cloud.test.zorcam.ru" not in paired["cloud_form"]
