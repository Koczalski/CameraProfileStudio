"""Preview panel: toolbar (load image, compare mode, zoom) + ImageViewer.

Image decoding and LUT application are submitted as jobs on the
``<owner>:image`` / ``<owner>:preview`` channels so that only the newest
request's result reaches the viewer (generation IDs in JobManager).
"""

from __future__ import annotations

from typing import Callable

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..backend import ImageData, LutData, to_qimage
from ..i18n import T, tr
from ..jobs import ErrorInfo
from .common import SPACE_S, PathLabel, WrapLabel, bind_combo, fill_combo, make_button, make_tool_button
from .viewer import CompareMode, ImageViewer

PREVIEW_MAX_SIDE = 2048

MODES = (
    ("viewer.mode.split", CompareMode.SPLIT.value),
    ("viewer.mode.side", CompareMode.SIDE_BY_SIDE.value),
    ("viewer.mode.before", CompareMode.BEFORE.value),
    ("viewer.mode.after", CompareMode.AFTER.value),
)


class PreviewPanel(QWidget):
    image_loaded = Signal(object)  # ImageData

    def __init__(self, ctx, owner: str, parent: QWidget | None = None, *, allow_load: bool = True) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.owner = owner
        self.source: ImageData | None = None
        self._before_qimage = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_S)
        bar = QHBoxLayout()
        bar.setSpacing(SPACE_S)
        self.load_button = make_button("preview.load_image", "image")
        self.load_button.setVisible(allow_load)
        self._load_text = self.load_button.text()
        T.unbind(self.load_button)
        T.bind(self.load_button, "setAccessibleName", "preview.load_image")
        T.watch(self, self._retext_load)
        bar.addWidget(self.load_button)
        self.mode_label = QLabel(self)
        T.bind(self.mode_label, "setText", "preview.compare")
        self.mode_combo = QComboBox(self)
        fill_combo(self.mode_combo, MODES)
        self.mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        bind_combo(self.mode_combo)
        self.mode_label.setBuddy(self.mode_combo)
        T.bind(self.mode_combo, "setAccessibleName", "preview.compare")
        bar.addWidget(self.mode_label)
        bar.addWidget(self.mode_combo)
        bar.addStretch(1)
        self.zoom_out_btn = make_tool_button("viewer.zoom_out", "zoom_out", self)
        self.zoom_in_btn = make_tool_button("viewer.zoom_in", "zoom_in", self)
        self.fit_btn = make_tool_button("viewer.fit", "zoom_fit", self)
        self.zoom_label = QLabel("", self)
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for w in (self.zoom_label, self.zoom_out_btn, self.zoom_in_btn, self.fit_btn):
            bar.addWidget(w)
        layout.addLayout(bar)
        self.viewer = ImageViewer(self)
        layout.addWidget(self.viewer, 1)
        self.path_label = PathLabel(self)
        layout.addWidget(self.path_label)
        self.note = WrapLabel("", secondary=True, parent=self)
        self.note.setVisible(False)
        layout.addWidget(self.note)
        self._note_key = ""
        self._note_kwargs: dict = {}
        T.watch(self, self._render_note)

        self.load_button.clicked.connect(self.browse)
        self.mode_combo.currentIndexChanged.connect(lambda _i: self.viewer.set_mode(self.mode_combo.currentData()))
        self.viewer.mode_changed.connect(self._sync_mode)
        self.zoom_in_btn.clicked.connect(self.viewer.zoom_in)
        self.zoom_out_btn.clicked.connect(self.viewer.zoom_out)
        self.fit_btn.clicked.connect(self.viewer.fit)
        self.viewer.zoom_changed.connect(self._zoom_text)
        self.viewer.set_mode(CompareMode.SPLIT)
        self.setAcceptDrops(True)

    # ------------------------------------------------------------- loading
    def browse(self) -> None:
        path = self.ctx.ask_image()
        if path:
            self.load(path)

    def _invalidate_preview(self) -> None:
        self.ctx.jobs.invalidate(f"{self.owner}:preview")

    def load(self, path: str, on_loaded: Callable[[ImageData], None] | None = None) -> int:
        self._invalidate_preview()  # an old render must not paint over the new image
        self.viewer.set_busy(True)
        self.path_label.set_path(path)
        engine = self.ctx.engine

        def work(token, progress):
            data = engine.load_image(path, PREVIEW_MAX_SIDE, token)
            return data, to_qimage(data.array)

        def done(result):
            data, qimage = result
            self.source = data
            self._before_qimage = qimage
            self.viewer.set_images(qimage, None, keep_view=False)
            self.ctx.log.success("log.image.loaded", detail=path, w=data.width, h=data.height)
            self.image_loaded.emit(data)
            if on_loaded:
                on_loaded(data)

        def failed(err: ErrorInfo):
            self.viewer.clear("viewer.load_failed")
            self.ctx.log.error("log.image.failed", detail=f"{path}: {err.summary()} {err.detail}")

        return self.ctx.jobs.submit(
            f"{self.owner}:image", "job.load_image", work, on_result=done, on_error=failed,
            on_cancel=lambda: self.viewer.set_busy(False), title_kwargs={"name": path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]},
        )

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and self.load_button.isVisible():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.load(urls[0])

    # ------------------------------------------------------------- preview
    def render(self, lut: LutData | None) -> int:
        """Show ``lut`` applied to the source via ``images.render_preview``.

        ``lut`` is the exact engine Lut3D (strength already baked) that export
        writes, so preview and export cannot diverge.  Returns the job id.
        """
        if self.source is None:
            self._invalidate_preview()
            return -1
        if lut is None or not lut.previewable:
            # Drop any in-flight render of a previous LUT; show Before only.
            self._invalidate_preview()
            self.viewer.set_images(self._before_qimage, None)
            return -1
        source = self.source
        engine = self.ctx.engine
        self.viewer.set_busy(True)

        def work(token, progress):
            return to_qimage(engine.render_preview(source, lut, token))

        def done(qimage):
            self.viewer.set_images(self._before_qimage, qimage)

        def failed(err: ErrorInfo):
            self.viewer.set_images(self._before_qimage, None)
            self.set_note("preview.failed_note", reason=err.summary(), detail=err.detail)
            self.ctx.log.error("log.preview.failed", detail=f"{err.summary()} {err.detail}".strip())

        return self.ctx.jobs.submit(f"{self.owner}:preview", "job.preview", work, on_result=done, on_error=failed,
                                    on_cancel=lambda: self.viewer.set_busy(False))

    def set_source(self, data: ImageData, qimage) -> None:
        """Use an image decoded elsewhere (worker) as the preview source."""
        self._invalidate_preview()
        self.source = data
        self._before_qimage = qimage
        self.path_label.set_path(data.path)
        self.viewer.set_images(qimage, None, keep_view=False)
        self.image_loaded.emit(data)

    def show_images(self, before, after) -> None:
        """Display already-prepared QImages (e.g. registration check)."""
        self._invalidate_preview()
        self.viewer.set_images(before, after, keep_view=False)
    def set_note(self, key: str, **kwargs) -> None:
        self._note_key, self._note_kwargs = key, kwargs
        self._render_note()

    def _render_note(self) -> None:
        self.note.setText(tr(self._note_key, **self._note_kwargs) if self._note_key else "")
        self.note.setVisible(bool(self._note_key))

    def _sync_mode(self, mode: str) -> None:
        idx = self.mode_combo.findData(mode)
        if idx >= 0 and idx != self.mode_combo.currentIndex():
            self.mode_combo.setCurrentIndex(idx)

    def set_load_text_key(self, key: str) -> None:
        self._load_key = key
        self._retext_load()

    def _retext_load(self) -> None:
        self._load_text = tr(getattr(self, "_load_key", "preview.load_image"))
        T.bind(self.load_button, "setAccessibleName", getattr(self, "_load_key", "preview.load_image"))
        self._apply_compact()

    def _full_toolbar_width(self) -> int:
        fm = self.load_button.fontMetrics()
        width = fm.horizontalAdvance(self._load_text) + 56 if self.load_button.isVisibleTo(self) else 0
        width += fm.horizontalAdvance(self.mode_label.text()) + self.mode_combo.sizeHint().width()
        width += 3 * self.fit_btn.sizeHint().width() + 56 + 6 * SPACE_S
        return width

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), 280))
        return hint

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_compact()

    def _apply_compact(self) -> None:
        compact = self.width() < self._full_toolbar_width()
        # Narrow: the load button becomes icon-only (name kept in tooltip /
        # accessible name) and the zoom readout hides, so nothing clips.
        self.load_button.setText("" if compact else self._load_text)
        self.load_button.setToolTip(self._load_text if compact else "")
        self.zoom_label.setVisible(not compact)
        self.mode_label.setVisible(not compact)

    def _zoom_text(self, zoom: float) -> None:
        self.zoom_label.setText(f"{zoom * 100:.0f}%")


def debounce(parent: QWidget, msec: int, fn: Callable[[], None]) -> QTimer:
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(msec)
    timer.timeout.connect(fn)
    return timer


def array_stats(arr: np.ndarray) -> str:  # pragma: no cover - debugging helper
    return f"min={float(np.min(arr)):.4f} max={float(np.max(arr)):.4f}"




