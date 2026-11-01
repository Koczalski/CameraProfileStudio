"""Shared application state passed to every workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from . import icons
from .backend import Engine, ProfileInfo
from .i18n import T, tr
from .jobs import ErrorInfo, JobManager


def setting_int(settings: QSettings, key: str, default: int, allowed: tuple[int, ...] | None = None) -> int:
    """Read an integer setting without trusting the file: a hand-edited or
    damaged settings file must never stop the application from starting
    (0.2.4). Unparsable or disallowed values fall back to ``default``."""
    raw = settings.value(key, default)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if allowed is not None and value not in allowed:
        return default
    return value


def setting_choice(settings: QSettings, key: str, default: str, allowed: tuple[str, ...]) -> str:
    value = str(settings.value(key, default))
    return value if value in allowed else default
from .widgets.log_panel import Logger, LogModel

PROFILE_SUFFIXES = (".icc", ".icm", ".dcp")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff")


@dataclass
class ProfileEntry:
    path: str
    state: str = "loading"  # loading | ready | failed
    info: ProfileInfo | None = None
    error: ErrorInfo | None = None
    job_id: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return Path(self.path).name


class ProfilesModel(QAbstractTableModel):
    """Loaded profiles (Model/View) shared by Inspector, Converter and Look."""

    COLUMNS = ("profiles.col.name", "profiles.col.format", "profiles.col.state", "profiles.col.technical", "profiles.col.look")
    PathRole = Qt.ItemDataRole.UserRole + 10

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.entries: list[ProfileEntry] = []
        T.watch(self, lambda: self._retranslate(""))

    def _retranslate(self, _l: str) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.COLUMNS) - 1)
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, len(self.COLUMNS) - 1))

    def row_of(self, path: str) -> int:
        norm = str(Path(path))
        for i, e in enumerate(self.entries):
            if str(Path(e.path)) == norm:
                return i
        return -1

    def upsert(self, entry: ProfileEntry) -> int:
        row = self.row_of(entry.path)
        if row >= 0:
            self.entries[row] = entry
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.COLUMNS) - 1))
            return row
        row = len(self.entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self.entries.append(entry)
        self.endInsertRows()
        return row

    def changed(self, row: int) -> None:
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.COLUMNS) - 1))

    def remove(self, row: int) -> None:
        if 0 <= row < len(self.entries):
            self.beginRemoveRows(QModelIndex(), row, row)
            del self.entries[row]
            self.endRemoveRows()

    def entry(self, row: int) -> ProfileEntry | None:
        return self.entries[row] if 0 <= row < len(self.entries) else None

    def ready_entries(self) -> list[ProfileEntry]:
        return [e for e in self.entries if e.state == "ready"]

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(self.COLUMNS[section])
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        col = index.column()
        if role == self.PathRole:
            return e.path
        if role == Qt.ItemDataRole.ToolTipRole:
            if e.error is not None:
                return f"{e.path}\n{e.error.summary()}\n{e.error.detail}".strip()
            return e.path
        if role == Qt.ItemDataRole.DecorationRole and col == 2:
            name = {"ready": "checkmark_circle", "failed": "error_circle"}.get(e.state, "info")
            return icons.icon(name)
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return None
        if col == 0:
            return e.name
        if col == 1:
            return e.info.format_label if e.info else Path(e.path).suffix.upper().lstrip(".")
        if col == 2:
            return tr(f"profile.state.{e.state}")
        if col == 3:
            return tr(f"cap.{e.info.technical.status}") if e.info else ""
        if col == 4:
            return tr(f"cap.{e.info.look.status}") if e.info else ""
        return None


class AppContext(QObject):
    """Holds the engine adapter, jobs, log, profiles and persistent settings."""

    current_profile_changed = Signal(object)  # ProfileEntry | None
    profile_ready = Signal(object)  # ProfileEntry
    status_message = Signal(str)
    navigate = Signal(str)

    def __init__(self, engine: Engine, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.jobs = JobManager(self)
        self.jobs.error_mapper = engine.map_error
        self.log_model = LogModel(self)
        self.log = Logger(self.log_model)
        self.profiles = ProfilesModel(self)
        self._current: ProfileEntry | None = None
        self.window: QWidget | None = None

    # -------------------------------------------------------------- profiles
    @property
    def current_profile(self) -> ProfileEntry | None:
        return self._current

    def set_current_profile(self, entry: ProfileEntry | None) -> None:
        if entry is not self._current:
            self._current = entry
            self.current_profile_changed.emit(entry)

    def open_profiles(self, paths: list[str], make_current: bool = True) -> list[str]:
        """Start inspection jobs for each profile path (non-blocking).

        ``make_current=False`` (e.g. picking a converter *output* profile)
        never changes the globally selected input profile.
        """
        started = []
        for raw in paths:
            path = str(Path(raw))
            if Path(path).suffix.lower() not in PROFILE_SUFFIXES:
                self.log.warning("log.profile.unsupported_suffix", detail=path)
                continue
            entry = ProfileEntry(path)
            row = self.profiles.upsert(entry)
            self.log.info("log.profile.inspecting", detail=path)

            def work(token, progress, _p=path):
                return self.engine.inspect(_p, token, progress)

            def done(info, _e=entry):
                _e.state, _e.info = "ready", info
                self._refresh(_e)
                self.log.success("log.profile.inspected", detail=_e.path, name=_e.name)
                self.profile_ready.emit(_e)
                if self._current is _e:
                    self.current_profile_changed.emit(_e)
                elif self._current is None and make_current:
                    self.set_current_profile(_e)

            def failed(err: ErrorInfo, _e=entry):
                _e.state, _e.error = "failed", err
                self._refresh(_e)
                self.log.error("log.profile.failed", detail=f"{_e.path}: {err.summary()} {err.detail}".strip(), name=_e.name)
                if self._current is _e:
                    self.current_profile_changed.emit(_e)

            def cancelled(_e=entry):
                _e.state = "failed"
                _e.error = ErrorInfo("error.cancelled")
                self._refresh(_e)
                self.log.warning("log.cancelled", detail=_e.path)

            entry.job_id = self.jobs.submit(
                f"inspect:{path}",
                "job.inspect",
                work,
                on_result=done,
                on_error=failed,
                on_cancel=cancelled,
                title_kwargs={"name": Path(path).name},
            )
            if row == 0 and self._current is None and make_current:
                self.set_current_profile(entry)
            started.append(path)
        return started

    def _refresh(self, entry: ProfileEntry) -> None:
        row = self.profiles.row_of(entry.path)
        if row >= 0 and self.profiles.entries[row] is entry:
            self.profiles.changed(row)

    # -------------------------------------------------------------- dialogs
    def last_dir(self, kind: str) -> str:
        return str(self.settings.value(f"paths/{kind}", str(Path.home())))

    def remember_dir(self, kind: str, path: str) -> None:
        p = Path(path)
        self.settings.setValue(f"paths/{kind}", str(p if p.is_dir() else p.parent))

    def ask_profiles(self) -> list[str]:
        files, _ = QFileDialog.getOpenFileNames(
            self.window, tr("dialog.open_profiles"), self.last_dir("profiles"), tr("filter.profiles")
        )
        if files:
            self.remember_dir("profiles", files[0])
        return files

    def ask_image(self, title_key: str = "dialog.open_image") -> str:
        file, _ = QFileDialog.getOpenFileName(self.window, tr(title_key), self.last_dir("images"), tr("filter.images"))
        if file:
            self.remember_dir("images", file)
        return file

    def ask_save(self, title_key: str, suggested: str, filter_key: str, kind: str) -> str:
        start = str(Path(self.last_dir(kind)) / suggested)
        # Native dialog asks for overwrite confirmation itself.
        file, _ = QFileDialog.getSaveFileName(self.window, tr(title_key), start, tr(filter_key))
        if file:
            self.remember_dir(kind, file)
        return file

    def ask_save_typed(self, title_key: str, suggested: str, choices: list[tuple[str, str]], current: str,
                       kind: str) -> tuple[str, str]:
        """Save dialog whose "Save as type" list offers every ``choices``
        entry ``(key, filter_key)``, preselecting ``current``.

        Returns ``(path, key)`` where ``key`` is the type chosen in the dialog
        (``current`` if the dialog does not report one)."""
        start = str(Path(self.last_dir(kind)) / suggested)
        keys = [k for k, _ in choices]
        filters = [tr(f) for _, f in choices]
        selected = filters[keys.index(current)] if current in keys else filters[0]
        file, chosen = QFileDialog.getSaveFileName(self.window, tr(title_key), start, ";;".join(filters), selected)
        if file:
            self.remember_dir(kind, file)
        key = keys[filters.index(chosen)] if chosen in filters else current
        return file, key

    def ask_directory(self, title_key: str, kind: str) -> str:
        folder = QFileDialog.getExistingDirectory(self.window, tr(title_key), self.last_dir(kind))
        if folder:
            self.remember_dir(kind, folder)
        return folder

    def confirm(self, title_key: str, text_key: str, **kwargs) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, tr(title_key), tr(text_key, **kwargs), parent=self.window)
        yes = box.addButton(tr("action.yes"), QMessageBox.ButtonRole.YesRole)
        no = box.addButton(tr("action.no"), QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    def show_error(self, err: ErrorInfo, title_key: str = "dialog.error") -> None:
        box = QMessageBox(QMessageBox.Icon.Critical, tr(title_key), err.summary(), parent=self.window)
        if err.detail:
            box.setInformativeText(err.detail)
        if err.traceback:
            box.setDetailedText(err.traceback)
        box.addButton(tr("action.ok"), QMessageBox.ButtonRole.AcceptRole)
        box.exec()



