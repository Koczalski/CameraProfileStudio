"""LUT Compare without Qt: contact sheet pages and cell placement, output file
names, the split before/after image and JPEG publishing that never overwrites.

Drawing text needs a font engine, so the sheets themselves are painted by
:mod:`camera_profile_studio.gui.compare_render`.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Hashable, Iterable

import numpy as np

CELL_SIDES = (320, 480, 720)
DEFAULT_CELL_SIDE = 480
MAX_COLUMNS = 8  # one page, including the Before column
MAX_ROWS = 6
JPEG_QUALITY = 95
JPEG_SUBSAMPLING = 0  # 4:4:4: colour is what is being compared

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_NAME_PART = 100  # two parts, the separator and ".jpg" stay under the 255 limit of file systems
_DIVIDER_BLACK, _DIVIDER_WHITE = 0, 255


@dataclass(frozen=True)
class Page:
    number: int  # 1-based
    photos: tuple[int, ...]  # row indices
    luts: tuple[int, ...]  # LUT indices; the Before column is implied on every page


def paginate(photo_count: int, lut_count: int) -> list[Page]:
    """Split the grid: photos beyond the limit go down, LUTs beyond it go across."""
    per_page = MAX_COLUMNS - 1
    pages: list[Page] = []
    for row in range(0, photo_count, MAX_ROWS):
        photos = tuple(range(row, min(row + MAX_ROWS, photo_count)))
        for column in range(0, lut_count, per_page):
            luts = tuple(range(column, min(column + per_page, lut_count)))
            pages.append(Page(len(pages) + 1, photos, luts))
    return pages


@dataclass(frozen=True)
class SheetMetrics:
    side: int
    margin: int
    gap: int
    label_width: int  # includes the left margin
    header_height: int  # includes the top margin; holds the LUT name and the warning line
    font_px: int

    @classmethod
    def for_side(cls, side: int) -> SheetMetrics:
        font_px = max(14, side // 24)
        margin = gap = font_px // 2 + 4
        return cls(side, margin, gap, margin + side // 2, margin + font_px * 3, font_px)

    def sheet_size(self, page: Page) -> tuple[int, int]:
        columns, rows = len(page.luts) + 1, len(page.photos)
        return (self.label_width + columns * self.side + (columns - 1) * self.gap + self.margin,
                self.header_height + rows * self.side + (rows - 1) * self.gap + self.margin)

    def cell_origin(self, row: int, column: int) -> tuple[int, int]:
        """Top-left corner of a cell; column 0 is the Before column."""
        return (self.label_width + column * (self.side + self.gap),
                self.header_height + row * (self.side + self.gap))


def centered_offset(width: int, height: int, side: int) -> tuple[int, int]:
    """Where a ``width`` x ``height`` picture goes inside a square cell of ``side``."""
    return (side - width) // 2, (side - height) // 2


# ---------------------------------------------------------------- output names


def contact_file_name(number: int) -> str:
    return f'contact-{number:03d}.jpg'


def _safe_part(name: str) -> str:
    return _UNSAFE.sub('_', name).strip('. ')[:_MAX_NAME_PART].strip('. ') or '_'


def comparison_file_name(photo_name: str, lut_name: str) -> str:
    return f'{_safe_part(photo_name)}__{_safe_part(lut_name)}.jpg'


@dataclass(frozen=True)
class ComparisonPlan:
    files: list[tuple[Hashable, str]]  # (key, file name)
    collisions: list[tuple[Hashable, str, Hashable]]  # (key, file name, key of the combination that keeps it)


def plan_comparison_files(combinations: Iterable[tuple[Hashable, str, str]]) -> ComparisonPlan:
    """Name each ``(key, photo name, LUT name)``.  When two would write the same
    file the first keeps it.  Case is ignored, as on the file systems of Windows."""
    files: list[tuple[Hashable, str]] = []
    collisions: list[tuple[Hashable, str, Hashable]] = []
    owners: dict[str, Hashable] = {}
    for key, photo_name, lut_name in combinations:
        file_name = comparison_file_name(photo_name, lut_name)
        folded = file_name.casefold()
        if folded in owners:
            collisions.append((key, file_name, owners[folded]))
        else:
            owners[folded] = key
            files.append((key, file_name))
    return ComparisonPlan(files, collisions)


def existing_files(folder: str | os.PathLike[str], names: Iterable[str]) -> list[str]:
    return [name for name in names if os.path.lexists(Path(folder) / name)]


# ---------------------------------------------------------------- pixels


def to_rgb8(array: Any) -> np.ndarray:
    """Encoded-sRGB float [0, 1] to 8 bit, with the rounding of ``gui.backend.to_qimage``."""
    return (np.clip(np.nan_to_num(np.asarray(array, dtype=np.float32)), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def split_image(before: Any, after: Any) -> np.ndarray:
    """Left half ``before``, right half ``after``, with a line that shows on any photo."""
    left, right = to_rgb8(before), to_rgb8(after)
    if left.shape != right.shape:
        raise ValueError(f'before and after differ in size: {left.shape} and {right.shape}')
    middle = left.shape[1] // 2
    out = np.concatenate((left[:, :middle], right[:, middle:]), axis=1)
    out[:, max(0, middle - 2):middle + 2] = _DIVIDER_BLACK
    out[:, max(0, middle - 1):middle + 1] = _DIVIDER_WHITE
    return out


def write_jpeg_exclusive(rgb8: np.ndarray, destination: str | os.PathLike[str]) -> None:
    """Publish an 8-bit sRGB JPEG, or raise ``FileExistsError`` and leave what is there.

    Written to a temporary in the same folder first, so a failure leaves no partial file."""
    from PIL import Image

    from .lut import _publish_exclusive

    target = Path(destination)
    descriptor, name = tempfile.mkstemp(prefix='.cps-compare-', suffix='.jpg.tmp', dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open('wb') as stream:
            Image.fromarray(rgb8).save(stream, format='JPEG', quality=JPEG_QUALITY, subsampling=JPEG_SUBSAMPLING)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_exclusive(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
