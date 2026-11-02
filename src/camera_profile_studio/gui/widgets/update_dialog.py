"""Update notice (dismissible bar) and the update dialog with release notes and download progress."""

from __future__ import annotations

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ..i18n import T, tr
from .common import SPACE_L, SPACE_M, SPACE_S, DisabledReason, HeadingLabel, WrapLabel, make_button, make_tool_button


class UpdateBar(QFrame):
    """Small dismissible notice above the workspaces for an automatic check that found an update."""

    def __init__(self, controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("update_bar")
        self.controller = controller
        self.release = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.AlternateBase)   # follows Light/Dark without style sheets
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_L, SPACE_S, SPACE_S, SPACE_S)
        row.setSpacing(SPACE_S)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.details = make_button("update.action.details", parent=self)
        self.dismiss = make_tool_button("update.action.dismiss", "dismiss_circle", self)
        row.addWidget(self.label, 1)
        row.addWidget(self.details)
        row.addWidget(self.dismiss)
        T.bind(self, "setAccessibleName", "update.bar.accessible")
        self.details.clicked.connect(lambda: self.release and self.controller.show_update(self.release))
        self.dismiss.clicked.connect(lambda: self.setVisible(False))
        T.watch(self, self._render)
        self.setVisible(False)

    def show_release(self, release) -> None:
        self.release = release
        self._render()
        self.setVisible(True)

    def _render(self) -> None:
        if self.release is not None:
            self.label.setText(tr("update.bar.text", version=self.release.version))
            self.setAccessibleDescription(self.label.text())


class UpdateDialog(QDialog):
    """Version, date, release notes and the actions: install (installed copy) or open the download page, skip, later."""

    def __init__(self, controller, release, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("update_dialog")
        self.controller, self.release = controller, release
        self._alive, self._job, self._installer = True, 0, None
        self._progress_state: tuple[str, dict] | None = None
        self.can_install, reason_key = controller.install_availability(release)
        T.bind(self, "setWindowTitle", "update.title")
        self.setMinimumWidth(460)
        self.resize(580, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE_L, SPACE_L, SPACE_L, SPACE_L)
        root.setSpacing(SPACE_M)

        self.heading = HeadingLabel("", 2, self)
        T.bind(self.heading, "setText", "update.heading", version=release.version)
        self.summary = WrapLabel(secondary=True, parent=self)
        root.addWidget(self.heading)
        root.addWidget(self.summary)
        if release.prerelease:
            root.addWidget(WrapLabel("update.prerelease_note", secondary=True, parent=self))

        notes_label = HeadingLabel("update.notes", 3, self)
        self.notes = QPlainTextEdit(self)
        self.notes.setReadOnly(True)               # release notes are shown as plain text, never as markup
        self.notes.setMinimumHeight(140)
        self.notes.setPlainText(release.notes)
        T.bind(self.notes, "setAccessibleName", "update.notes")
        root.addWidget(notes_label)
        root.addWidget(self.notes, 1)

        self.reason = DisabledReason(self)         # why this copy opens the download page instead of installing
        self.reason.set_reason(reason_key or None)
        root.addWidget(self.reason)

        self.progress_box = QWidget(self)
        pl = QVBoxLayout(self.progress_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(SPACE_S)
        self.progress_label = WrapLabel(parent=self.progress_box)
        self.progress = QProgressBar(self.progress_box)
        self.progress.setRange(0, 1000)
        T.bind(self.progress, "setAccessibleName", "update.progress_accessible")
        prow = QHBoxLayout()
        prow.setSpacing(SPACE_S)
        prow.addWidget(self.progress, 1)
        self.cancel_download = make_button("update.action.cancel_download", "dismiss_circle", parent=self.progress_box)
        prow.addWidget(self.cancel_download)
        pl.addWidget(self.progress_label)
        pl.addLayout(prow)
        self.progress_box.setVisible(False)
        root.addWidget(self.progress_box)

        self.buttons = QDialogButtonBox(self)
        self.primary = self.buttons.addButton("", QDialogButtonBox.ButtonRole.AcceptRole)
        self.skip_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.later_button = self.buttons.addButton("", QDialogButtonBox.ButtonRole.RejectRole)
        for button, key in ((self.skip_button, "update.action.skip"), (self.later_button, "update.action.later")):
            T.bind(button, "setText", key)
            T.bind(button, "setAccessibleName", key)
        self.primary.setDefault(True)
        root.addWidget(self.buttons)

        self.primary.clicked.connect(self._primary)
        self.skip_button.clicked.connect(self._skip)
        self.later_button.clicked.connect(self.reject)
        self.cancel_download.clicked.connect(self._cancel_download)
        T.watch(self, self._render)
        self._render()

    # ------------------------------------------------------------ rendering
    def _render(self) -> None:
        r = self.release
        self.summary.setText(tr("update.summary_dated", current=__version__, date=r.published_date) if r.published_date
                             else tr("update.summary", current=__version__))
        if not r.notes.strip():
            self.notes.setPlainText(tr("update.no_notes"))
        key = "update.action.install_now" if self._installer else "update.action.install" if self.can_install else "update.action.open_page"
        self.primary.setText(tr(key))
        self.primary.setAccessibleName(self.primary.text().replace("&", ""))
        if self._progress_state is not None:
            self.progress_label.setText(tr(self._progress_state[0], **self._progress_state[1]))

    def _show_progress(self, key: str, kwargs: dict, fraction: float | None) -> None:
        self._progress_state = (key, kwargs)
        self.progress_label.setText(tr(key, **kwargs))
        if fraction is None:
            self.progress.setRange(0, 0)           # busy indicator
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))

    def _set_busy(self, busy: bool) -> None:
        self.progress_box.setVisible(busy or self._installer is not None)
        self.cancel_download.setVisible(busy)
        for button in (self.primary, self.skip_button):
            button.setEnabled(not busy)
            button.setToolTip(tr("update.reason.downloading") if busy else "")

    # ------------------------------------------------------------ actions
    def _primary(self) -> None:
        if self._installer is not None:
            self._install()
        elif self.can_install:
            self._download()
        else:
            self.controller.open_page(self.release)
            self.accept()

    def _skip(self) -> None:
        self.controller.skip(self.release)
        self.accept()

    def _download(self) -> None:
        self._set_busy(True)
        self._show_progress("update.preparing", {}, None)
        self._job = self.controller.start_download(self.release, self._on_progress, self._on_finished, self._on_error,
                                                   self._on_cancelled)
        if self._job < 0:
            self._job = 0
            self._set_busy(False)

    def _cancel_download(self) -> None:
        if self._job:
            self.controller.ctx.jobs.cancel(self._job)
            self.cancel_download.setEnabled(False)

    def _on_progress(self, fraction, key: str, kwargs: dict) -> None:
        if self._alive and key:
            self._show_progress(key, kwargs, fraction)

    def _on_finished(self, result) -> None:
        self._job = 0
        if not self._alive:
            return
        self._set_busy(False)
        if result.error:
            self.progress_box.setVisible(False)
            self.controller.show_download_error(result, self)
            return
        self._installer = result.path
        self.controller.note_downloaded(result)
        self._show_progress("update.verified", {}, 1.0)
        self.progress_box.setVisible(True)
        self._render()
        self._install()

    def _on_error(self, err) -> None:
        self._job = 0
        if self._alive:
            self._set_busy(False)
            self.progress_box.setVisible(False)
            self.controller.ctx.show_error(err, "update.download_failed_title")

    def _on_cancelled(self) -> None:
        self._job = 0
        if self._alive:
            self.cancel_download.setEnabled(True)
            self._set_busy(False)
            self.progress_box.setVisible(False)

    def _install(self) -> None:
        if self._installer is not None and self.controller.confirm_install(self.release, self):
            if self.controller.install(self._installer, self.release):
                self.accept()

    def done(self, result: int) -> None:  # noqa: D401 - Qt override: closing the dialog also stops a running download
        self._alive = False
        if self._job:
            self.controller.ctx.jobs.cancel(self._job)
        super().done(result)
        self.deleteLater()
