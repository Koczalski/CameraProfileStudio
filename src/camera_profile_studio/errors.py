"""Structured engine failures and cooperative job cancellation."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import JobContext


class StudioError(Exception):
    def __init__(self, message: str, *, code: str = 'engine_error',
                 details: tuple[tuple[str, str], ...] = ()) -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = str(code)
        self.details = tuple((str(k), str(v)) for k, v in details)


class InvalidProfileError(StudioError):
    """Malformed or internally inconsistent profile data."""


class UnsupportedError(StudioError):
    """Valid data or a requested operation is not supported."""


class ValidationError(StudioError):
    """Invalid settings, shapes, numerical values or metadata."""


class DependencyError(StudioError):
    """An optional Python or native runtime is unavailable."""


class ImageError(StudioError):
    """Image decoding or color interpretation failed."""


class ExportError(StudioError):
    """An output artifact could not be safely published."""


class CancelledError(StudioError):
    """Cooperative cancellation; never a successful result."""


def checkpoint(context: JobContext | None, *, stage: str, fraction: float,
               message: str = '') -> None:
    if context is not None and context.cancel is not None and context.cancel.is_cancelled():
        raise CancelledError('Operation cancelled.', code='cancelled')
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValidationError('Progress fraction must be finite and within [0, 1].', code='invalid_progress')
    if context is not None and context.progress is not None:
        from .types import Progress
        context.progress(Progress(stage, fraction, message))
