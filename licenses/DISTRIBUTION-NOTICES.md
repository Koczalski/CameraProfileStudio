# Distribution notices

Camera Profile Studio is distributed as an unsigned Windows x64 standalone
application (portable ZIP and Setup.exe). Windows SmartScreen may warn about the
unsigned executable. No external Python installation, pip, compiler, or Qt SDK
is needed at runtime. Build tools are not shipped inside the application.

Camera Profile Studio itself is licensed under the MIT License (LICENSE in the
source, APP-LICENSE.txt in this folder). Third-party components keep their own
licenses, listed below and in manifest.json; the MIT License does not change them.

No third-party camera profiles are bundled. Users must supply profiles they are
entitled to use. The packaging gate `packaging/verify_no_camera_inputs.py`
rejects a payload that contains one.

## How the license obligations are met

Publisher review: 2026-11-01 (recorded in `packaging/upstream-sources.json`).
This is the publisher's own assessment of the conditions below; it is not legal
advice.

| Component | License | What the payload does |
|---|---|---|
| Qt 6.9.1 (QtCore, QtGui, QtWidgets, QtNetwork, QtSvg, image format, platform, style and TLS plugins) | LGPL-3.0-only | Separate, replaceable DLLs; license texts in `Qt/`; official source archives in `third-party-source/` |
| PySide6 / Shiboken6 6.9.1 | LGPL-3.0-only | Separate, replaceable modules; source archive in `third-party-source/` |
| LibRaw 0.21.4 (`raw_r.dll`, via rawpy) | LGPL-2.1 (CDDL alternative) | Separate, replaceable DLL; exact source commits in `third-party-source/` |
| rawpy 0.25.1 | MIT | Notice in `third-party/` |
| LittleCMS 2.17 (`lcms2.dll`) | MIT | Notice in `LittleCMS/`; source archive in `third-party-source/` |
| NumPy, SciPy (with OpenBLAS), colour-science, tifffile | BSD | Notices in `third-party/` |
| Pillow (with its bundled image libraries) | MIT-CMU (HPND) | Notices in `third-party/` |
| OpenCV (opencv-python-headless) | Apache-2.0 | Notices in `third-party/`; the FFmpeg videoio plugin is excluded |
| Microsoft Fluent UI System Icons | MIT | Notice next to the icons |
| CPython 3.12 (incl. OpenSSL 3, libffi, zlib, bzip2, xz) | PSF-2.0 and component licenses | `Python-LICENSE.txt` |
| Microsoft Visual C++ runtime (`vcruntime140*.dll`, `msvcp140*.dll`) | Microsoft redistributable files | Shipped unmodified, as distributed with CPython and the wheels |

### Qt / PySide6 / Shiboken6 (LGPL-3.0)

- The Qt libraries and plugins stay separate dynamic DLLs in the standalone
  folder. They are not statically linked or hidden in a one-file archive. You may
  replace them with ABI-compatible modified versions; keep the directory names
  and DLL/plugin names compatible, and keep the CPython 3.12 x64 extension
  interface for a PySide/Shiboken replacement.
- The application imposes no restriction on reverse engineering for debugging
  such modifications (the application is MIT licensed).
- The binaries come unmodified from the official PySide6 6.9.1 wheels.
- Corresponding source accompanies every portable and Setup payload in
  `third-party-source/`: the PySide/Shiboken 6.9.1 source archive and the official
  Qt 6.9.1 module archives qtbase, qtsvg, qtimageformats, qtshadertools and
  qttranslations, with SHA-256 values in `manifest.json`. The same archives are
  available upstream:
  - https://download.qt.io/archive/qt/6.9/6.9.1/submodules/
  - https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.9.1-src/
  - https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.9.1
- The build fails if a shipped Qt DLL or plugin cannot be mapped to one of these
  archives (`packaging/verify_qt_sources.py`). Qt6Pdf (from qtwebengine) is not
  used by the application and is excluded from the payload
  (`scripts/build-windows.ps1`, `packaging/audit_native.py`).
- GPL-3.0 texts and the Qt GPL exception are included because some Qt build
  tools in the source archives use them; no GPL-only Qt module is shipped.

### rawpy / LibRaw (LGPL-2.1)

rawpy 0.25.1 is MIT licensed. Its `_rawpy` extension dynamically imports
`raw_r.dll` (LibRaw 0.21.4), which remains separately replaceable
(`packaging/audit_native.py` fails the build otherwise). The exact LibRaw and
LibRaw-cmake source commits from the rawpy release submodules, the vcpkg build
reference, and verbatim license files are included. Optional GPL demosaic packs
and RawSpeed are disabled in the wheel. The LibRaw-cmake build scripts have their
own notices; they do not change the LGPL choice for the LibRaw runtime.

The upstream Windows build uses vcpkg 2025.01.13 and statically links zlib 1.3.1,
libjpeg-turbo 3.1.0, JasPer 4.2.4 and LittleCMS 2.16 into raw_r.dll (all
permissive licenses; texts under `upstream-source/`). This internal LittleCMS is
distinct from the application's separately built LittleCMS 2.17 DLL.

### Other libraries

The generated manifest.json records exact installed versions, upstream project
URLs and verbatim license files. Build/test-only packages can appear in the
manifest but are not part of the standalone payload. The unused OpenCV videoio
FFmpeg plugin is excluded; an isolated copy without that DLL passed PNG/JPEG/TIFF
encode/decode and ORB tests, and the native inventory gate rejects an
accidentally included FFmpeg binary.

## Packaging tooling

Nuitka 2.7.12 is Apache-2.0 licensed. Inno Setup 6.0.5 is acquired from the
official jrsoftware release and extracted locally with innoextract 1.9 (zlib
license); neither installer acquisition nor compilation installs software on
the host. Inno Setup's license is shipped with these notices. The older compiler
is deliberately pinned because innoextract 1.9 cannot extract the current Inno
Setup format.

## Checklist for publishing a release

1. Build with `packaging/windows-launcher/Build-0.2.5.cmd` (fresh-source route).
   The build stops if the Qt source coverage, native boundary, camera-input or
   source-bundle checks fail.
2. Publish the `Portable.zip`, `Setup.exe` and `Source.zip` produced by the build
   together, unmodified. Do not remove `licenses/` or `third-party-source/` from
   the payload.
3. If the third-party source archives are offered as a separate download, keep
   them available for as long as the binaries are offered.
