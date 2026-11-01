"""Capability status display (Technical / Look / Match / JPEG accuracy)."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFormLayout, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import icons
from ..backend import Capability
from ..i18n import T, tr
from .common import SPACE_S, SPACE_XS, title_font

POSITIVE = {"supported", "available", "verified"}
PARTIAL = {"partial", "unverified", "unknown"}

def status_icon(status: str) -> str:
    if status in POSITIVE:
        return "checkmark_circle"
    if status in PARTIAL:
        return "warning"
    return "dismiss_circle"


class CapabilityRow(QWidget):
    def __init__(self, parent: QWidget | None = None, show_title: bool = False) -> None:
        super().__init__(parent)
        self._show_title = show_title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(SPACE_S)
        self.icon = QLabel(self)
        self.icon.setFixedSize(QSize(20, 20))
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.status, 1)
        layout.addLayout(top)
        self.reason = QLabel(self)
        self.reason.setWordWrap(True)
        self.reason.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.reason.setContentsMargins(28, 0, 0, 0)
        layout.addWidget(self.reason)
        self._cap: Capability | None = None
        self._title_key = ""
        T.watch(self, self._render)

    def changeEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.PaletteChange:
            self._render()  # re-tint the status icon for Light/Dark
        super().changeEvent(event)

    def set_capability(self, title_key: str, cap: Capability | None) -> None:
        self._title_key = title_key
        self._cap = cap
        self._render()

    def _render(self) -> None:
        cap = self._cap
        if cap is None:
            self.icon.clear()
            self.status.setText(tr("cap.none_loaded"))
            self.reason.setVisible(False)
            self.setAccessibleName(tr(self._title_key) if self._title_key else "")
            return
        self.icon.setPixmap(icons.icon(status_icon(cap.status)).pixmap(QSize(20, 20), self.devicePixelRatioF()))
        status = tr(f"cap.{cap.status}")
        self.status.setText(f"{tr(self._title_key)} \u2014 {status}" if self._show_title else status)
        reason = tr(cap.reason_key) if cap.reason_key else cap.reason
        self.reason.setText(reason)
        self.reason.setVisible(bool(reason))
        self.setAccessibleName(f"{tr(self._title_key)}: {tr('cap.' + cap.status)}")
        self.setAccessibleDescription(reason)


class CapabilityPanel(QGroupBox):
    ROWS = (
        ("technical", "cap.title.technical"),
        ("look", "cap.title.look"),
        ("match", "cap.title.match"),
        ("jpeg", "cap.title.jpeg"),
    )

    def __init__(self, key: str = "cap.group", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        T.bind(self, "setTitle", key)
        T.bind(self, "setAccessibleName", key)
        self.form = QFormLayout(self)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.form.setVerticalSpacing(SPACE_S)
        self.form.setHorizontalSpacing(SPACE_S)
        self.rows: dict[str, CapabilityRow] = {}
        self.labels: dict[str, QLabel] = {}
        for name, title in self.ROWS:
            label = QLabel(self)
            label.setFont(title_font(label, 0.0))
            T.bind(label, "setText", title)
            row = CapabilityRow(self)
            label.setBuddy(row)
            self.form.addRow(label, row)
            self.rows[name] = row
            self.labels[name] = label

    def set_info(self, info) -> None:
        for name, title in self.ROWS:
            self.rows[name].set_capability(title, getattr(info, name) if info is not None else None)


class ComponentList(QGroupBox):
    """Generic list of named capabilities, e.g. DCP look components."""

    def __init__(self, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        T.bind(self, "setTitle", key)
        T.bind(self, "setAccessibleName", key)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setSpacing(SPACE_XS + 2)
        self.rows: list[CapabilityRow] = []
        self.empty = QLabel(self)
        self.empty.setWordWrap(True)
        T.bind(self.empty, "setText", "cap.none_loaded")
        self.layout_.addWidget(self.empty)

    def set_items(self, items: dict[str, Capability], prefix: str) -> None:
        for row in self.rows:
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        self.empty.setVisible(not items)
        for name, cap in items.items():
            row = CapabilityRow(self, show_title=True)
            row.set_capability(f"{prefix}.{name}", cap)
            self.layout_.addWidget(row)
            self.rows.append(row)




