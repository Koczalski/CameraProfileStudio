"""Common workspace scaffold.

Layout per Microsoft "Layout" / Fluent 2 guidance and the spec:

* top: page title (current screen name) + primary commands
* centre: work area / preview (stretch)
* right: settings column inside a scroll area
* centre and right are separated by a QSplitter the user can drag

The bottom Jobs/Log panel is owned by the main window and shared.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget

from ..context import AppContext
from ..widgets.common import SPACE_L, SPACE_M, SPACE_S, HeadingLabel, SidePanel, WrapLabel


class Workspace(QWidget):
    key = ""  # e.g. "inspector"
    icon_name = ""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName(f"workspace_{self.key}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QWidget(self)
        hl = QVBoxLayout(header)
        hl.setContentsMargins(SPACE_L + 4, SPACE_M, SPACE_L, SPACE_S)
        hl.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(SPACE_S)
        self.title = HeadingLabel(f"nav.{self.key}", 1, header)
        # Wraps instead of forcing a wide minimum window in narrow layouts.
        self.title.setWordWrap(True)
        top.addWidget(self.title, 1)
        self.commands = QHBoxLayout()
        self.commands.setSpacing(SPACE_S)
        top.addLayout(self.commands)
        hl.addLayout(top)
        self.subtitle = WrapLabel(f"{self.key}.subtitle", secondary=True, parent=header)
        hl.addWidget(self.subtitle)
        outer.addWidget(header)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setObjectName(f"splitter_{self.key}")
        outer.addWidget(self.splitter, 1)
        self.center = QWidget(self.splitter)
        self.center_layout = QVBoxLayout(self.center)
        self.center_layout.setContentsMargins(SPACE_L, SPACE_S, SPACE_S, SPACE_S)
        self.center_layout.setSpacing(SPACE_M)
        self.side = SidePanel(self.splitter)
        self.splitter.addWidget(self.center)
        self.splitter.addWidget(self.side)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([700, 340])

    def add_command(self, widget: QWidget) -> None:
        self.commands.addWidget(widget)

    def activated(self) -> None:
        """Called when the workspace becomes visible."""

    def primary_focus(self) -> QWidget | None:
        return None

    def shutdown(self) -> None:
        """Called before the window closes (jobs are cancelled centrally)."""

