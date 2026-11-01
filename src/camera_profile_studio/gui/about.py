"""Version and licence information shown in the GUI (Settings > About and
Help > Licences).

Licence texts are read from the ``licenses`` folder that the build copies next
to the executable (``launcher.dist/licenses``, also the installed folder) or,
when running from source, from the repository's ``licenses`` folder.  Nothing
is downloaded and nothing is invented: a component appears only when its text
is present.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import qVersion

from .. import __version__ as APP_VERSION

APP_NAME = "Camera Profile Studio"
#: SPDX identifier of the application's own licence (kept equal to pyproject.toml; tested).
APP_LICENSE = "MIT"
#: The licence text: copied into licenses/ by packaging/collect_licenses.py; the
#: repository-root LICENSE is used when running from source.
APP_LICENSE_FILE = "APP-LICENSE.txt"
#: Licence identifiers read from the bundled texts of the pinned packages whose
#: metadata carries no identifier (empty or the full text).  Used only while the
#: recorded version matches, so an upgrade falls back to "See text".
#: (name, version) -> (SPDX expression, has further bundled notices)
KNOWN_LICENSES = {
    ("colorama", "0.4.6"): ("BSD-3-Clause", False),
    ("numpy", "2.2.6"): ("BSD-3-Clause", True),
    ("scipy", "1.15.3"): ("BSD-3-Clause", True),
    ("rawpy", "0.25.1"): ("MIT AND LGPL-2.1 (LibRaw)", False),
    ("wheel", "0.45.1"): ("MIT", False),
    ("zstandard", "0.23.0"): ("BSD-3-Clause", False),
    ("opencv-python-headless", "4.11.0.86"): ("Apache-2.0", True),
    ("Nuitka", "2.7.12"): ("Apache-2.0", True),
    ("Inno Setup", ""): ("Inno Setup License", False),
}
MAX_TEXT_BYTES = 2_000_000


@dataclass
class LicenseEntry:
    name: str
    version: str
    license: str  # short identifier; "" when only the full text says it
    files: list[Path] = field(default_factory=list)
    kind: str = "library"  # app | notice | library | installer
    bundled: bool = False  # the text also covers further bundled components


def licenses_dir() -> Path | None:
    """The licence folder of this installation, or None if it is missing."""
    here = Path(__file__).resolve()
    candidates = (
        Path(sys.executable).resolve().parent / "licenses",  # standalone / installed build
        here.parents[2] / "licenses",  # compiled package next to licenses/
        here.parents[3] / "licenses",  # source checkout: <root>/src/camera_profile_studio/gui
    )
    for folder in candidates:
        if (folder / "manifest.json").is_file():
            return folder
    return None


_SPDX = {"Apache License, Version 2.0": "Apache-2.0", "Apache 2.0": "Apache-2.0"}  # spelling only, no guessing


def _short(text: str) -> str:
    text = (text or "").strip()
    if not text or "\n" in text or len(text) > 60:
        return ""
    return _SPDX.get(text, text)


def _lcms_version(base: Path) -> str:
    try:
        data = json.loads((base / "LittleCMS" / "SOURCE.json").read_text(encoding="utf-8-sig"))
        return str(data.get("upstream", {}).get("version", ""))
    except (OSError, ValueError):
        return ""


def license_entries(base: Path | None = None) -> list[LicenseEntry]:
    """Application first, then distribution notices, runtime parts, recorded packages."""
    base = licenses_dir() if base is None else base
    app_files = []
    if base is not None:
        app_files = [f for f in (base / APP_LICENSE_FILE, base.parent / "LICENSE") if f.is_file()][:1]
    entries = [LicenseEntry(APP_NAME, APP_VERSION, APP_LICENSE, app_files, "app")]
    if base is None:
        return entries

    def add(name: str, version: str, lic: str, files, kind: str = "library", keep_without_files: bool = False) -> None:
        files = [f for f in files if f.is_file()]
        known = KNOWN_LICENSES.get((name, version))
        bundled = False
        if known is not None:
            lic, bundled = known
        if files or keep_without_files:
            entries.append(LicenseEntry(name, version, lic, files, kind, bundled))

    icons = Path(__file__).resolve().parent / "icons"
    add("DISTRIBUTION-NOTICES", "", "", [base / "DISTRIBUTION-NOTICES.md"], "notice")
    # LGPL first: it is the licence the application uses Qt under (GPL texts follow).
    add("Qt", qVersion(), "LGPL-3.0-only", sorted((base / "Qt").glob("*.txt"), key=lambda p: (not p.name.startswith("LGPL"), p.name)))
    add("Python", platform.python_version(), "PSF-2.0", [base / "Python-LICENSE.txt"])
    add("LittleCMS", _lcms_version(base), "MIT", [base / "LittleCMS" / "LICENSE"])
    add("Fluent UI System Icons", "", "MIT", [icons / "LICENSE", icons / "NOTICE.txt"])
    add("Inno Setup", "", "", [base / "Inno-Setup-License.txt"], "installer")
    try:
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        manifest = []
    for item in sorted(manifest, key=lambda m: str(m.get("name", "")).lower()):
        # Every recorded package is listed, also one whose wheel ships no licence file.
        add(str(item.get("name", "")), str(item.get("version", "")), _short(str(item.get("license", ""))),
            [base / f for f in item.get("notice_files", [])], keep_without_files=True)
    return entries


def read_text(entry: LicenseEntry, base: Path | None = None) -> str:
    """All licence files of ``entry`` as text (headed by their paths when several)."""
    parts = []
    for f in entry.files:
        try:
            data = f.read_bytes()[:MAX_TEXT_BYTES]
        except OSError as exc:
            parts.append(f"[{f}: {exc}]")
            continue
        text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
        if len(entry.files) > 1:
            try:
                label = f.relative_to(base).as_posix() if base else f.name
            except ValueError:
                label = f.name
            text = f"──── {label} ────\n\n{text}"
        parts.append(text.rstrip())
    return "\n\n\n".join(parts)
