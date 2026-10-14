import os
import re
import shutil

import pytest

import app
from conftest import REPO


def test_changelog_matches_version():
    """The latest released version in CHANGELOG.md equals VERSION in app.py (a release bumps both and adds a tag)."""
    with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as f:
        text = f.read()
    released = re.findall(r"^## \[(\d+\.\d+(?:\.\d+)?)\]", text, re.M)
    assert released, "CHANGELOG.md has no versions"
    assert released[0] == app.VERSION


ENGLISH_ONLY = ["README.md", "config/zorcam.conf.example", "config/secrets.env.example",
                "app/install.sh", "app/uninstall.sh", "app/debug.sh", "app/diagnose.py",
                "app/go2rtc/go2rtc.yaml", "app/capture/zorcam-nvr-capture.service", "app/gallery/zorcam-nvr-gallery.service",
                "app/go2rtc/zorcam-nvr-go2rtc.service", ".github/workflows/ci.yml", "pyproject.toml",
                "app/storage/zorcam-nvr-storage.service", "app/storage/zorcam-nvr-storage.timer",
                "app/storage/zorcam-nvr-storage.path"]


def test_config_files_scripts_and_tests_are_in_english():
    """Settings files, install and test scripts are always in English (issue #5); UI text lives in zorcam_i18n."""
    tests = [os.path.join("tests", f) for f in os.listdir(os.path.join(REPO, "tests")) if f.endswith(".py")]
    for rel in ENGLISH_ONLY + tests:
        with open(os.path.join(REPO, rel), encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if "(README.ru.md)" in line:  # the language switch names Russian in Russian
                    continue
                assert not re.search("[\u0400-\u04ff]", line), f"{rel}:{n}: {line.strip()}"


def test_readmes_link_to_each_other_and_cover_the_same_sections():
    """Issue #8: README.md in English, README.ru.md in Russian, the same structure."""
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        en = f.read()
    with open(os.path.join(REPO, "README.ru.md"), encoding="utf-8") as f:
        ru = f.read()
    assert "(README.ru.md)" in en and "(README.md)" in ru
    assert len(re.findall(r"^#{2,3} ", en, re.M)) == len(re.findall(r"^#{2,3} ", ru, re.M))
    assert re.search("[\u0400-\u04ff]", ru)


def test_debug_off_removes_everything_on_adds():
    """app/debug.sh: production after "off" means no monitor, no drop-ins, no sysctl and no journal on disk."""
    with open(os.path.join(REPO, "app", "debug.sh"), encoding="utf-8") as f:
        src = f.read()
    on = src[src.index("\non() {"):src.index("\noff() {")]
    off = src[src.index("\noff() {"):src.index("\nstatus() {")]
    for thing in ("$MONITOR_UNIT", "$MONITOR_BIN", "$JOURNALD_CONF", "$SYSCTL_CONF", '$(dropin "$s")', "/var/log/journal"):
        assert thing in on and thing in off, thing
    assert "ZORCAM_DEBUG=1" in on and "ZORCAM_GO2RTC_LOG=debug" in on


def test_install_checks_the_gallery_on_healthz():
    """On a fresh install /login redirects to the setup wizard (303): the check must not call that a failure."""
    with open(os.path.join(REPO, "app", "install.sh"), encoding="utf-8") as f:
        src = f.read()
    assert "http://127.0.0.1/healthz" in src and "http://127.0.0.1/login" not in src


def test_install_does_not_touch_the_watched_settings_needlessly():
    """zorcam-nvr-go2rtc-config.path restarts go2rtc on any change of zorcam.conf, cameras.json or secrets.env, a chmod
    included: a repeated install.sh must not chmod them when the mode is already right."""
    with open(os.path.join(REPO, "app", "install.sh"), encoding="utf-8") as f:
        lines = f.read().splitlines()
    for line in lines:
        if "chmod" in line and "dirname" not in line and any(v in line for v in ("$SECRETS", "$CONF", "cameras.json")):
            assert "stat -c %a" in line and "||" in line, line


def test_install_renames_an_old_installation_first_and_step_by_step():
    """The block "the old names" (zorcam): before the service user is made (else a second user would
    appear next to the old one), and every step guarded, so a run stopped halfway finishes the rest when repeated."""
    with open(os.path.join(REPO, "app", "install.sh"), encoding="utf-8") as f:
        text = f.read()
    block = text[text.index("# ---------------------------------------------------------------- the old names"):
                 text.index('step "Service user $SVC_USER"')]
    # entered while anything old is left: the unit, the user or the state folder
    assert "old=zorcam\n" in block
    assert '[ -f "$UNIT_DIR/$old-gallery.service" ] || id "$old"' in block and 'sudo test -d "/var/lib/$old"' in block
    for step, guard in [('sudo groupmod -n', '! getent group "$SVC_USER"'),
                        ('sudo usermod -l', '! id "$SVC_USER"'),
                        ('sudo mv "/var/lib/$old" "$STATE"', '! sudo test -e "$STATE"'),
                        ]:
        assert step in block and guard in block[:block.index(step)], step
    # the services stop before the user is renamed (usermod refuses a user with running processes)
    assert block.index("systemctl disable -q --now") < block.index("sudo usermod -l")
    # units one by one: a wildcard zorcam-* would also remove zorcam-nvr-*
    assert '"$UNIT_DIR/$old"-*' not in block and "$old-*" not in block
    # the temporary files keep their names (zorcam_* in /dev/shm)
    assert "/dev/shm" not in block



def test_community_snapshot_has_only_the_core(tmp_path, monkeypatch):
    """tools/community_export.py: the public snapshot (zorcam-nvr-community) gets the core and community/, never the
    development files; its README installs from the public repository. Skipped in the snapshot itself."""
    script = os.path.join(REPO, "tools", "community_export.py")
    if not os.path.exists(script):
        pytest.skip("this is the public snapshot")
    import importlib.util
    spec = importlib.util.spec_from_file_location("community_export", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.export(str(tmp_path / "snap"))
    names = {os.path.relpath(os.path.join(r, n), out) for r, ds, fs in os.walk(out) for n in ds + fs}
    for private in (".claude", "CLAUDE.md", "marketing", "legal", "docs", "tools"):
        assert private not in names, private
    for public in ("LICENSE", "CLA.md", "CONTRIBUTING.md", "app/install.sh", ".github/workflows/ci.yml"):
        assert public in names, public
    for readme in ("README.md", "README.ru.md"):
        with open(os.path.join(out, readme), encoding="utf-8") as f:
            text = f.read()
        assert "zorcam-nvr-community.git ~/zorcam-nvr" in text and "git@github.com:" not in text
        assert "editions.md" not in text and "ZORCAM_EDITION" not in text
    mod.export(str(tmp_path / "rel"), release=True)  # the licence and the CLA are approved (1.0)
    draft = tmp_path / "draft"
    shutil.copytree(mod.OVERLAY, draft)
    (draft / "CLA.md").write_text("Version 2.0 — DRAFT\n", encoding="utf-8")
    monkeypatch.setattr(mod, "OVERLAY", str(draft))
    with pytest.raises(SystemExit):  # a release needs the approved licence and CLA, not a draft
        mod.export(str(tmp_path / "rel2"), release=True)


def test_install_checks_the_downloads(tmp_path):
    """go2rtc and frpc run as root: install.sh knows the SHA-256 of the files of its default versions for every
    architecture it downloads for, and a file with another checksum is not installed."""
    import re
    import subprocess
    with open(os.path.join(REPO, "app", "install.sh"), encoding="utf-8") as f:
        src = f.read()
    go2rtc = re.search(r'GO2RTC_VERSION="\$\{GO2RTC_VERSION:-([0-9.]+)\}"', src).group(1)
    frp = re.search(r'FRP_VERSION="\$\{FRP_VERSION:-([0-9.]+)\}"', src).group(1)
    funcs = src[src.index("known_sha256() {"):src.index("# ----------------------------------------------------------------"
                                                           " go2rtc")]
    names = [f"go2rtc-{go2rtc}-{a}" for a in set(re.findall(r"asset=(go2rtc_linux_\w+)", src))]
    names += [f"frp-{frp}-{a}" for a in set(re.findall(r"frp_arch=(\w+) ;;", src))]
    assert len(names) == 7
    good = tmp_path / "file"
    good.write_bytes(b"x")
    script = "warn() { echo \"WARN $*\"; }\n" + funcs + "".join(
        f'[ -n "$(known_sha256 {n})" ] || echo "MISSING {n}"\n' for n in names) + (
        f'check_sha256 {good} {names[0]} && echo "ACCEPTED"\n'
        f'check_sha256 {good} go2rtc-0.0.1-go2rtc_linux_arm64 && echo "UNKNOWN OK"\n')
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
    assert "MISSING" not in out and "ACCEPTED" not in out, out
    assert "WARN no checksum of go2rtc-0.0.1" in out and "UNKNOWN OK" in out


def test_interface_texts_do_not_name_the_board():
    """The interface says device or NVR, never Pi or Raspberry (README and community are not the interface)."""
    import glob
    import zorcam_i18n
    word = re.compile(r"\b(pi|pis|raspberry)\b", re.I)
    texts = [(k, v) for k, d in zorcam_i18n.STRINGS.items() for v in d.values() if isinstance(v, str)]
    texts += [(k, v) for k, v in getattr(zorcam_i18n, "PARAM_RU", {}).items() if isinstance(v, str)]
    for path in glob.glob(os.path.join(REPO, "app", "gallery", "templates", "*")) + \
            glob.glob(os.path.join(REPO, "app", "gallery", "static", "*.js")):
        with open(path, encoding="utf-8") as f:
            code = re.sub(r"//.*|/\*.*?\*/", "", f.read(), flags=re.S)  # JS comments are not the interface
        texts.append((path, code))
    bad = [k for k, v in texts if word.search(v)]
    assert not bad, bad
