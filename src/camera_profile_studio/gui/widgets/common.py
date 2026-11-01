"""Small reusable widgets built only from standard Qt Widgets.

No style sheets are used: appearance comes entirely from the active QStyle
(``windows11`` when available) and the system palette.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont, QFontMetrics, QKeySequence, QPalette, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..i18n import T, tr
from .. import icons

# Fluent 2 global spacing ramp (4 px base unit): size40=4, size80=8,
# size120=12, size160=16, size240=24.  Values are logical pixels; Qt scales
# them with the device pixel ratio.
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24


def title_font(widget: QWidget, delta: float = 6.0, weight: QFont.Weight = QFont.Weight.DemiBold) -> QFont:
    """System font enlarged for headings (Windows type ramp: Subtitle/Title)."""
    font = QFont(widget.font())
    size = font.pointSizeF() if font.pointSizeF() > 0 else 9.0
    font.setPointSizeF(size + delta)
    font.setWeight(weight)
    return font


class HeadingLabel(QLabel):
    def __init__(self, key: str = "", level: int = 1, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFont(title_font(self, {1: 8.0, 2: 3.0, 3: 1.0}.get(level, 1.0)))
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        if key:
            T.bind(self, "setText", key)


class WrapLabel(QLabel):
    """Word-wrapped body/caption text with optional secondary colour."""

    def __init__(self, key: str = "", secondary: bool = False, parent: QWidget | None = None, **kwargs) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._apply_secondary(secondary)
        if key:
            T.bind(self, "setText", key, **kwargs)
    def minimumSizeHint(self) -> QSize:  # noqa: N802
        # Word-wrapped labels otherwise report the height needed at a tiny
        # width, inflating the window minimum; layouts still use
        # heightForWidth for the real height.
        fm = self.fontMetrics()
        return QSize(min(super().minimumSizeHint().width(), 120), fm.height())

    def _apply_secondary(self, secondary: bool) -> None:
        if secondary:
            # Secondary text uses the palette's own role; follows Light/Dark.
            self.setForegroundRole(QPalette.ColorRole.PlaceholderText)

class PathLabel(QLabel):
    """Shows a long file path elided in the middle; full path in tooltip.

    The full path is also exposed as accessible description and can be
    selected/copied, so nothing is lost when the label is narrow.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = ""
        self._empty_key = "path.none"
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(60)
        T.watch(self, lambda: self._update())
        self._update()

    def set_path(self, path: str | Path | None, empty_key: str = "path.none") -> None:
        self._path = str(path) if path else ""
        self._empty_key = empty_key
        self._update()

    def path(self) -> str:
        return self._path

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update()

    def _update(self) -> None:
        if not self._path:
            super().setText(tr(self._empty_key))
            self.setToolTip("")
            self.setAccessibleDescription(tr(self._empty_key))
            return
        fm = QFontMetrics(self.font())
        width = max(40, self.width() - 4)
        super().setText(fm.elidedText(self._path, Qt.TextElideMode.ElideMiddle, width))
        self.setToolTip(self._path)
        self.setAccessibleDescription(self._path)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(60, super().minimumSizeHint().height())


class WrapChoice(QWidget):
    """A standard QCheckBox/QRadioButton whose text wraps.

    Qt's check/radio buttons cannot word-wrap, which clips long Japanese
    labels in narrow side panels.  The text lives in a word-wrapped QLabel;
    clicking the label toggles the button and the button carries the
    accessible name, so keyboard (Space) and screen readers work unchanged.
    """

    toggled = Signal(bool)

    def __init__(self, key: str, radio: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QCheckBox, QRadioButton

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SPACE_XS)
        self.button = QRadioButton(self) if radio else QCheckBox(self)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.label.setBuddy(self.button)
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.label, 1)
        self.setFocusProxy(self.button)
        T.bind(self.label, "setText", key)
        T.bind(self.button, "setAccessibleName", key)
        self.button.toggled.connect(self.toggled)
        self.label.mousePressEvent = self._label_clicked  # type: ignore[method-assign]

    def _label_clicked(self, event) -> None:
        if self.button.isEnabled():
            if isinstance(self.button, QAbstractButton) and self.button.isCheckable():
                if self.button.autoExclusive() and self.button.isChecked():
                    return
                self.button.setChecked(not self.button.isChecked())
            self.button.setFocus(Qt.FocusReason.MouseFocusReason)

    def isChecked(self) -> bool:  # noqa: N802
        return self.button.isChecked()

    def setChecked(self, on: bool) -> None:  # noqa: N802
        self.button.setChecked(on)

    def setToolTip(self, text: str) -> None:  # noqa: N802
        super().setToolTip(text)
        self.button.setToolTip(text)
        self.label.setToolTip(text)

    def text(self) -> str:
        return self.label.text()


class AdvancedSection(QWidget):
    """Disclosure section: a checkable tool button revealing grouped settings."""

    toggled = Signal(bool)

    def __init__(self, key: str = "advanced.title", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_S)
        self.button = QToolButton(self)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setCheckable(True)
        self.button.setArrowType(Qt.ArrowType.RightArrow)
        self.button.setAutoRaise(True)
        self.button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        T.bind(self.button, "setText", key)
        T.bind(self.button, "setAccessibleName", key)
        self.body = QWidget(self)
        self.body.setVisible(False)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(SPACE_S, 0, 0, 0)
        self.body_layout.setSpacing(SPACE_S)
        layout.addWidget(self.button)
        layout.addWidget(self.body)
        self.button.toggled.connect(self._on_toggled)

    def _on_toggled(self, on: bool) -> None:
        self.button.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.body.setVisible(on)
        self.toggled.emit(on)

    def set_expanded(self, on: bool) -> None:
        self.button.setChecked(on)

    def is_expanded(self) -> bool:
        return self.button.isChecked()

    def add_widget(self, widget: QWidget) -> None:
        self.body_layout.addWidget(widget)

    def add_layout(self, layout: QLayout) -> None:
        self.body_layout.addLayout(layout)


class SidePanel(QScrollArea):
    """Right-hand settings column; scrolls vertically, never clips widgets."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Never clip: if the user drags the splitter narrower than the
        # content minimum, scroll instead.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.inner = QWidget()
        self.layout_ = QVBoxLayout(self.inner)
        self.layout_.setContentsMargins(SPACE_L, SPACE_M, SPACE_L, SPACE_L)
        self.layout_.setSpacing(SPACE_M)
        self.setWidget(self.inner)
        self.setMinimumWidth(260)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.layout_.addWidget(widget, stretch)
        return widget

    def add_stretch(self) -> None:
        self.layout_.addStretch(1)


class DisabledReason(QLabel):
    """Explains why the primary action is unavailable (never a silent no-op)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._key = ""
        self._kwargs: dict = {}
        T.watch(self, lambda: self._render())
        self.setVisible(False)

    def set_reason(self, key: str | None, **kwargs) -> None:
        self._key = key or ""
        self._kwargs = kwargs
        self._render()

    def reason_text(self) -> str:
        return self.text() if self._key else ""

    def _render(self) -> None:
        if self._key:
            self.setText(tr(self._key, **self._kwargs))
            self.setAccessibleName(self.text())
            self.setVisible(True)
        else:
            self.setText("")
            self.setVisible(False)


class CommandButton(QPushButton):
    """QPushButton whose explicit keyboard shortcut survives text changes.

    ``QAbstractButton.setText`` replaces the button's shortcut with the new
    text's mnemonic.  T.bind re-runs setText on every language switch, so
    shortcuts such as Ctrl+E, Esc or F5 were silently dropped after the
    first switch while their labels still advertised them.
    """

    _fixed_shortcut: QKeySequence | None = None

    def setShortcut(self, key) -> None:  # noqa: N802 - Qt override
        self._fixed_shortcut = QKeySequence(key) if key else None
        super().setShortcut(self._fixed_shortcut or QKeySequence())

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setText(text)
        if self._fixed_shortcut is not None:
            super().setShortcut(self._fixed_shortcut)


def make_button(key: str, icon_name: str | None = None, *, primary: bool = False, parent: QWidget | None = None,
                tooltip_key: str | None = None) -> QPushButton:
    button = CommandButton(parent)
    T.bind(button, "setText", key)
    T.bind(button, "setAccessibleName", key)
    if tooltip_key:
        T.bind(button, "setToolTip", tooltip_key)
    if icon_name:
        button.setIcon(icons.icon(icon_name))
    if primary:
        # Primary command: QStyle draws the default (accent) button natively.
        button.setDefault(True)
        button.setAutoDefault(True)
    return button


def make_tool_button(key: str, icon_name: str, parent: QWidget | None = None, shortcut_hint: str = "") -> QToolButton:
    button = QToolButton(parent)
    button.setIcon(icons.icon(icon_name))
    button.setAutoRaise(True)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    T.bind(button, "setToolTip", key)
    T.bind(button, "setAccessibleName", key)
    return button


def set_enabled_with_reason(button: QAbstractButton, reason_label: DisabledReason, reason_key: str | None, **kwargs) -> None:
    button.setEnabled(reason_key is None)
    reason_label.set_reason(reason_key, **kwargs)
    if reason_key:
        button.setToolTip(tr(reason_key, **kwargs))
    else:
        button.setToolTip("")


def fill_combo(combo: QComboBox, items: Iterable[tuple[str, object]], keep: bool = True) -> None:
    """Fill a combo with (translation key or literal, data) pairs, retaining selection."""
    # Long (especially Japanese) item texts must not dictate the side-panel
    # minimum width; the popup still shows full text and tooltips.
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(8)
    current = combo.currentData() if keep else None
    combo.blockSignals(True)
    combo.clear()
    for key, data in items:
        combo.addItem(tr(key), data)
        combo.setItemData(combo.count() - 1, key, Qt.ItemDataRole.UserRole + 1)
    if current is not None:
        idx = combo.findData(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
    combo.blockSignals(False)


def retranslate_combo(combo: QComboBox) -> None:
    for i in range(combo.count()):
        key = combo.itemData(i, Qt.ItemDataRole.UserRole + 1)
        if isinstance(key, str):
            combo.setItemText(i, tr(key))


def bind_combo(combo: QComboBox) -> None:
    T.watch(combo, lambda: retranslate_combo(combo))


class DropArea(QFrame):
    """Accepts dropped files whose suffix is in ``suffixes``; click opens dialog."""

    files_dropped = Signal(list)
    browse_requested = Signal()

    def __init__(self, suffixes: Iterable[str], key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.suffixes = {s.lower() for s in suffixes}
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Sunken)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_L, SPACE_M, SPACE_L, SPACE_M)
        layout.setSpacing(SPACE_M)
        self.icon_label = QLabel(self)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        self.label = WrapLabel(key, parent=self)
        layout.addWidget(self.label, 1)
        self.browse = make_button("action.browse", "folder_open", parent=self)
        self.browse.clicked.connect(self.browse_requested)
        layout.addWidget(self.browse, 0, Qt.AlignmentFlag.AlignVCenter)
        self._set_icon()
        T.bind(self, "setAccessibleName", key)

    def _set_icon(self) -> None:
        self.icon_label.setPixmap(icons.icon("document_search").pixmap(QSize(24, 24), self.devicePixelRatioF()))

    def changeEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.PaletteChange:
            self._set_icon()
        super().changeEvent(event)

    def accepts(self, paths: list[str]) -> list[str]:
        return [p for p in paths if Path(p).suffix.lower() in self.suffixes]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if self.accepts([u.toLocalFile() for u in urls if u.isLocalFile()]):
            event.acceptProposedAction()
            self.setFrameShadow(QFrame.Shadow.Raised)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setFrameShadow(QFrame.Shadow.Sunken)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self.setFrameShadow(QFrame.Shadow.Sunken)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        accepted = self.accepts(paths)
        if accepted:
            event.acceptProposedAction()
            self.files_dropped.emit(accepted)


def hline(parent: QWidget | None = None) -> QFrame:
    line = QFrame(parent)
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def connect_once(signal, slot: Callable) -> None:
    signal.connect(slot)







