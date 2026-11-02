"""In-app update support: versions, release selection, error mapping, download and SHA-256 checks.

No test touches the network: every request goes through an injected opener.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.error

import pytest

from camera_profile_studio import __version__, updates
from camera_profile_studio.updates import Version

API = updates.RELEASES_API


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._buf, self.status = io.BytesIO(body), status
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release(tag, *, draft=False, pre=False, body="Notes\r\nline", assets=True):
    base = f"https://github.com/Koczalski/CameraProfileStudio/releases/download/{tag}/CameraProfileStudio-{tag.lstrip('v')}"
    return {
        "tag_name": tag, "name": f"Release {tag}", "draft": draft, "prerelease": pre, "body": body,
        "html_url": f"https://github.com/Koczalski/CameraProfileStudio/releases/tag/{tag}", "published_at": "2026-11-02T08:30:00Z",
        "assets": [{"name": f"CameraProfileStudio-{tag.lstrip('v')}-Setup.exe", "browser_download_url": base + "-Setup.exe", "size": 123},
                   {"name": "DELIVERY_SHA256SUMS.txt", "browser_download_url": base + "-sums", "size": 9}] if assets else [],
    }


CANNED = [
    release("v0.3.0", draft=True),                     # draft: never offered
    release("v0.2.9-beta", pre=True),                  # pre-release: only when allowed
    release("v0.2.8"),                                 # newest stable
    release("v0.2.6"),                                 # the current version is not "newer"
    release("v0.2.5-beta", pre=True),                  # older
    {"tag_name": "nightly", "draft": False, "prerelease": False, "assets": []},   # not a version
]


def serve(data, calls=None):
    def opener(request, timeout=None):
        if calls is not None:
            calls.append((request, timeout))
        return FakeResponse(data if isinstance(data, bytes) else json.dumps(data).encode())
    return opener


def failing(exc):
    def opener(request, timeout=None):
        raise exc
    return opener


# ---------------------------------------------------------------- versions
def test_semver_precedence_including_prereleases():
    order = ["0.2.5", "0.2.6-alpha", "0.2.6-beta", "0.2.6-beta.2", "0.2.6-beta.10", "0.2.6-rc.1", "0.2.6", "0.2.7", "0.10.0", "1.0.0"]
    parsed = [Version.parse(t) for t in order]
    assert all(a < b for a, b in zip(parsed, parsed[1:]))
    assert Version.parse("v0.2.6-beta") > Version.parse("0.2.5") and Version.parse("0.2.6") > Version.parse("v0.2.6-beta")
    assert Version.parse("V1.2") == Version.parse("1.2.0+build.5")
    assert updates.compare_versions("0.2.6", "0.2.6-beta") == 1 and updates.compare_versions("0.2.5", "0.2.5") == 0
    assert str(Version.parse("v0.2.6-beta.2")) == "0.2.6-beta.2"
    assert Version.try_parse("nightly") is None and Version.try_parse(None) is None
    assert updates.default_include_prereleases("0.2.6") and not updates.default_include_prereleases("1.0.0")


def test_auto_check_runs_at_most_once_a_day():
    assert updates.auto_check_due(None) and updates.auto_check_due(0)
    assert not updates.auto_check_due(1000, now=1000 + 3600)
    assert updates.auto_check_due(1000, now=1000 + 24 * 3600 + 1)
    assert updates.auto_check_due(5000, now=1000)          # clock moved backwards: check again


# ---------------------------------------------------------------- release selection
def test_check_selects_newest_newer_release_and_sends_only_a_plain_get():
    calls: list = []
    result = updates.check_for_update("0.2.6", opener=serve(CANNED, calls))
    assert result.ok and result.update_available
    r = result.release
    assert (r.version, r.tag, r.title, r.prerelease) == ("0.2.8", "v0.2.8", "Release v0.2.8", False)
    assert r.notes == "Notes\nline" and r.published_date == "2026-11-02" and r.html_url.endswith("/tag/v0.2.8")
    assert r.setup_asset.name == "CameraProfileStudio-0.2.8-Setup.exe" and r.setup_asset.size == 123
    assert r.checksum_asset.name == "DELIVERY_SHA256SUMS.txt"
    (request, timeout), = calls
    assert request.full_url == API and request.get_method() == "GET" and request.data is None and timeout == 10
    assert request.get_header("User-agent") == "CameraProfileStudio/0.2.6"
    assert set(k.lower() for k in request.headers) == {"user-agent", "accept"}

    beta = updates.check_for_update("0.2.6", include_prereleases=True, opener=serve(CANNED)).release
    assert beta.version == "0.2.9-beta" and beta.prerelease
    assert updates.check_for_update("0.2.8", include_prereleases=False, opener=serve(CANNED)).release is None
    up_to_date = updates.check_for_update("0.2.9", include_prereleases=True, opener=serve(CANNED))
    assert up_to_date.ok and not up_to_date.update_available


def test_check_errors_become_results_never_exceptions():
    def codes(opener):
        r = updates.check_for_update("0.2.6", opener=opener)
        assert not r.ok and r.release is None
        return r.error

    http = lambda code: urllib.error.HTTPError(API, code, "x", {}, None)  # noqa: E731
    assert codes(failing(urllib.error.URLError("no route to host"))) == updates.OFFLINE
    assert codes(failing(TimeoutError("timed out"))) == updates.OFFLINE
    assert codes(failing(http(404))) == updates.NOT_FOUND
    assert codes(failing(http(403))) == updates.RATE_LIMITED and codes(failing(http(429))) == updates.RATE_LIMITED
    assert codes(failing(http(500))) == updates.HTTP_ERROR
    assert codes(serve(b"<html>not json")) == updates.BAD_RESPONSE
    assert codes(serve({"message": "API rate limit exceeded"})) == updates.BAD_RESPONSE
    assert codes(failing(RuntimeError("boom"))) == updates.UNEXPECTED
    assert updates.check_for_update("not a version", opener=serve([])).error == updates.UNEXPECTED


# ---------------------------------------------------------------- SHA-256
def test_sha256_verification_refuses_mismatch_and_missing_entry(tmp_path):
    f = tmp_path / "CameraProfileStudio-0.2.8-Setup.exe"
    f.write_bytes(b"installer bytes")
    digest = hashlib.sha256(b"installer bytes").hexdigest()
    sums = f"\ufeff{digest.upper()}  *output/releases/CameraProfileStudio-0.2.8-Setup.EXE\r\n{'0' * 64}  other.zip\r\n"
    assert updates.verify_sha256(f, sums) == digest                         # BOM, CRLF, case, folder prefix tolerated
    with pytest.raises(updates.ChecksumError) as bad:
        updates.verify_sha256(f, f"{'1' * 64}  {f.name}\n")
    assert bad.value.code == updates.CHECKSUM_MISMATCH
    with pytest.raises(updates.ChecksumError) as missing:
        updates.verify_sha256(f, f"{digest}  some-other-file.exe\n")
    assert missing.value.code == updates.CHECKSUM_MISSING


# ---------------------------------------------------------------- download
def test_download_is_atomic_and_resumes_after_cancel(tmp_path):
    payload = bytes(range(10))
    url = "https://github.com/Koczalski/CameraProfileStudio/releases/download/v0.2.8/a-Setup.exe"
    dest = tmp_path / "dl" / "a-Setup.exe"
    seen = []
    ranges = []

    def opener(request, timeout=None):
        start = int((request.get_header("Range") or "bytes=0-").split("=")[1].rstrip("-"))
        ranges.append(start)
        return FakeResponse(payload[start:], 206 if start else 200)

    with pytest.raises(updates.UpdateError) as cancelled:
        updates.download_asset(url, dest, len(payload), lambda d, t: seen.append((d, t)), lambda: len(seen) >= 2,
                               opener=opener, chunk_size=2)
    assert cancelled.value.code == updates.CANCELLED and isinstance(cancelled.value, updates.DownloadCancelled)
    assert not dest.exists() and dest.with_name(dest.name + ".part").read_bytes() == payload[:4]    # kept for resuming

    assert updates.download_asset(url, dest, len(payload), lambda d, t: seen.append((d, t)), opener=opener, chunk_size=2) == dest
    assert ranges == [0, 4] and dest.read_bytes() == payload and not dest.with_name(dest.name + ".part").exists()
    assert seen[-1] == (10, 10)


def test_download_rejects_wrong_size_and_untrusted_addresses(tmp_path):
    url = "https://github.com/Koczalski/CameraProfileStudio/releases/download/v0.2.8/a-Setup.exe"
    with pytest.raises(updates.UpdateError) as short:
        updates.download_asset(url, tmp_path / "a.exe", 20, opener=serve(b"0123456789"))
    assert short.value.code == updates.SIZE_MISMATCH and not (tmp_path / "a.exe").exists()
    for bad in ("http://github.com/x.exe", "https://github.com.evil.example/x.exe", "https://example.com/x.exe", "file:///c:/x.exe"):
        with pytest.raises(updates.UpdateError) as refused:
            updates.download_asset(bad, tmp_path / "b.exe", opener=serve(b""))
        assert refused.value.code == updates.INSECURE_URL
    assert updates.is_trusted_url("https://objects.githubusercontent.com/a") and updates.is_trusted_url(updates.RELEASES_PAGE)


# ---------------------------------------------------------------- install mode and launch
def test_install_mode_and_installer_command(tmp_path):
    here = str(tmp_path / "Programs" / "CameraProfileStudio")
    exe = os.path.join(here, "CameraProfileStudio.exe")
    assert updates.detect_install_mode(exe, frozen=False, location=lambda: here) == updates.SOURCE
    assert updates.detect_install_mode(exe, frozen=True, location=lambda: here + os.sep) == updates.INSTALLED
    assert updates.detect_install_mode(exe, frozen=True, location=lambda: str(tmp_path / "elsewhere")) == updates.PORTABLE
    assert updates.detect_install_mode(exe, frozen=True, location=lambda: None) == updates.PORTABLE
    assert updates.UNINSTALL_KEY.endswith("{B15FF861-C98A-4F3F-B46D-143C62935FA3}_is1")

    started = []
    updates.launch_installer(tmp_path / "Setup.exe", popen=lambda args, **kw: started.append((args, kw)))
    args, kw = started[0]
    assert args[0].endswith("Setup.exe") and {"/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"} <= set(args)
    assert kw["cwd"] == str(tmp_path)
    assert updates.acquire_app_mutex() in (True, False)     # harmless off Windows
    updates.release_app_mutex()


# ---------------------------------------------------------------- GUI
@pytest.fixture()
def win(qtbot, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings

    from camera_profile_studio.gui.app import create_application
    from camera_profile_studio.gui.i18n import T
    from camera_profile_studio.gui.main_window import MainWindow

    create_application([])
    T.set_language("en")
    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(w)
    w.updates.mode = updates.PORTABLE
    yield w
    T.set_language("en")
    w._force_close = True
    w.close()


@pytest.mark.gui
def test_manual_check_shows_the_result_without_network(qtbot, win):
    ctl = win.updates
    stub_calls = []

    def stub(current, include_prereleases=False):
        stub_calls.append((current, include_prereleases))
        return updates.UpdateCheck(current, updates.check_for_update(current, include_prereleases=include_prereleases,
                                                                      opener=serve(CANNED)).release)

    ctl.checker = stub
    assert win.act_check_updates in win.help_menu.actions() and win.act_check_updates.text().replace("&", "") == "Check for updates…"
    win.act_check_updates.trigger()
    qtbot.waitUntil(lambda: ctl.dialog is not None, timeout=5000)
    dialog = ctl.dialog
    assert stub_calls == [(__version__, True)]                        # pre-releases default ON while the major version is 0
    assert dialog.release.version == "0.2.9-beta" and "0.2.9-beta" in dialog.heading.text()
    assert "2026-11-02" in dialog.summary.text() and f"version {__version__}" in dialog.summary.text()
    assert dialog.primary.text() == "Open download page" and not dialog.can_install      # portable copy: no self-update
    assert "portable" in dialog.reason.text() and dialog.notes.toPlainText() == "Notes\nline"
    assert str(ctl.ctx.settings.value("updates/last_status")) == "update" and "0.2.9-beta" in ctl.last_result_text()
    dialog.skip_button.click()
    assert ctl.skipped_version == "0.2.9-beta" and "skipped" in ctl.last_result_text()

    # "Check now" on the Settings page: disabled while a check runs, and a current version says so
    ctl.checker = lambda current, include_prereleases=False: updates.UpdateCheck(current)
    settings = win.workspaces["settings"]
    settings.update_check.click()
    assert not settings.update_check.isEnabled() and settings.update_check.toolTip() == "A check is already running."
    qtbot.waitUntil(lambda: ctl.message is not None, timeout=5000)
    assert "latest version" in ctl.message.text() and settings.update_check.isEnabled()
    ctl.message.close()


@pytest.mark.gui
def test_automatic_check_is_gated_and_shows_a_dismissible_bar(qtbot, win):
    ctl = win.updates
    ctl.checker = lambda current, include_prereleases=False: updates.UpdateCheck(
        current, updates.check_for_update(current, include_prereleases=False, opener=serve(CANNED)).release)
    ctl.auto_check = False
    assert not ctl.maybe_auto_check()                                   # setting off: nothing happens
    ctl.auto_check = True
    assert ctl.maybe_auto_check()                                       # never checked: due
    qtbot.waitUntil(lambda: not win.update_bar.isHidden(), timeout=5000)
    assert "0.2.8" in win.update_bar.label.text() and ctl.dialog is None     # a notice only, no dialog
    assert not ctl.maybe_auto_check()                                   # checked just now: not due for 24 h
    win.update_bar.dismiss.click()
    assert win.update_bar.isHidden()
    assert ctl.include_prereleases is True and str(ctl.ctx.settings.value("updates/last_status")) == "update"


def _installer_fixture(tmp_path, monkeypatch, sums_hash=None):
    payload = b"setup bytes " * 500
    item = release("v0.2.8")
    item["assets"][0]["size"] = len(payload)
    setup = item["assets"][0]["name"]
    sums = f"{sums_hash or hashlib.sha256(payload).hexdigest()}  {setup}\n".encode()
    monkeypatch.setattr(updates, "download_dir", lambda: tmp_path / "dl")

    def opener(request, timeout=None):
        return FakeResponse(payload if request.full_url.endswith("-Setup.exe") else sums)

    return updates.select_release([item], "0.2.6", False), opener, tmp_path / "dl" / setup, payload


@pytest.mark.gui
def test_installed_copy_downloads_verifies_then_starts_setup_and_closes(qtbot, win, tmp_path, monkeypatch):
    rel, opener, installer, payload = _installer_fixture(tmp_path, monkeypatch)
    ctl, started, closed = win.updates, [], []
    ctl.mode, ctl.opener, ctl.launcher = updates.INSTALLED, opener, started.append
    ctl.confirm_install = lambda release, parent=None: True
    win.close = lambda: closed.append(True)
    ctl.show_update(rel)
    dialog = ctl.dialog
    assert dialog.can_install and dialog.primary.text() == "Download and install" and not dialog.reason.reason_text()
    dialog.primary.click()
    assert not dialog.primary.isEnabled() and dialog.primary.toolTip()          # disabled while downloading, with the reason
    qtbot.waitUntil(lambda: bool(closed), timeout=5000)
    assert started == [installer] and installer.read_bytes() == payload and win._force_close
    assert any("SHA-256" in e.render() for e in ctl.ctx.log_model.entries)


@pytest.mark.gui
def test_installer_failing_its_checksum_is_deleted_and_never_started(qtbot, win, tmp_path, monkeypatch):
    rel, opener, installer, _ = _installer_fixture(tmp_path, monkeypatch, sums_hash="0" * 64)
    ctl, started = win.updates, []
    ctl.mode, ctl.opener, ctl.launcher = updates.INSTALLED, opener, started.append
    ctl.show_update(rel)
    ctl.dialog.primary.click()
    qtbot.waitUntil(lambda: ctl.message is not None, timeout=5000)
    assert "does not match its checksum" in ctl.message.text() and not installer.exists() and started == []
    assert ctl.dialog.primary.isEnabled()                                        # the user can try again or open the page
    ctl.message.close()
    ctl.dialog.reject()
