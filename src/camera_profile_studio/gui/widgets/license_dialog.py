"""Licences dialog: application version, every bundled component with its
version and licence, and the full licence text of the selected component."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import about
from ..i18n import T, tr
from .common import SPACE_M, SPACE_S, HeadingLabel, PathLabel, WrapLabel, make_button


class LicenseDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, base=None) -> None:
        super().__init__(parent)
        self.setObjectName("license_dialog")
        T.bind(self, "setWindowTitle", "about.licenses_title")
        self.resize(1120, 680)  # licence texts are mostly fixed at ~80 columns
        self.base = about.licenses_dir() if base is None else base
        self.entries = about.license_entries(self.base)

        lay = QVBoxLayout(self)
        lay.setSpacing(SPACE_M)
        self.heading = HeadingLabel("", 2, self)
        self.heading.setText(f"{about.APP_NAME} {about.APP_VERSION}")
        lay.addWidget(self.heading)
        lay.addWidget(WrapLabel("about.licenses_intro", secondary=True, parent=self))

        folder_row = QHBoxLayout()
        folder_row.setSpacing(SPACE_S)
        folder_label = QLabel(self)
        T.bind(folder_label, "setText", "about.licenses_folder")
        self.folder = PathLabel(self)
        self.folder.set_path(self.base, "about.licenses_missing")
        self.open_button = make_button("about.open_folder", "folder_open", parent=self)
        self.open_button.setEnabled(self.base is not None)
        folder_row.addWidget(folder_label)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(self.open_button)
        lay.addLayout(folder_row)

        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.setChildrenCollapsible(False)
        self.model = QStandardItemModel(self)
        self.table = QTableView(split)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().height() + 10)
        T.bind(self.table, "setAccessibleName", "about.components")
        right = QWidget(split)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(SPACE_S)
        self.note = WrapLabel("", secondary=True, parent=right)
        self.note.setVisible(False)
        self.text = QPlainTextEdit(right)
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        T.bind(self.text, "setAccessibleName", "about.license_text")
        rl.addWidget(self.note)
        rl.addWidget(self.text, 1)
        split.addWidget(self.table)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([440, 660])
        lay.addWidget(split, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = make_button("action.close", None, primary=True, parent=self)
        buttons.addWidget(self.close_button)
        lay.addLayout(buttons)

        self.open_button.clicked.connect(self._open_folder)
        self.close_button.clicked.connect(self.accept)
        self._fill()
        self.table.selectionModel().currentRowChanged.connect(lambda cur, _prev: self._show(cur.row()))
        T.watch(self, self._retranslate)
        self._retranslate()
        self.table.selectRow(0)
        self._show(0)
        self.table.setFocus()

    def _display_name(self, entry: about.LicenseEntry) -> str:
        return tr("about.distribution_notices") if entry.kind == "notice" else entry.name

    def _fill(self) -> None:
        self.model.removeRows(0, self.model.rowCount())
        for e in self.entries:
            lic = e.license or (tr("about.license_see_text") if e.files else "—")
            if e.bundled:
                lic = tr("about.license_with_bundled", license=lic)
            row = [QStandardItem(self._display_name(e)), QStandardItem(e.version or "—"), QStandardItem(lic)]
            for item in row:
                item.setToolTip(item.text())
            self.model.appendRow(row)

    def _retranslate(self) -> None:
        row = max(0, self.table.currentIndex().row())
        self._fill()
        self.model.setHorizontalHeaderLabels([tr("about.col.name"), tr("about.col.version"), tr("about.col.license")])
        header = self.table.horizontalHeader()
        # Name keeps a readable width; the licence column takes the rest and elides.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        fm = self.table.fontMetrics()
        widest = max((fm.horizontalAdvance(self._display_name(e)) for e in self.entries), default=120)
        header.resizeSection(0, min(widest + 24, 220))
        if self.entries:
            self.table.selectRow(min(row, len(self.entries) - 1))
            self._show(min(row, len(self.entries) - 1))

    def _show(self, row: int) -> None:
        if not 0 <= row < len(self.entries):
            return
        e = self.entries[row]
        note = tr("about.installer_only") if e.kind == "installer" else ""
        if e.kind == "library" and not e.files:
            note = tr("about.no_license_file")
        self.note.setText(note)
        self.note.setVisible(bool(note))
        if e.files:
            self.text.setPlainText(about.read_text(e, self.base))
        elif e.kind == "app":
            # Nothing to read for the application itself: say so where the text would be.
            lines = [f"{about.APP_NAME}", tr("about.version", version=about.APP_VERSION), "", tr("about.app_license_missing")]
            if self.base is None:
                lines += ["", tr("about.licenses_missing")]
            self.text.setPlainText("\n".join(lines))
        else:
            self.text.setPlainText(tr("about.no_license_file") if e.kind == "library" else "")
        self.text.moveCursor(self.text.textCursor().MoveOperation.Start)

    def current_text(self) -> str:
        return self.text.toPlainText()

    def _open_folder(self) -> None:
        if self.base is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.base)))
