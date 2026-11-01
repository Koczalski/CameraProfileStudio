"""Background job execution for the GUI.

* All heavy work (profile parsing, LUT generation, image decode/transform,
  matching, batch) runs on a private :class:`QThreadPool`; the GUI thread only
  receives queued signals.
* Each job belongs to a *channel* (e.g. ``"preview"``).  Submitting to a
  channel bumps its *generation*; a result whose generation is no longer
  current is discarded (and the job is shown as "superseded"), so a slow,
  stale preview can never overwrite a newer one.
* Cancellation is cooperative through :class:`CancelToken`, which is passed to
  the work function and forwarded to backend calls that accept it.
* :meth:`JobManager.shutdown` cancels everything and waits for the pool, so
  closing the window never destroys a QObject that a worker still signals.
"""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRunnable,
    QThreadPool,
    Qt,
    Signal,
)

from .i18n import T, tr


class JobCancelled(Exception):
    """Raised inside a work function when its token is cancelled."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    # Some backends call ``token.is_cancelled()``; support both spellings.
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise JobCancelled()


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


FINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.SUPERSEDED}


@dataclass
class ErrorInfo:
    """A failure as shown to the user: translated summary + raw detail."""

    key: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    traceback: str = ""

    def summary(self) -> str:
        return tr(self.key, **self.kwargs)


@dataclass
class JobRecord:
    job_id: int
    channel: str
    generation: int
    title_key: str
    title_kwargs: dict[str, Any]
    token: CancelToken
    state: JobState = JobState.QUEUED
    progress: float | None = None
    message_key: str = ""
    message_kwargs: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None
    side_effects: bool = False
    filtered: bool = True  # results dropped when a newer superseding job exists
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None


class _Signals(QObject):
    started = Signal(int)
    progress = Signal(int, object, str, object)
    succeeded = Signal(int, object)
    failed = Signal(int, object)
    cancelled = Signal(int)


ErrorMapper = Callable[[BaseException], ErrorInfo]


def default_error_mapper(exc: BaseException) -> ErrorInfo:
    return ErrorInfo("error.unexpected", {"type": type(exc).__name__}, str(exc), traceback.format_exc())


def is_cancellation(exc: BaseException) -> bool:
    """True for our JobCancelled and the engine's CancelledError/code."""
    if isinstance(exc, JobCancelled):
        return True
    if type(exc).__name__ == "CancelledError":
        return True
    return getattr(exc, "code", None) == "cancelled"


class _Runnable(QRunnable):
    """Runs one work function and reports exactly one terminal outcome.

    Outcome rules:

    * A cancellation exception (ours or the engine's) -> cancelled.
    * ``side_effects=True`` (export, batch item): the *actual* outcome wins.
      A result returned after a late cancel means the engine already
      committed files, so it is reported as success with its paths; a real
      error (e.g. partial publication) is reported as failure even if the
      token was cancelled meanwhile.
    * ``side_effects=False`` (inspect, LUT build, preview): nothing was
      published, so a cancel requested before the result arrives discards the
      result/error and reports cancelled.  Stale (superseded) results are
      additionally dropped by generation in :class:`JobManager`.
    """

    def __init__(self, record: JobRecord, fn: Callable[..., Any], signals: _Signals, mapper: ErrorMapper,
                 side_effects: bool) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._record = record
        self._fn = fn
        self._signals = signals
        self._mapper = mapper
        self._side_effects = side_effects

    def run(self) -> None:  # noqa: D401 - Qt override
        rec, sig = self._record, self._signals
        if rec.token.cancelled:
            sig.cancelled.emit(rec.job_id)
            return
        sig.started.emit(rec.job_id)

        def progress(fraction: float | None = None, key: str = "", **kwargs: Any) -> None:
            if not self._side_effects:
                rec.token.raise_if_cancelled()
            sig.progress.emit(rec.job_id, fraction, key, kwargs)

        try:
            result = self._fn(rec.token, progress)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the user
            if is_cancellation(exc) or (rec.token.cancelled and not self._side_effects):
                sig.cancelled.emit(rec.job_id)
                return
            try:
                info = self._mapper(exc)
            except Exception:  # pragma: no cover - mapper must never kill a worker
                info = default_error_mapper(exc)
            if not info.traceback:
                info.traceback = traceback.format_exc()
            sig.failed.emit(rec.job_id, info)
        else:
            if rec.token.cancelled and not self._side_effects:
                sig.cancelled.emit(rec.job_id)
            else:
                sig.succeeded.emit(rec.job_id, result)

@dataclass
class _Callbacks:
    on_result: Callable[[Any], None] | None
    on_error: Callable[[ErrorInfo], None] | None
    on_cancel: Callable[[], None] | None
    on_progress: Callable[[float | None, str, dict], None] | None
    on_done: Callable[[JobState], None] | None


class JobManager(QObject):
    """Submits jobs, tracks state for the Jobs table and drops stale results."""

    job_changed = Signal(int)
    job_finished = Signal(int, str)  # job id, final JobState value
    busy_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None, max_threads: int | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        if max_threads is not None:
            self.pool.setMaxThreadCount(max_threads)
        else:
            self.pool.setMaxThreadCount(max(2, min(8, QThreadPool.globalInstance().maxThreadCount() - 1)))
        self._ids = itertools.count(1)
        self._generations: dict[str, int] = {}
        self.records: dict[int, JobRecord] = {}
        self._callbacks: dict[int, _Callbacks] = {}
        self._signals: dict[int, _Signals] = {}
        self.error_mapper: ErrorMapper = default_error_mapper
        self._shutting_down = False
        self.model = JobsModel(self)

    # -------------------------------------------------------------- submission
    def submit(
        self,
        channel: str,
        title_key: str,
        fn: Callable[[CancelToken, Callable[..., None]], Any],
        *,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[ErrorInfo], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        on_progress: Callable[[float | None, str, dict], None] | None = None,
        on_done: Callable[[JobState], None] | None = None,
        supersede: bool = True,
        side_effects: bool = False,
        title_kwargs: dict[str, Any] | None = None,
    ) -> int:
        """Run ``fn(token, progress)`` on the pool.

        With ``supersede`` (default) any running job on the same channel is
        cancelled and its eventual result ignored.  ``side_effects`` marks
        jobs that publish files: they are never superseded and always report
        their actual terminal outcome (see :class:`_Runnable`).
        """
        if side_effects:
            supersede = False
        if self._shutting_down:
            return -1
        if supersede:
            for rec in self.active(channel):
                if rec.side_effects or not rec.filtered:
                    continue  # never supersede publishing / independent jobs
                rec.token.cancel()
                self._set_state(rec, JobState.SUPERSEDED)
        # Only superseding submissions advance the channel generation; jobs
        # submitted with supersede=False are never filtered as stale.
        if supersede:
            generation = self._generations.get(channel, 0) + 1
            self._generations[channel] = generation
        else:
            generation = self._generations.get(channel, 0)
        job_id = next(self._ids)
        rec = JobRecord(job_id, channel, generation, title_key, title_kwargs or {}, CancelToken(), side_effects=side_effects,
                        filtered=supersede)
        self.records[job_id] = rec
        self._callbacks[job_id] = _Callbacks(on_result, on_error, on_cancel, on_progress, on_done)
        signals = _Signals()
        signals.started.connect(self._on_started)
        signals.progress.connect(self._on_progress)
        signals.succeeded.connect(self._on_succeeded)
        signals.failed.connect(self._on_failed)
        signals.cancelled.connect(self._on_cancelled)
        self._signals[job_id] = signals
        self.model.add(job_id)
        self.pool.start(_Runnable(rec, fn, signals, self.error_mapper, side_effects))
        self.busy_changed.emit(True)
        return job_id

    def invalidate(self, channel: str) -> None:
        """Make every pending result on ``channel`` stale without submitting.

        Used when the request the channel was serving no longer applies
        (e.g. preview switched to an unsupported LUT or a new image), so a
        slow old job cannot paint over the new state.
        """
        self._generations[channel] = self._generations.get(channel, 0) + 1
        for rec in self.active(channel):
            if rec.side_effects or not rec.filtered:
                continue
            rec.token.cancel()
            self._set_state(rec, JobState.SUPERSEDED)

    def generation(self, channel: str) -> int:
        return self._generations.get(channel, 0)

    def is_current(self, job_id: int) -> bool:
        rec = self.records.get(job_id)
        if rec is None:
            return False
        if not rec.filtered:
            return True
        return rec.generation == self._generations.get(rec.channel, 0)

    def active(self, channel: str | None = None) -> list[JobRecord]:
        return [
            r
            for r in self.records.values()
            if r.state not in FINAL_STATES and (channel is None or r.channel == channel)
        ]

    def is_busy(self, channel: str | None = None) -> bool:
        return bool(self.active(channel))

    def cancel(self, job_id: int) -> None:
        rec = self.records.get(job_id)
        if rec and rec.state not in FINAL_STATES:
            rec.token.cancel()
            rec.message_key = "job.cancelling"
            self.model.refresh(job_id)

    def cancel_channel(self, channel: str) -> None:
        for rec in self.active(channel):
            self.cancel(rec.job_id)

    def cancel_all(self) -> None:
        for rec in self.active():
            self.cancel(rec.job_id)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Cancel everything and wait for worker threads (safe teardown).

        Returns False if workers are still running after ``timeout_ms``; the
        caller must then keep this manager (and its signal objects) alive and
        retry, never destroy it under a running worker.
        """
        self._shutting_down = True
        self.pool.clear()  # drop queued, not-yet-started runnables
        for rec in self.active():
            rec.token.cancel()
        return self.pool.waitForDone(timeout_ms)

    def is_idle(self) -> bool:
        return self.pool.activeThreadCount() == 0

    # ----------------------------------------------------------------- slots
    def _rec(self, job_id: int) -> JobRecord | None:
        return self.records.get(job_id)

    def _on_started(self, job_id: int) -> None:
        rec = self._rec(job_id)
        if rec and rec.state is JobState.QUEUED:
            rec.state = JobState.RUNNING
            rec.started = time.monotonic()
            self.model.refresh(job_id)
            self.job_changed.emit(job_id)

    def _on_progress(self, job_id: int, fraction: Any, key: str, kwargs: Any) -> None:
        rec = self._rec(job_id)
        if not rec or rec.state in FINAL_STATES:
            return
        rec.progress = fraction
        if key:
            rec.message_key, rec.message_kwargs = key, dict(kwargs or {})
        self.model.refresh(job_id)
        cb = self._callbacks.get(job_id)
        if cb and cb.on_progress and self.is_current(job_id):
            cb.on_progress(fraction, key, dict(kwargs or {}))
        self.job_changed.emit(job_id)

    def _finish(self, job_id: int, state: JobState, call: Callable[[_Callbacks], None] | None) -> None:
        rec = self._rec(job_id)
        if rec is None:
            return
        if rec.side_effects:
            # Committed work is never hidden behind "superseded".
            current, final = True, state
        else:
            current = self.is_current(job_id) and rec.state is not JobState.SUPERSEDED
            final = state if current else JobState.SUPERSEDED
        self._set_state(rec, final)
        cb = self._callbacks.pop(job_id, None)
        if cb and current and not self._shutting_down:
            if call:
                call(cb)
            if cb.on_done:
                cb.on_done(final)
        sig = self._signals.pop(job_id, None)
        if sig is not None:
            sig.deleteLater()
        self.job_finished.emit(job_id, final.value)
        if not self.active():
            self.busy_changed.emit(False)

    def _set_state(self, rec: JobRecord, state: JobState) -> None:
        rec.state = state
        if state in FINAL_STATES and rec.ended is None:
            rec.ended = time.monotonic()
        self.model.refresh(rec.job_id)
        self.job_changed.emit(rec.job_id)

    def _on_succeeded(self, job_id: int, result: Any) -> None:
        rec = self._rec(job_id)
        if rec:
            rec.progress = 1.0
        self._finish(job_id, JobState.SUCCEEDED, lambda cb: cb.on_result and cb.on_result(result))

    def _on_failed(self, job_id: int, info: ErrorInfo) -> None:
        rec = self._rec(job_id)
        if rec:
            rec.error = info
        self._finish(job_id, JobState.FAILED, lambda cb: cb.on_error and cb.on_error(info))

    def _on_cancelled(self, job_id: int) -> None:
        self._finish(job_id, JobState.CANCELLED, lambda cb: cb.on_cancel and cb.on_cancel())


class JobsModel(QAbstractTableModel):
    """Model/View table of jobs for the bottom panel."""

    COLUMNS = ("jobs.col.task", "jobs.col.state", "jobs.col.progress", "jobs.col.detail")
    MAX_ROWS = 300

    def __init__(self, manager: JobManager) -> None:
        super().__init__(manager)
        self._manager = manager
        self._rows: list[int] = []
        T.watch(self, lambda: self._retranslate(""))

    def _retranslate(self, _lang: str) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.COLUMNS) - 1)
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, len(self.COLUMNS) - 1))

    def add(self, job_id: int) -> None:
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._rows.insert(0, job_id)
        self.endInsertRows()
        if len(self._rows) > self.MAX_ROWS:
            # Drop the oldest finished rows.
            for idx in range(len(self._rows) - 1, -1, -1):
                rec = self._manager.records.get(self._rows[idx])
                if rec and rec.state in FINAL_STATES:
                    self.beginRemoveRows(QModelIndex(), idx, idx)
                    del self._rows[idx]
                    self.endRemoveRows()
                    if len(self._rows) <= self.MAX_ROWS:
                        break

    def refresh(self, job_id: int) -> None:
        try:
            row = self._rows.index(job_id)
        except ValueError:
            return
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.COLUMNS) - 1))

    def job_at(self, row: int) -> JobRecord | None:
        if 0 <= row < len(self._rows):
            return self._manager.records.get(self._rows[row])
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(self.COLUMNS[section])
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        rec = self.job_at(index.row())
        if rec is None:
            return None
        col = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if col == 0:
                return tr(rec.title_key, **rec.title_kwargs)
            if col == 1:
                return tr(f"job.state.{rec.state.value}")
            if col == 2:
                if rec.progress is None:
                    return "" if rec.state in FINAL_STATES else tr("job.indeterminate")
                return f"{rec.progress * 100:.0f}%"
            if col == 3:
                if rec.error is not None:
                    text = rec.error.summary()
                    if rec.error.detail:
                        text += f" \u2014 {rec.error.detail}"
                    return text
                if rec.message_key:
                    return tr(rec.message_key, **rec.message_kwargs)
                return ""
        if role == Qt.ItemDataRole.UserRole:
            return rec.job_id
        return None




