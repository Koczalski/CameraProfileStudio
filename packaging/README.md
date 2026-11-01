# Windows x64 packaging

## Build

1. Provision CPython 3.12.15 x64 into `.venv` (or use `python -m venv .venv`).
2. Install `requirements-lock.txt`, then install the application editable with
   `python -m pip install --no-deps --no-build-isolation -e .`.
3. Ensure the complete app source, GUI and icons are frozen and ready. The build
   automatically rebuilds LittleCMS 2.17 if its DLL is absent (as in the source ZIP).
4. Run `powershell -NoProfile -File scripts/build-windows.ps1`.
5. Run `powershell -NoProfile -File scripts/verify_distribution.ps1`.

The source ZIP includes scripts/build-lcms.ps1 and packaging/verify_lcms_runtime.py.
No hidden .tools/lcms worker output is required. The script obtains the SHA256-pinned
official LittleCMS 2.17 archive, builds its VC2022 Release|x64 project under
.tools/packaging, verifies static CRT imports and real float32 transforms, and
writes fresh binary provenance. It never modifies the app source directory. For
an explicit rebuild run powershell -NoProfile -File scripts/build-lcms.ps1.
The archive and MIT notices accompany the full application distributions; a
source-ZIP-only recipient can download that same pinned archive automatically.

Existing Visual Studio 2022 C++ x64 tools and a Windows SDK are required on the
build machine only. The script imports vcvars64 into its process, never changes
system PATH. Nuitka 2.7.12 builds standalone with PySide6 6.9.1, retaining separate
Qt DLLs. First build may download Nuitka dependency-scanning tools into the
workspace cache. Compiler bootstrap URLs and SHA256 values are pinned in
`scripts/build-bootstrap.ps1`; it extracts official Inno Setup 6.0.5 using
innoextract 1.9 without executing the downloaded installer. Current Inno 6.4.3
cannot be extracted by this tool version (failed attempt is logged).

Outputs are `output/CameraProfileStudio-Setup.exe` and
`output/CameraProfileStudio-Portable.zip`. The latter contains `launcher.dist/`;
launch its `CameraProfileStudio.exe` with no arguments for the GUI, or `--help`
for CLI usage. Neither artifact is signed. The installer is per-user; optional
Start menu shortcuts default OFF. Silent smoke installation supplies no tasks,
installs under a unique workspace sandbox and uninstalls immediately afterward.

`output/distribution` retains compiler/build/smoke logs, inventory and SHA256.
Source and notices are included in the standalone payload. The filtered
CameraProfileStudio-Source.zip includes assets, task scripts, licenses and
.github/workflows; it excludes interpreter caches and native build products.
Icon assets are read from assets/icons/fluent (or the existing package-local
fallback) and installed at camera_profile_studio/gui/icons for icons.py.
Builds lock package
versions, but do not claim byte-for-byte reproducibility (compiler versions,
Windows SDK versions and timestamps can change output bytes).

## Verification boundaries

The verification script launches from a fresh extracted directory, with PATH
restricted to Windows system directories and Python environment variables
cleared (including Qt plugin/path and LCMS override variables). It checks CLI
help, inspection of a generated ICC fixture, a 17-grid builtin sRGB to linear
sRGB conversion against the direct nonidentity decode curve, an offscreen Qt
event-loop startup, per-user silent installation, installed CLI help, and uninstall.
The development Python generates the fixture and validates results; the packaged
process does not invoke it. These checks passed against the first native build.
The packaged D70s LookOnly and SciPy ccm_tone_lut feature gates also passed using
external test inputs, not camera assets included in the payload. These tests do
not simulate a clean Windows VM or isolate the network.

Default verification also launches the native Windows Qt platform, selects two
application tabs via scoped Windows UI Automation, and captures only that
process's main window with PrintWindow. No desktop/global-key capture fallback
is allowed. Inspect native-app-window.png manually for correct/nonblank rendering;
API success alone is not visual acceptance. If accessible tab selection fails,
the check records the limitation and fails rather than claiming interaction.
Hosted CI uses -SkipNativeGui; that explicit skip is NOT desktop acceptance.
The first packaged native-window check passed. UI Automation selected Jobs and
Log tabs ONLY, not all workspaces. The lead visually accepted one nonblank,
coherent dark/English Profile Inspector window with legible text/icons and no
visible error. The bounded review is recorded verbatim in
output/distribution/first-smoke-review/visual-review.txt. It does not establish packaged
Japanese/DPI/all-workspace/RAW-preview acceptance. A final repack requires a new
smoke run and a new review of its newly captured window; do not carry this visual
acceptance forward automatically.

Public redistribution also requires review of `licenses/DISTRIBUTION-NOTICES.md`,
including corresponding Qt/PySide source delivery. Upstream source links alone
are not a claim of complete LGPL compliance. The generated license manifest
includes build/test packages for transparency; some are absent from the payload.
No third-party camera profiles are included by packaging.

Bootstrap acquires and verifies the exact PySide/Shiboken 6.9.1 upstream source
archive (16,669,448 bytes). The third-party-source directory and manifest are
included in both portable and Setup payloads. Five official Qt module archives
(qtbase/qtsvg/qtimageformats/qtshadertools/qttranslations) add 56,554,652 bytes;
the 1.03 GiB monolith was not downloaded. Actual Qt DLL/plugin inventory must
pass packaging/verify_qt_sources.py before artifacts are created. The first
actual payload passes: 18 mapped Qt DLL/plugin files and 178 native binaries
inventoried. The earlier installed-wheel preflight is separate evidence.
LibRaw and its build/dependency source archives are included too; details and
hashes are in packaging/upstream-sources.json. packaging/audit_native.py records
every final DLL/PYD/EXE, verifies shared LibRaw and rejects accidental FFmpeg.
The source archives accompany the binaries, so no separate written source offer is used. The redistribution basis is recorded in `licenses/DISTRIBUTION-NOTICES.md` and `packaging/upstream-sources.json`. Qt6Pdf is excluded; the Qt gate matches DLL names case-insensitively.

## Frozen-code final repack

After the explicit source freeze and first full build, run all packaged checks,
inspect the native-window PNG, and update documentation with actual results.
After the lead's explicit final-docs-ready authorization, run
scripts/build-windows.ps1 -SkipCompile to refresh notices, source ZIP,
documentation and bounded evidence without recompiling the same frozen code.
The build snapshots compiled inputs (src, assets, launcher and dependency metadata)
and rejects -SkipCompile if they changed or the snapshot is absent. It also
checks that source did not change during a full compilation.

Re-run scripts/verify_distribution.ps1 on the final repacked artifacts, then
recompute/compare their SHA256 values with output/distribution/SHA256.txt. Embedded
evidence explicitly describes the earlier package instance; final verification
and final hashes remain outside the packages to avoid a self-referential hash.
Do not imply the final repack was tested until that rerun actually completes.
CI builds/validates LittleCMS before pytest and supplies CPS_LCMS2_DLL for native
engine tests. Before pytest it also runs `.venv/Scripts/python.exe tasks/ccd/acquire.py`
(the actual acquisition entry point; there is no fetch_sources.py). That script
pins upstream revisions and verifies profile SHA256 values, including the three
real profiles required by GUI tests. Downloaded inputs stay in input/ccd_profiles;
input/ and output/gui-validation are excluded from source/payload collection.
The workflow does not skip the real GUI suite and has not been triggered externally.

## First actual build evidence

The source-frozen application was compiled with Nuitka/MSVC and real Setup.exe
and Portable.zip artifacts were produced. output/distribution/first-build-results.json
records the first build and smoke results; external artifact-hashes.json and
compiled-executable.json identify the package instance and compiled executable.
No ZIP self-hash is embedded in this document or the payload.

- Compiled inputs remain unchanged against compiled-inputs.json.
- Restricted-PATH ICC inspection and LCMS 2.17.0 sRGB-to-linear-sRGB conversion
  passed: 4,913 samples, maximum direct-curve error 2.67e-8.
- Packaged D70s LookOnly generated a finite nonidentity 17-grid LUT.
- Packaged SciPy ccm_tone_lut matching passed with 192 training / 64 held-out
  samples and disjoint groups; held-out mean DeltaE00 was 4.69993 before and
  0.06843 after on synthetic paired data, not OEM-camera evidence.
- Per-user sandbox install and uninstall both exited 0; exact AppId checks found
  no prior registration and no remaining registration afterward.
- Portable ZIP CRC, 28 SVG icons plus LICENSE/NOTICE, native source-module
  coverage and exclusion of camera-profile/RAW fixtures passed.

Curated CLI/conversion/native-window/installer evidence is in
output/distribution/final-smoke; original sandbox logs are retained separately.
The current payload's filtered source ZIP is copied to
output/CameraProfileStudio-Source.zip with byte/hash equality recorded separately.
The guarded documentation/source repack has also passed fresh full automated
smoke, with the same compiled executable and unchanged compiled inputs. The
source copy is refreshed with each authorized wrapper run; byte equality, ZIP
CRC and the precise current source hash are recorded outside the archives in
output/distribution/repack-results.json.

Artifacts are unsigned. No clean-VM or actual network-isolated test was performed;
the packaged RAW GUI workflow was not tested. Native visual acceptance is limited
to the one lead-reviewed dark/English Inspector window and Jobs/Log tab selection.
Optional matplotlib APIs are unavailable; tested numerical matching succeeds.
Public redistribution provenance/license review remains open, with no binding
source offer or broad legal guarantee. Current-wrapper verification results,
artifact hashes and the screenshot-specific lead review status are recorded in
output/distribution/repack-results.json and final-smoke, not inferred from an
earlier package's evidence. Historical wrapper results are retained separately.
