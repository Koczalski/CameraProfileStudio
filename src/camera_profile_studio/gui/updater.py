"""Update controller: settings, scheduling, the check job, notices and installation.

The network work lives in :mod:`camera_profile_studio.updates` (Qt-free).  This
class only connects it to the GUI: it runs the check and the download as jobs,
remembers the last result in the settings and decides which notice to show.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from .. import __version__, updates
from .context import setting_bool, setting_int
from .i18n import tr
from .strings import STRINGS

KEY_AUTO = "updates/auto_check"
KEY_PRERELEASE = "updates/include_prereleases"
KEY_LAST = "updates/last_check"            # epoch seconds
KEY_STATUS = "updates/last_status"         # current | update | error:<code>
KEY_VERSION = "updates/last_version"       # version offered by the last check
KEY_SKIPPED = "updates/skipped_version"
DISABLE_ENV = "CPS_DISABLE_UPDATE_CHECK"   # set to any value to turn the automatic check off (CI, offline tests)


def run_cancellable(token, fn: Callable[[], Any], poll: float = 0.1) -> Any:
    """Run a blocking call on a helper thread so a job can end at once when cancelled.

    A socket read cannot be interrupted; the abandoned daemon thread finishes by itself
    (the download checks its own cancel flag per chunk), so closing the window never waits for the network.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the job thread
            box["error"] = exc

    thread = threading.Thread(target=target, name="cps-update", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(poll)
        token.raise_if_cancelled()
    if "error" in box:
        raise box["error"]
    return box["value"]


@dataclass
class DownloadResult:
    path: Path | None = None
    sha256: str = ""
    error: str = ""
    detail: str = ""


def _mb(n: int) -> str:
    return f"{n / (1 << 20):.1f} MB"


class UpdateController(QObject):
    """Owned by the main window (``ctx.updates``); the Settings page and the Help menu talk to it."""

    state_changed = Signal()

    def __init__(self, ctx, parent: QObject | None = None, *, checker: Callable[..., Any] | None = None,
                 mode: str | None = None, launcher: Callable[[Any], None] | None = None,
                 opener: Callable[..., Any] | None = None) -> None:
        super().__init__(parent or ctx)
        self.ctx = ctx
        self.checker = checker or updates.check_for_update
        self.mode = mode or updates.detect_install_mode()
        self.launcher = launcher or updates.launch_installer
        self.opener = opener                      # injected by tests; None = real network
        self.checking = False
        self.bar = None                           # UpdateBar, set by the main window
        self.dialog = None                        # the open UpdateDialog, if any
        self.message: QMessageBox | None = None   # the last result box
        self._manual = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.maybe_auto_check)

    # ------------------------------------------------------------ settings
    @property
    def auto_check(self) -> bool:
        return setting_bool(self.ctx.settings, KEY_AUTO, True)

    @auto_check.setter
    def auto_check(self, on: bool) -> None:
        self.ctx.settings.setValue(KEY_AUTO, bool(on))

    @property
    def include_prereleases(self) -> bool:
        return setting_bool(self.ctx.settings, KEY_PRERELEASE, updates.default_include_prereleases())

    @include_prereleases.setter
    def include_prereleases(self, on: bool) -> None:
        self.ctx.settings.setValue(KEY_PRERELEASE, bool(on))

    @property
    def skipped_version(self) -> str:
        return str(self.ctx.settings.value(KEY_SKIPPED, "") or "")

    # ------------------------------------------------------------ checking
    def schedule_auto_check(self, delay_ms: int = 3000) -> None:
        """Called once after the window is shown; the check itself waits a moment so start-up stays fast."""
        if not os.environ.get(DISABLE_ENV):
            self._timer.start(delay_ms)

    def auto_check_due(self) -> bool:
        s = self.ctx.settings
        return self.auto_check and (updates.auto_check_due(setting_int(s, KEY_LAST, 0))
                                    or str(s.value(KEY_STATUS, "")) == f"error:{updates.OFFLINE}")

    def maybe_auto_check(self) -> bool:
        if self.checking or not self.auto_check_due():
            return False
        self.check(manual=False)
        return True

    def check(self, manual: bool = False) -> None:
        """Start a check; ``manual`` results are always reported in a dialog, automatic ones quietly."""
        if self.checking:
            self._manual = self._manual or manual
            return
        include, checker = self.include_prereleases, self.checker
        self.checking, self._manual = True, manual
        self.ctx.log.info("log.update.checking")

        def work(token, progress):
            updates.cleanup_downloads()
            return run_cancellable(token, lambda: checker(__version__, include_prereleases=include))

        job = self.ctx.jobs.submit("update:check", "job.update_check", work, on_result=self._checked,
                                   on_error=lambda _e: self._check_stopped(), on_cancel=self._check_stopped, background=True)
        if job < 0:
            self.checking = False
        self.state_changed.emit()

    def _check_stopped(self) -> None:
        self.checking, self._manual = False, False
        self.state_changed.emit()

    def _checked(self, result: updates.UpdateCheck) -> None:
        self.checking = False
        manual, self._manual = self._manual, False
        s, log = self.ctx.settings, self.ctx.log
        s.setValue(KEY_LAST, int(time.time()))
        if result.error:
            s.setValue(KEY_STATUS, f"error:{result.error}")
            s.setValue(KEY_VERSION, "")
            log.warning(f"update.error.{result.error}", detail=result.detail)
            if manual:
                self._message(QMessageBox.Icon.Warning, "update.check_failed_title", f"update.error.{result.error}", result.detail)
        elif result.release is not None:
            release = result.release
            s.setValue(KEY_STATUS, "update")
            s.setValue(KEY_VERSION, release.version)
            if not manual and release.version == self.skipped_version:
                log.info("log.update.skipped", version=release.version)
            else:
                log.add("success", "log.update.available", action_key="update.action.details",
                        action=lambda r=release: self.show_update(r), version=release.version, current=result.current)
                if manual:
                    self.show_update(release)
                elif self.bar is not None:
                    self.bar.show_release(release)
        else:
            s.setValue(KEY_STATUS, "current")
            s.setValue(KEY_VERSION, "")
            log.info("log.update.current", current=result.current)
            if manual:
                self._message(QMessageBox.Icon.Information, "update.up_to_date_title", "update.up_to_date", "",
                              current=result.current)
        self.state_changed.emit()

    def last_result_text(self) -> str:
        """The last check as one sentence for the Settings page (kept across restarts)."""
        if self.checking:
            return tr("settings.update.checking")
        s = self.ctx.settings
        last, status = setting_int(s, KEY_LAST, 0), str(s.value(KEY_STATUS, ""))
        try:
            when = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else ""
        except (OverflowError, OSError, ValueError):
            when = ""
        if not when:
            return tr("settings.update.never")
        if status == "current":
            result = tr("settings.update.result.current")
        elif status == "update":
            version = str(s.value(KEY_VERSION, ""))
            result = tr("settings.update.result.skipped" if version and version == self.skipped_version
                        else "settings.update.result.update", version=version)
        elif status.startswith("error:"):
            key = f"update.error.{status[6:]}"
            result = tr(key if key in STRINGS["en"] else "update.error.unexpected")
        else:
            return tr("settings.update.never")
        return tr("settings.update.last", when=when, result=result)

    def mode_text(self) -> str:
        return tr("settings.update.mode", mode=tr(f"update.mode_name.{self.mode}"))

    # ------------------------------------------------------------ notices
    def _message(self, icon, title_key: str, text_key: str, detail: str = "", parent=None, **kwargs) -> None:
        box = QMessageBox(icon, tr(title_key), tr(text_key, **kwargs), parent=parent or self.ctx.window)
        if detail:
            box.setInformativeText(detail)
        box.addButton(tr("action.ok"), QMessageBox.ButtonRole.AcceptRole)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.message = box
        box.open()

    def show_download_error(self, result: "DownloadResult", parent=None) -> None:
        key = f"update.error.{result.error}"
        key = key if key in STRINGS["en"] else "update.error.unexpected"
        self.ctx.log.error(key, detail=result.detail)
        self._message(QMessageBox.Icon.Warning, "update.download_failed_title", key, result.detail, parent)

    def show_update(self, release: updates.ReleaseInfo) -> None:
        """Open the update dialog (non-blocking: the window stays usable behind the modal sheet only for the dialog's own parent)."""
        if self.dialog is not None:
            self.dialog.raise_()
            self.dialog.activateWindow()
            return
        from .widgets.update_dialog import UpdateDialog

        dialog = UpdateDialog(self, release, self.ctx.window)
        self.dialog = dialog
        dialog.finished.connect(lambda _r: setattr(self, "dialog", None))
        dialog.open()

    def install_availability(self, release: updates.ReleaseInfo) -> tuple[bool, str]:
        """``(True, "")`` when this copy can install ``release`` itself, else ``(False, key of the reason)``."""
        if self.mode != updates.INSTALLED:
            return False, f"update.mode.{self.mode}"
        setup, sums = release.setup_asset, release.checksum_asset
        if setup is None:
            return False, "update.reason.no_installer"
        if sums is None:
            return False, "update.reason.no_checksums"
        if not (updates.is_trusted_url(setup.url) and updates.is_trusted_url(sums.url)):
            return False, "update.reason.untrusted"
        return True, ""

    def skip(self, release: updates.ReleaseInfo) -> None:
        self.ctx.settings.setValue(KEY_SKIPPED, release.version)
        self.ctx.log.info("log.update.skip_set", version=release.version)
        if self.bar is not None:
            self.bar.setVisible(False)
        self.state_changed.emit()

    def open_page(self, release: updates.ReleaseInfo) -> bool:
        url = release.html_url if updates.is_trusted_url(release.html_url) else updates.RELEASES_PAGE
        opened = QDesktopServices.openUrl(QUrl(url))
        if opened:
            self.ctx.log.info("log.update.opened_page", url=url)
        else:
            self.ctx.log.warning("log.update.open_failed", url=url)
        return bool(opened)

    # ------------------------------------------------------------ install
    def start_download(self, release: updates.ReleaseInfo, on_progress, on_finished, on_error, on_cancel) -> int:
        """Download the installer and check its SHA-256 as a job; ``on_finished`` gets a :class:`DownloadResult`."""
        setup, sums = release.setup_asset, release.checksum_asset
        assert setup is not None and sums is not None
        dest = updates.download_dir() / Path(setup.name).name
        opener = self.opener

        def work(token, progress):
            def run():
                progress(None, "update.preparing")
                sums_text = updates.fetch_text(sums.url, opener=opener)

                def report(done: int, total: int) -> None:
                    progress(done / total if total else None, "update.progress" if total else "update.progress_unknown",
                             done=_mb(done), total=_mb(total))

                path = updates.download_asset(setup.url, dest, setup.size, report, lambda: token.cancelled, opener=opener)
                progress(1.0, "update.verifying")
                try:
                    sha = updates.verify_sha256(path, sums_text, setup.name)
                except updates.ChecksumError:
                    path.unlink(missing_ok=True)   # never leave an installer that failed its check
                    raise
                return DownloadResult(path, sha)

            try:
                return run_cancellable(token, run)
            except updates.UpdateError as exc:
                if exc.code == updates.CANCELLED:
                    raise
                return DownloadResult(error=exc.code, detail=exc.detail)

        return self.ctx.jobs.submit("update:download", "job.update_download", work, on_result=on_finished, on_error=on_error,
                                    on_cancel=on_cancel, on_progress=on_progress, title_kwargs={"version": release.version})

    def note_downloaded(self, result: DownloadResult) -> None:
        self.ctx.log.success("log.update.downloaded", name=result.path.name if result.path else "", sha=result.sha256[:16])

    def confirm_install(self, release: updates.ReleaseInfo, parent=None) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, tr("update.confirm_title"),
                          tr("update.confirm_text", version=release.version), parent=parent or self.ctx.window)
        yes = box.addButton(tr("update.action.install_now"), QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(tr("update.action.not_now"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    def install(self, path: Path, release: updates.ReleaseInfo) -> bool:
        """Start Setup for the verified installer and close the application; False if it did not start."""
        busy = [r for r in self.ctx.jobs.active() if not r.background]
        if busy and not self.ctx.confirm("dialog.quit_title", "dialog.quit_running", n=len(busy)):
            return False
        self.ctx.log.info("log.update.launching", version=release.version)
        updates.release_app_mutex()          # Setup must not wait for this process to exit
        try:
            self.launcher(path)
        except OSError as exc:
            updates.acquire_app_mutex()
            self.ctx.log.error("update.error.launch", detail=str(exc))
            self._message(QMessageBox.Icon.Critical, "update.install_failed_title", "update.error.launch", str(exc))
            return False
        window = self.ctx.window
        if window is not None:
            window._force_close = True       # skip the "tasks are running" question: the user just confirmed
            window.close()
        return True
