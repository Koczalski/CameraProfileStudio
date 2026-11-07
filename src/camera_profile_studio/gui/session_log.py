"""Session diagnostics for hands-on testing.

Off unless ``CPS_SESSION_LOG`` names a folder (TestRun.bat sets it).  Then one text file per
run collects, line by line and flushed at once (so a crash loses nothing):

* the Log panel entries and every finished job,
* everything written to stdout/stderr, which includes the traceback of an uncaught exception,
* native crashes (``faulthandler`` writes the stacks of all threads into the same file),
* a GUI-thread stall report with the stacks of all threads (heavy work on the GUI thread),
* a heartbeat with memory, thread count and running jobs.

The module imports no Qt at the top, so the parts that need none can be tested without PySide6.
"""

from __future__ import annotations

import faulthandler
import io
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

ENV = "CPS_SESSION_LOG"
STALL_AFTER = 2.0     # seconds without a GUI-thread tick before a stall is reported
TICK_MS = 250         # how often the GUI thread proves it is alive
HEARTBEAT = 30.0      # seconds between heartbeat lines


class StallDetector:
    """The GUI thread calls ``tick``; another thread calls ``check``.  Times are monotonic seconds."""

    def __init__(self, threshold: float = STALL_AFTER) -> None:
        self.threshold = threshold
        self._last = time.monotonic()
        self._reported = False

    def tick(self, now: float) -> float | None:
        """Record a sign of life.  Returns the length of the stall that just ended, if one was reported."""
        away = now - self._last if self._reported else None
        self._last = now
        self._reported = False
        return away

    def check(self, now: float) -> float | None:
        """The seconds the GUI thread has been silent, the first time that exceeds the threshold."""
        silent = now - self._last
        if silent > self.threshold and not self._reported:
            self._reported = True
            return silent
        return None


class _Tee(io.TextIOBase):
    """Passes writes to the console stream (if there is one) and to the session file."""

    def __init__(self, original: TextIO | None, session: SessionLog, name: str) -> None:
        self._original, self._session, self._name = original, session, name

    def write(self, text: str) -> int:
        if self._original is not None:
            self._original.write(text)
        if text.strip():
            self._session.write(text.rstrip("\r\n"), self._name)
        return len(text)

    def flush(self) -> None:
        if self._original is not None:
            self._original.flush()

    def isatty(self) -> bool:
        return bool(self._original is not None and self._original.isatty())

    def fileno(self) -> int:
        if self._original is None:
            raise io.UnsupportedOperation("no console stream")
        return self._original.fileno()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._original, "encoding", None) or "utf-8"


class SessionLog:
    def __init__(self, folder: Path, stamp: str | None = None) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"session-{stamp or time.strftime('%Y%m%d-%H%M%S')}.log"
        self._file: TextIO | None = self.path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._saved: list[tuple[str, Any]] = []

    # ------------------------------------------------------------ writing
    def write(self, text: str, level: str = "INFO") -> None:
        stamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        with self._lock:
            if self._file is not None:
                for line in str(text).splitlines() or [""]:
                    self._file.write(f"{stamp} {level:<6} {line}\n")
                self._file.flush()

    def write_header(self) -> None:
        from .. import __version__

        self.write(f"Camera Profile Studio {__version__}", "START")
        self.write(f"Python {platform.python_version()} on {platform.platform()}", "START")
        for name in ("PySide6", "numpy", "scipy"):
            module = sys.modules.get(name)
            if module is not None:
                self.write(f"{name} {getattr(module, '__version__', '?')}", "START")
        self.write(f"CPS_LCMS2_DLL={os.environ.get('CPS_LCMS2_DLL', '(not set)')}", "START")
        self.write(f"cwd={os.getcwd()} argv={sys.argv}", "START")

    def dump_threads(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                faulthandler.dump_traceback(file=self._file, all_threads=True)

    # ------------------------------------------------------- global hooks
    def enable_crash_dump(self) -> None:
        """Native crashes (Qt, OpenCV, LittleCMS) write the stacks of all threads into this file."""
        if self._file is not None:
            faulthandler.enable(file=self._file, all_threads=True)

    def tee_console(self) -> None:
        for name in ("stdout", "stderr"):
            self._saved.append((name, getattr(sys, name)))
            setattr(sys, name, _Tee(getattr(sys, name), self, name))

    def close(self) -> None:
        if self._file is None:
            return
        self._stop.set()
        self.write("session end (closed normally)", "END")
        for name, original in self._saved:
            setattr(sys, name, original)
        self._saved.clear()
        with self._lock:
            file, self._file = self._file, None
        try:
            faulthandler.disable()
        finally:
            file.close()

    # ----------------------------------------------------------- watching
    def watch(self, window: Any) -> None:
        """Log panel, jobs, GUI-thread stalls and the heartbeat of a running main window."""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        ctx = window.ctx
        model = ctx.log_model
        model.rowsInserted.connect(lambda _p, first, last: self._log_entries(model.entries[first:last + 1]))
        ctx.jobs.job_finished.connect(self._job_finished(ctx.jobs))

        detector = StallDetector()
        timer = QTimer(window)
        timer.setInterval(TICK_MS)

        def alive() -> None:
            away = detector.tick(time.monotonic())
            if away is not None:
                self.write(f"GUI thread responding again after {away:.1f} s", "STALL")

        timer.timeout.connect(alive)
        timer.start()

        def watchdog() -> None:
            last_beat = time.monotonic()
            while not self._stop.wait(1.0):
                now = time.monotonic()
                silent = detector.check(now)
                if silent is not None:
                    self.write(f"GUI thread has not responded for {silent:.1f} s; stacks of all threads follow", "STALL")
                    self.dump_threads()
                if now - last_beat >= HEARTBEAT:
                    last_beat = now
                    self._heartbeat(ctx.jobs)

        threading.Thread(target=watchdog, name="session-watchdog", daemon=True).start()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    def _log_entries(self, entries: list) -> None:
        for entry in entries:
            self.write(entry.render(), {"error": "ERROR", "warning": "WARN"}.get(entry.level, "LOG"))

    def _job_finished(self, jobs: Any):
        def finished(job_id: int, state: str) -> None:
            rec = jobs.records.get(job_id)
            if rec is None:
                self.write(f"job {job_id} {state}", "JOB")
                return
            seconds = (rec.ended or time.monotonic()) - rec.started
            self.write(f"job {job_id} {rec.channel} {rec.title_key} {state} after {seconds:.1f} s", "JOB")
            if rec.error is not None:
                self.write(f"{rec.error.summary()} {rec.error.detail}".strip(), "ERROR")
                if rec.error.traceback:
                    self.write(rec.error.traceback, "ERROR")

        return finished

    def _heartbeat(self, jobs: Any) -> None:
        running = [f"{r.job_id}:{r.channel}" for r in list(jobs.active())]
        memory = _working_set_mb()
        self.write(f"memory {memory:.0f} MB, threads {threading.active_count()}, running jobs {running or 'none'}"
                   if memory is not None else
                   f"threads {threading.active_count()}, running jobs {running or 'none'}", "BEAT")


def _working_set_mb() -> float | None:
    """Resident memory of this process (Windows); None elsewhere."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return counters.WorkingSetSize / (1024 * 1024)


def start(window: Any, folder: Path, *, crash_dump: bool = True, tee: bool = True) -> SessionLog:
    session = SessionLog(folder)
    session.write_header()
    if crash_dump:
        session.enable_crash_dump()
    if tee:
        session.tee_console()
    session.watch(window)
    return session


def start_from_env(window: Any) -> SessionLog | None:
    folder = os.environ.get(ENV)
    return start(window, Path(folder)) if folder else None
