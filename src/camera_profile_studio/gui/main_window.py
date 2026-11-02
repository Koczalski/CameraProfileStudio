"""Main window: left navigation (Model/View list), workspace stack, bottom
Jobs/Log panel, menu bar and status bar.

Navigation follows Microsoft "Navigation basics": a flat structure of eight
peer pages with a left navigation list, each page clearly labelled with a
title at the top.  New pages are appended so existing Ctrl+1..6 shortcuts and
list adjacency stay unchanged (Image Look Creator is Ctrl+7, LUT Check Ctrl+8).
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QModelIndex, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QListView,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import about, icons
from .app import settings as app_settings
from .backend import Engine
from .context import AppContext
from .i18n import T, strip_mnemonic, tr
from .updater import UpdateController
from .widgets.log_panel import BottomPanel
from .widgets.update_dialog import UpdateBar
from .workspaces.base import Workspace
from .workspaces.batch import BatchWorkspace
from .workspaces.compare import CompareWorkspace
from .workspaces.converter import ConverterWorkspace
from .workspaces.diagnose import DiagnoseWorkspace
from .workspaces.image_look import ImageLookWorkspace
from .workspaces.inspector import InspectorWorkspace
from .workspaces.look import LookWorkspace
from .workspaces.match import MatchWorkspace
from .workspaces.settings import SettingsWorkspace

WORKSPACES = (InspectorWorkspace, ConverterWorkspace, LookWorkspace, MatchWorkspace, BatchWorkspace, SettingsWorkspace,
              ImageLookWorkspace, DiagnoseWorkspace, CompareWorkspace)


class NavigationView(QListView):
    """Left navigation list backed by a QStandardItemModel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navigation")
        self.nav_model = QStandardItemModel(self)
        self.setModel(self.nav_model)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setIconSize(QSize(20, 20))
        self.setSpacing(2)
        self.setUniformItemSizes(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setMinimumWidth(44)
        T.bind(self, "setAccessibleName", "nav.accessible")

    def add(self, key: str, icon_name: str) -> None:
        item = QStandardItem(icons.icon(icon_name), tr(f"nav.{key}"))
        item.setData(key, Qt.ItemDataRole.UserRole)
        item.setSizeHint(QSize(0, 36))
        self.nav_model.appendRow(item)

    compact = False

    def set_compact(self, compact: bool) -> None:
        """Icon-only navigation for narrow windows (the NavigationView
        "LeftCompact" pattern); names remain in tooltips and accessible text."""
        if compact == self.compact:
            return
        self.compact = compact
        self.retranslate()
        self.updateGeometry()

    def retranslate(self) -> None:
        for r in range(self.nav_model.rowCount()):
            item = self.nav_model.item(r)
            key = item.data(Qt.ItemDataRole.UserRole)
            name = tr(f"nav.{key}")
            item.setText("" if self.compact else name)
            item.setData(name, Qt.ItemDataRole.AccessibleTextRole)
            item.setToolTip(name if self.compact else tr(f"{key}.subtitle"))

    def sizeHint(self) -> QSize:  # noqa: D401
        if self.compact:
            return QSize(52, 300)
        fm = self.fontMetrics()
        width = max(fm.horizontalAdvance(self.nav_model.item(r).text()) for r in range(self.nav_model.rowCount())) if self.nav_model.rowCount() else 120
        return QSize(width + 64, 300)


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine | None = None, settings=None) -> None:
        super().__init__()
        self.setObjectName("main_window")
        self.ctx = AppContext(engine or Engine(), settings or app_settings(), self)
        self.ctx.window = self
        if self.ctx.engine.available:
            # Before any worker job exists: engine imports must not happen on
            # pool threads (see Engine.preload).
            self.ctx.engine.preload()
        self._install_exception_hook()
        self.updates = self.ctx.updates = UpdateController(self.ctx)   # before the workspaces: Settings talks to it
        T.bind(self, "setWindowTitle", "app.title")
        self.setMinimumSize(720, 480)
        # navigation
        self.nav = NavigationView()
        self.stack = QStackedWidget()
        self.workspaces: dict[str, Workspace] = {}
        for cls in WORKSPACES:
            ws = cls(self.ctx)
            self.workspaces[cls.key] = ws
            self.stack.addWidget(ws)
            self.nav.add(cls.key, cls.icon_name)
        self.bottom = BottomPanel(self.ctx.jobs, self.ctx.log_model)
        self.bottom.setMinimumHeight(90)
        # layout: [nav | [stack / bottom]]
        self.vsplit = QSplitter(Qt.Orientation.Vertical)
        self.vsplit.setObjectName("main_vsplit")
        self.vsplit.addWidget(self.stack)
        self.vsplit.addWidget(self.bottom)
        self.vsplit.setStretchFactor(0, 4)
        self.vsplit.setStretchFactor(1, 1)
        self.vsplit.setChildrenCollapsible(False)
        self.hsplit = QSplitter(Qt.Orientation.Horizontal)
        self.hsplit.setObjectName("main_hsplit")
        self.hsplit.addWidget(self.nav)
        self.hsplit.addWidget(self.vsplit)
        self.hsplit.setStretchFactor(0, 0)
        self.hsplit.setStretchFactor(1, 1)
        self.hsplit.setChildrenCollapsible(False)
        # update notice (hidden until an automatic check finds a newer version) above the workspaces
        self.update_bar = UpdateBar(self.ctx.updates)
        self.ctx.updates.bar = self.update_bar
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.update_bar)
        central_layout.addWidget(self.hsplit, 1)
        self.setCentralWidget(central)
        self.nav.selectionModel().currentChanged.connect(self._nav_changed)
        self.ctx.navigate.connect(self.show_workspace)
        self.ctx.status_message.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.ctx.jobs.busy_changed.connect(self._busy_changed)
        # status bar
        self.engine_label = QLabel()
        self.statusBar().addPermanentWidget(self.engine_label)
        self._build_menus()
        T.watch(self, self._retranslate)
        self._retranslate()
        self._restore()
        self.show_workspace("inspector")
        if not self.ctx.engine.available:
            self.ctx.log.error("log.engine_unavailable", detail=self.ctx.engine.unavailable_reason)

    # ----------------------------------------------------------- menus
    def _action(self, key: str, slot, shortcut=None, icon_name: str | None = None) -> QAction:
        act = QAction(self)
        T.bind(act, "setText", key)
        if shortcut is not None:
            act.setShortcut(shortcut)
        if icon_name:
            act.setIcon(icons.icon(icon_name))
        act.triggered.connect(slot)
        return act

    def _build_menus(self) -> None:
        bar = self.menuBar()
        self.file_menu = bar.addMenu("")
        self.view_menu = bar.addMenu("")
        self.help_menu = bar.addMenu("")
        self.act_open = self._action("menu.open", self._open, QKeySequence.StandardKey.Open, "folder_open")
        self.act_quit = self._action("menu.quit", self.close, QKeySequence("Alt+F4"))
        self.file_menu.addAction(self.act_open)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.act_quit)
        self.ws_actions = []
        for i, cls in enumerate(WORKSPACES, start=1):
            act = self._action(f"nav.{cls.key}", lambda _c=False, k=cls.key: self.show_workspace(k),
                               QKeySequence(f"Ctrl+{i}"), cls.icon_name)
            self.view_menu.addAction(act)
            self.ws_actions.append(act)
        self.view_menu.addSeparator()
        self.act_panel = self._action("menu.toggle_panel", self._toggle_panel, QKeySequence("Ctrl+J"))
        self.act_panel.setCheckable(True)
        self.act_panel.setChecked(True)
        self.view_menu.addAction(self.act_panel)
        self.act_lang = self._action("menu.switch_language", self._switch_language, QKeySequence("Ctrl+Shift+L"),
                                     "local_language")
        self.view_menu.addAction(self.act_lang)
        self.act_check_updates = self._action("menu.check_updates", lambda _c=False: self.ctx.updates.check(manual=True))
        self.act_licenses = self._action("menu.licenses", self.show_licenses, None, "document_table")
        self.act_about = self._action("menu.about", self._about, None, "info")
        self.help_menu.addAction(self.act_check_updates)
        self.help_menu.addAction(self.act_licenses)
        self.help_menu.addSeparator()
        self.help_menu.addAction(self.act_about)

    def _retranslate(self) -> None:
        self.file_menu.setTitle(tr("menu.file"))
        self.view_menu.setTitle(tr("menu.view"))
        self.help_menu.setTitle(tr("menu.help"))
        self.nav.retranslate()
        if self.ctx.engine.available:
            self.engine_label.setText(tr("status.engine_ready"))
            self.engine_label.setToolTip("")
        else:
            self.engine_label.setText(tr("status.engine_unavailable"))
            self.engine_label.setToolTip(self.ctx.engine.unavailable_reason)

    # ------------------------------------------------------- navigation
    def show_workspace(self, key: str) -> None:
        ws = self.workspaces.get(key)
        if ws is None:
            return
        row = list(self.workspaces).index(key)
        if self.nav.currentIndex().row() != row:
            self.nav.setCurrentIndex(self.nav.nav_model.index(row, 0))
        self.stack.setCurrentWidget(ws)
        ws.activated()

    def current_workspace(self) -> Workspace:
        return self.stack.currentWidget()  # type: ignore[return-value]

    def _nav_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if current.isValid():
            key = current.data(Qt.ItemDataRole.UserRole)
            ws = self.workspaces[key]
            self.stack.setCurrentWidget(ws)
            ws.activated()

    def _open(self) -> None:
        self.show_workspace("inspector")
        self.workspaces["inspector"].browse()

    def open_profiles(self, paths: list[str]) -> None:
        self.show_workspace("inspector")
        self.workspaces["inspector"].open_paths(paths)  # type: ignore[attr-defined]

    def _toggle_panel(self, on: bool) -> None:
        self.bottom.setVisible(on)

    def _switch_language(self) -> None:
        lang = "ja" if T.language == "en" else "en"
        self.ctx.settings.setValue("ui/language", lang)
        T.set_language(lang)

    # ------------------------------------------------------------ safety net
    def _install_exception_hook(self) -> None:
        """Unhandled exceptions on the GUI thread used to go to stderr only,
        which the packaged application does not have: they vanished and left
        the control in whatever state it was.  Now they are logged, shown once
        (not for every repeat) and never terminate the application."""
        import sys
        import traceback

        previous = sys.excepthook
        while getattr(previous, "_cps_previous", None) is not None:   # installed before: do not chain to ourselves
            previous = previous._cps_previous
        self._reported_exceptions: set[str] = set()

        def hook(exc_type, exc, tb):
            try:
                text = "".join(traceback.format_exception(exc_type, exc, tb))
                signature = f"{exc_type.__name__}:{traceback.extract_tb(tb)[-1].lineno if tb else 0}"
                self.ctx.log.error("log.gui.unexpected", detail=f"{exc_type.__name__}: {exc}")
                if signature not in self._reported_exceptions and not getattr(self, "_force_close", False):
                    self._reported_exceptions.add(signature)
                    box = QMessageBox(QMessageBox.Icon.Warning, tr("dialog.error"), tr("error.unexpected_gui"), parent=self)
                    box.setInformativeText(f"{exc_type.__name__}: {exc}")
                    box.setDetailedText(text)
                    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
                    box.setModal(False)
                    box.show()
            except Exception:  # noqa: BLE001 - the hook itself must never raise
                pass
            if previous not in (None, sys.__excepthook__):
                try:
                    previous(exc_type, exc, tb)
                except Exception:  # noqa: BLE001
                    pass
            else:
                sys.__excepthook__(exc_type, exc, tb)

        hook._cps_previous = previous
        sys.excepthook = hook

    def about_box(self) -> QMessageBox:
        """Help > About: name and version first, description below."""
        box = QMessageBox(QMessageBox.Icon.Information, strip_mnemonic(tr("menu.about")),
                          f"{about.APP_NAME}\n{tr('about.version', version=about.APP_VERSION)}", parent=self)
        box.setInformativeText(tr("about.text"))
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._about_licenses = box.addButton(tr("about.show_licenses"), QMessageBox.ButtonRole.ActionRole)
        box.setDefaultButton(box.addButton(tr("action.ok"), QMessageBox.ButtonRole.AcceptRole))
        return box

    def _about(self) -> None:
        box = self.about_box()
        box.exec()
        if box.clickedButton() is self._about_licenses:
            self.show_licenses()

    def show_licenses(self) -> None:
        from .widgets.license_dialog import LicenseDialog

        LicenseDialog(self).exec()

    def _busy_changed(self, busy: bool) -> None:
        if busy:
            self.statusBar().showMessage(tr("status.working"))
        else:
            self.statusBar().clearMessage()

    shutdown_timeout_ms = 5000
    NAV_COMPACT_BELOW = 1000  # logical px; Windows NavigationView switches to compact on medium widths

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = self.width() < self.NAV_COMPACT_BELOW
        if compact != self.nav.compact:
            self.nav.set_compact(compact)
            sizes = self.hsplit.sizes()
            total = sum(sizes)
            nav_w = self.nav.sizeHint().width()
            self.hsplit.setSizes([nav_w, max(0, total - nav_w)])

    # ------------------------------------------------------- persistence
    def _restore(self) -> None:
        s = self.ctx.settings
        # Saved window state is data from disk: a damaged value must not stop start-up.
        geo = s.value("window/geometry")
        restored = False
        if isinstance(geo, (bytes, bytearray, QByteArray)) and len(geo):
            try:
                restored = bool(self.restoreGeometry(geo))
            except Exception:  # noqa: BLE001
                restored = False
        if not restored or self.width() < 320 or self.height() < 240:
            self.resize(1280, 820)
        for name, splitter in (("h", self.hsplit), ("v", self.vsplit)):
            state = s.value(f"window/split_{name}")
            if isinstance(state, (bytes, bytearray, QByteArray)) and len(state):
                try:
                    splitter.restoreState(state)
                except Exception:  # noqa: BLE001
                    pass
        if s.value("window/split_h") is None:
            self.hsplit.setSizes([self.nav.sizeHint().width(), 1000])
            self.vsplit.setSizes([620, 180])

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        jobs = self.ctx.jobs
        if getattr(self, "_closing", False):
            # Second pass: only close once every worker thread has settled.
            if jobs.shutdown(0):
                event.accept()
            else:
                event.ignore()
                QTimer.singleShot(200, self.close)
            return
        running = [r for r in jobs.active() if not r.background]   # an update check never blocks closing
        if running and not getattr(self, "_force_close", False):
            if not self.ctx.confirm("dialog.quit_title", "dialog.quit_running", n=len(running)):
                event.ignore()
                return
        s = self.ctx.settings
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/split_h", self.hsplit.saveState())
        s.setValue("window/split_v", self.vsplit.saveState())
        for ws in self.workspaces.values():
            ws.shutdown()
        self._closing = True
        if jobs.shutdown(self.shutdown_timeout_ms):
            event.accept()
            return
        # Native calls may not be interruptible: keep the window (and the
        # QObjects workers signal) alive and retry until the pool is idle.
        event.ignore()
        self.setEnabled(False)
        self.statusBar().showMessage(tr("status.stopping"))
        QTimer.singleShot(200, self.close)



