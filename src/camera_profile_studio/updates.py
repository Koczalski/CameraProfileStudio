"""In-app update support: release check, download and integrity check.

Engine-free and Qt-free, so it can be tested without a display and reused by
a CLI.  Nothing here raises to the caller for expected failures: a check
returns an :class:`UpdateCheck` whose ``error`` is a stable code, and the
download and verification helpers raise :class:`UpdateError` with a code the
GUI translates.

Privacy: the only request made for a check is a plain ``GET`` of the public
GitHub Releases list with a ``User-Agent: CameraProfileStudio/<version>``
header.  No identifier, path or setting is sent.
"""

from __future__ import annotations

import functools
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import __version__

REPOSITORY = "Koczalski/CameraProfileStudio"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=20"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"
CHECKSUM_ASSET = "DELIVERY_SHA256SUMS.txt"
SETUP_SUFFIX = "-Setup.exe"
PORTABLE_SUFFIX = "-Portable.zip"

#: Named mutex of the running application; ``AppMutex`` in the Inno Setup script.
APP_MUTEX = "CameraProfileStudio_SingleInstance"
#: HKCU uninstall key written by Inno Setup (AppId in packaging/CameraProfileStudio.iss, "_is1" suffix).
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{B15FF861-C98A-4F3F-B46D-143C62935FA3}_is1"

AUTO_CHECK_INTERVAL = 24 * 3600  # seconds between automatic checks
MAX_API_BYTES = 4_000_000
MAX_NOTES_CHARS = 20_000
CHECKSUM_FILE_LIMIT = 1_000_000

# ---- error codes (translated in the GUI as update.error.<code>) -------------
OFFLINE = "offline"
NOT_FOUND = "not_found"            # HTTP 404: repository missing or not public
RATE_LIMITED = "rate_limited"      # HTTP 403 / 429
HTTP_ERROR = "http_error"
BAD_RESPONSE = "bad_response"
UNEXPECTED = "unexpected"
INSECURE_URL = "insecure_url"
SIZE_MISMATCH = "size_mismatch"
DISK = "disk"
CANCELLED = "cancelled"            # also what the job manager treats as a cancellation
CHECKSUM_MISSING = "checksum_missing"
CHECKSUM_MISMATCH = "checksum_mismatch"


class UpdateError(Exception):
    """A failed update step with a stable ``code`` (``detail`` is raw text)."""

    def __init__(self, code: str, detail: str = "", status: int = 0) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.status = status


class DownloadCancelled(UpdateError):
    def __init__(self) -> None:
        super().__init__(CANCELLED)


class ChecksumError(UpdateError):
    """The installer is missing from the sums file or its SHA-256 differs."""


# ---- versions ----------------------------------------------------------------
_SEMVER = re.compile(
    r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$"
)


@functools.total_ordering
@dataclass(frozen=True)
class Version:
    """Semantic version with SemVer 2.0 precedence (build metadata ignored).

    ``0.2.6`` > ``0.2.6-beta.2`` > ``0.2.6-beta`` > ``0.2.5``; a leading ``v`` is accepted.
    """

    major: int
    minor: int = 0
    patch: int = 0
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = _SEMVER.match(str(text).strip())
        if match is None:
            raise ValueError(f"not a version: {text!r}")
        major, minor, patch, pre = match.groups()
        return cls(int(major), int(minor or 0), int(patch or 0), tuple(pre.split(".")) if pre else ())

    @classmethod
    def try_parse(cls, text: Any) -> "Version | None":
        try:
            return cls.parse(text)
        except (ValueError, TypeError):
            return None

    def _key(self) -> tuple:
        ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.prerelease)
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, ids)

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}-{'.'.join(self.prerelease)}" if self.prerelease else core


def compare_versions(a: str, b: str) -> int:
    """-1, 0 or 1 by SemVer precedence (raises ValueError for non-versions)."""
    va, vb = Version.parse(a), Version.parse(b)
    return (va > vb) - (va < vb)


def default_include_prereleases(current: str = __version__) -> bool:
    """Pre-releases are offered by default while the major version is 0."""
    parsed = Version.try_parse(current)
    return parsed is None or parsed.major == 0


def auto_check_due(last_check: float | int | None, now: float | None = None, interval: float = AUTO_CHECK_INTERVAL) -> bool:
    """True when no check is recorded, ``interval`` has passed, or the clock went backwards."""
    if not last_check:
        return True
    now = time.time() if now is None else now
    return not (0 <= now - float(last_check) < interval)


# ---- releases ------------------------------------------------------------------
@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int = 0


@dataclass(frozen=True)
class ReleaseInfo:
    version: str          # normalised, e.g. "0.2.7-beta"
    tag: str
    title: str
    notes: str
    html_url: str
    published_at: str     # ISO 8601 as sent by GitHub
    prerelease: bool = False
    assets: tuple[ReleaseAsset, ...] = ()

    def find_asset(self, suffix: str) -> ReleaseAsset | None:
        low = suffix.lower()
        return next((a for a in self.assets if a.name.lower().endswith(low)), None)

    @property
    def setup_asset(self) -> ReleaseAsset | None:
        return self.find_asset(SETUP_SUFFIX)

    @property
    def checksum_asset(self) -> ReleaseAsset | None:
        return next((a for a in self.assets if a.name.lower() == CHECKSUM_ASSET.lower()), None)

    @property
    def published_date(self) -> str:
        return self.published_at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", self.published_at) else ""


@dataclass(frozen=True)
class UpdateCheck:
    """Outcome of :func:`check_for_update`: a newer ``release``, none, or an ``error`` code."""

    current: str
    release: ReleaseInfo | None = None
    error: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def update_available(self) -> bool:
        return self.release is not None


def is_trusted_url(url: str) -> bool:
    """https on github.com or githubusercontent.com (release pages, assets and their CDN)."""
    parts = urlparse(str(url))
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in ("github.com", "githubusercontent.com"))


def _http_code(status: int) -> str:
    if status == 404:
        return NOT_FOUND
    if status in (403, 429):
        return RATE_LIMITED
    return HTTP_ERROR


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to GitHub hosts over https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if not is_trusted_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to an untrusted address", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_default_opener = urllib.request.build_opener(_HttpsRedirects).open


def _open(opener: Callable[..., Any] | None, request: urllib.request.Request, timeout: float):
    try:
        return (opener or _default_opener)(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise UpdateError(_http_code(exc.code), f"HTTP {exc.code}", exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(OFFLINE, str(getattr(exc, "reason", None) or exc)) from exc


def _headers(version: str, **extra: str) -> dict[str, str]:
    return {"User-Agent": f"CameraProfileStudio/{version}", **extra}


def _parse_release(item: Any, include_prereleases: bool) -> tuple[Version, ReleaseInfo] | None:
    if not isinstance(item, dict) or item.get("draft"):
        return None
    tag = item.get("tag_name")
    version = Version.try_parse(tag)
    if version is None:
        return None
    prerelease = bool(item.get("prerelease")) or bool(version.prerelease)
    if prerelease and not include_prereleases:
        return None
    assets = []
    for raw in item.get("assets") or []:
        if isinstance(raw, dict) and isinstance(raw.get("name"), str) and isinstance(raw.get("browser_download_url"), str):
            size = raw.get("size")
            assets.append(ReleaseAsset(raw["name"], raw["browser_download_url"], size if isinstance(size, int) else 0))
    html_url = item.get("html_url")
    notes = item.get("body") if isinstance(item.get("body"), str) else ""
    if len(notes) > MAX_NOTES_CHARS:
        notes = notes[:MAX_NOTES_CHARS].rstrip() + "\n…"
    info = ReleaseInfo(
        version=str(version),
        tag=str(tag),
        title=item.get("name") if isinstance(item.get("name"), str) and item.get("name") else str(tag),
        notes=notes.replace("\r\n", "\n"),
        html_url=html_url if isinstance(html_url, str) and is_trusted_url(html_url) else RELEASES_PAGE,
        published_at=item.get("published_at") if isinstance(item.get("published_at"), str) else "",
        prerelease=prerelease,
        assets=tuple(assets),
    )
    return version, info


def select_release(data: Any, current: str, include_prereleases: bool) -> ReleaseInfo | None:
    """Newest release in the API list that is strictly newer than ``current``."""
    if not isinstance(data, list):
        raise UpdateError(BAD_RESPONSE, "the release list is not a JSON array")
    best_version, best = Version.parse(current), None
    for item in data:
        parsed = _parse_release(item, include_prereleases)
        if parsed is not None and parsed[0] > best_version:
            best_version, best = parsed
    return best


def check_for_update(current: str = __version__, *, include_prereleases: bool = False, timeout: float = 10.0,
                     opener: Callable[..., Any] | None = None, url: str = RELEASES_API) -> UpdateCheck:
    """Ask GitHub Releases for a version newer than ``current``; never raises.

    ``opener(request, timeout=...)`` returns a context-managed response (default: ``urllib``, https-only redirects).
    """
    try:
        request = urllib.request.Request(url, headers=_headers(current, Accept="application/vnd.github+json"), method="GET")
        try:
            with _open(opener, request, timeout) as response:
                body = response.read(MAX_API_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:  # connection dropped while reading
            raise UpdateError(OFFLINE, str(exc)) from exc
        if len(body) > MAX_API_BYTES:
            raise UpdateError(BAD_RESPONSE, "the response is too large")
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
            raise UpdateError(BAD_RESPONSE, str(exc)) from exc
        return UpdateCheck(current, select_release(data, current, include_prereleases))
    except UpdateError as exc:
        return UpdateCheck(current, error=exc.code, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001 - the GUI must never see an exception from a check
        return UpdateCheck(current, error=UNEXPECTED, detail=f"{type(exc).__name__}: {exc}")


# ---- download and verification ---------------------------------------------------
def download_dir() -> Path:
    return Path(tempfile.gettempdir()) / "CameraProfileStudio-update"


def cleanup_downloads(directory: Path | None = None, max_age: float = 7 * 86400, now: float | None = None) -> int:
    """Delete files in the download folder older than ``max_age`` seconds; returns how many."""
    folder, removed, now = directory or download_dir(), 0, time.time() if now is None else now
    try:
        for item in folder.iterdir():
            try:
                if item.is_file() and now - item.stat().st_mtime > max_age:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def fetch_text(url: str, *, timeout: float = 30.0, opener: Callable[..., Any] | None = None, version: str = __version__) -> str:
    """Download a small text file (the sums file)."""
    if not is_trusted_url(url):
        raise UpdateError(INSECURE_URL, url)
    try:
        with _open(opener, urllib.request.Request(url, headers=_headers(version), method="GET"), timeout) as response:
            data = response.read(CHECKSUM_FILE_LIMIT + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise UpdateError(OFFLINE, str(exc)) from exc
    if len(data) > CHECKSUM_FILE_LIMIT:
        raise UpdateError(BAD_RESPONSE, "the file is too large")
    return data.decode("utf-8-sig", errors="replace")


def download_asset(url: str, dest: str | Path, expected_size: int = 0,
                   progress: Callable[[int, int], None] | None = None, cancel: Callable[[], bool] | None = None, *,
                   timeout: float = 30.0, opener: Callable[..., Any] | None = None, version: str = __version__,
                   chunk_size: int = 1 << 20) -> Path:
    """Download ``url`` to ``dest`` through ``dest.part`` and rename it atomically when complete.

    A ``.part`` file left by an interrupted or cancelled download is resumed with an HTTP Range
    request (restarted if the server does not honour it).  ``progress(done, total)`` is called after
    each chunk (``total`` is 0 when unknown); ``cancel()`` is polled per chunk and raises
    :class:`DownloadCancelled`, keeping the partial file.  The size is checked against
    ``expected_size`` when known; the SHA-256 is checked separately with :func:`verify_sha256`.
    """
    if not is_trusted_url(url):
        raise UpdateError(INSECURE_URL, url)
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        if expected_size and have > expected_size:
            part.unlink()
            have = 0
    except OSError as exc:
        raise UpdateError(DISK, str(exc)) from exc
    if cancel and cancel():
        raise DownloadCancelled()
    done, total = have, expected_size
    if not (expected_size and have == expected_size):
        done, total = _transfer(url, part, have, expected_size, progress, cancel, timeout, opener, version, chunk_size)
    if (expected_size and done != expected_size) or (total and done != total):
        if done > (expected_size or total):
            part.unlink(missing_ok=True)
        raise UpdateError(SIZE_MISMATCH, f"{done} of {expected_size or total} bytes")
    try:
        os.replace(part, dest)
    except OSError as exc:
        raise UpdateError(DISK, str(exc)) from exc
    return dest


def _transfer(url, part: Path, have: int, expected: int, progress, cancel, timeout, opener, version, chunk_size) -> tuple[int, int]:
    headers = _headers(version)
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        response = _open(opener, urllib.request.Request(url, headers=headers, method="GET"), timeout)
    except UpdateError as exc:
        if exc.status == 416 and have:  # stale partial file: start over
            part.unlink(missing_ok=True)
            return _transfer(url, part, 0, expected, progress, cancel, timeout, opener, version, chunk_size)
        raise
    with response:
        if have and getattr(response, "status", 200) != 206:
            have = 0  # the server ignored the Range header and sends everything
        length = response.headers.get("Content-Length") if getattr(response, "headers", None) is not None else None
        total = expected or (have + int(length) if length and str(length).isdigit() else 0)
        done = have
        try:
            out = open(part, "ab" if have else "wb")
        except OSError as exc:
            raise UpdateError(DISK, str(exc)) from exc
        with out:
            while True:
                if cancel and cancel():
                    raise DownloadCancelled()
                try:
                    data = response.read(chunk_size)
                except (OSError, http.client.HTTPException) as exc:
                    raise UpdateError(OFFLINE, str(exc)) from exc
                if not data:
                    break
                try:
                    out.write(data)
                except OSError as exc:
                    raise UpdateError(DISK, str(exc)) from exc
                done += len(data)
                if progress:
                    progress(done, total)
            try:
                out.flush()
                os.fsync(out.fileno())
            except OSError as exc:
                raise UpdateError(DISK, str(exc)) from exc
    return done, total


def parse_sha256sums(text: str) -> dict[str, str]:
    """``{lower-case file name: lower-case hex}`` from ``<hash>  <name>`` lines (sha256sum / Get-FileHash style)."""
    sums: dict[str, str] = {}
    for line in text.lstrip("﻿").splitlines():
        match = re.match(r"^\s*([0-9A-Fa-f]{64})\s+\*?(.+?)\s*$", line)
        if match:
            sums[match.group(2).replace("\\", "/").rsplit("/", 1)[-1].lower()] = match.group(1).lower()
    return sums


def file_sha256(path: str | Path, chunk_size: int = 4 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def verify_sha256(path: str | Path, sums_text: str, name: str | None = None) -> str:
    """Return the file's SHA-256 if it matches its entry in ``sums_text``.

    Raises :class:`ChecksumError` (``checksum_missing`` / ``checksum_mismatch``): never install then.
    """
    expected = parse_sha256sums(sums_text).get((name or Path(path).name).lower())
    if expected is None:
        raise ChecksumError(CHECKSUM_MISSING, name or Path(path).name)
    actual = file_sha256(path)
    if actual != expected:
        raise ChecksumError(CHECKSUM_MISMATCH, f"expected {expected}, got {actual}")
    return actual


# ---- how this copy was installed -------------------------------------------------
INSTALLED, PORTABLE, SOURCE = "installed", "portable", "source"


def is_frozen() -> bool:
    """True in a Nuitka (``__compiled__``) or PyInstaller-style (``sys.frozen``) build."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def installed_location() -> str | None:
    """InstallLocation recorded by Inno Setup in HKCU (None off Windows or when not installed)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return str(winreg.QueryValueEx(key, "InstallLocation")[0]) or None
    except OSError:
        return None


def _norm(path: str | Path) -> str:
    return os.path.normpath(str(path)).rstrip("\\/").lower()


def detect_install_mode(executable: str | Path | None = None, *, frozen: bool | None = None,
                        location: Callable[[], str | None] = installed_location) -> str:
    """``installed`` (running from the Inno Setup folder), ``portable`` (any other packaged copy) or ``source``."""
    if not (is_frozen() if frozen is None else frozen):
        return SOURCE
    where = location()
    exe = Path(executable or sys.executable)
    return INSTALLED if where and _norm(exe.parent) == _norm(where) else PORTABLE


INSTALLER_ARGS = ("/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NORESTART", "/CPSRELAUNCH=1")


def launch_installer(path: str | Path, *, popen: Callable[..., Any] = subprocess.Popen) -> None:
    """Start Setup detached, with progress visible (/SILENT) and the app restarted afterwards.

    ``/CPSRELAUNCH=1`` is read by the Inno Setup script, which starts the new version when this
    process has already exited (Restart Manager can only restart applications it closed).
    """
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
    popen([str(path), *INSTALLER_ARGS], close_fds=True, creationflags=flags, cwd=str(Path(path).parent))


_mutex_handle: Any = None


def acquire_app_mutex(name: str = APP_MUTEX) -> bool:
    """Create the named mutex the installer looks for (Windows only; False elsewhere or on failure)."""
    global _mutex_handle
    if sys.platform != "win32" or _mutex_handle:
        return bool(_mutex_handle)
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        _mutex_handle = kernel32.CreateMutexW(None, False, name)
    except Exception:  # noqa: BLE001 - the mutex only helps the installer; never block start-up
        _mutex_handle = None
    return bool(_mutex_handle)


def release_app_mutex() -> None:
    """Close the mutex handle so a freshly started Setup does not wait for this process to exit."""
    global _mutex_handle
    if _mutex_handle and sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(_mutex_handle)
        except Exception:  # noqa: BLE001
            pass
    _mutex_handle = None
