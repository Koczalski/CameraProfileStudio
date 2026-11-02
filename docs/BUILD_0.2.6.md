# Building Camera Profile Studio 0.2.6 on Windows

## What 0.2.6 changes

0.2.6 is 0.2.5 plus the **LUT Check** workspace (`lut_health.py`, contract in
`docs/LUT_HEALTH_API.md`):

- Opens any 3D LUT: `.cube` of any size 2–129, `.3dl`, Hald and strip PNG/TIFF, or the LUT built
  in another workspace (new "Check" button in every LUT output group).
- Eight findings with plain explanations: values outside 0–1, tone reversal, grey tint, clipping,
  tone jumps, hue folds, banding risk, flattened regions. Grey-axis chart, hue map and preview.
- Input estimate: display LUT or log-to-display LUT with ranked log-curve candidates (S-Log3, V-Log,
  F-Log2, F-Log, N-Log, C-Log3, ARRI LogC3, Apple Log). Rec.709 and sRGB cannot be told apart from
  the data and the UI says so.
- Optional repair, never automatic: smoothing, local jump fix, rounded clip borders, neutral grey axis.
- Every export logs a one-line check result and stores a `health` summary in the JSON sidecar.
- CLI: `diagnose` and `repair` subcommands.

## Updates

0.2.6 also adds an in-app update check (`src/camera_profile_studio/updates.py`, no Qt; GUI in
`gui/updater.py` and `gui/widgets/update_dialog.py`; tests in `tests/test_updates.py`):

- **The repository and its releases must be public.** For a private repository GitHub answers an
  anonymous request with HTTP 404; the app then says the release list was not found and does nothing else.
- Where: Help > Check for updates… (always shows a result) and an automatic check 3 s after start-up, at
  most once per 24 h, as a background job (it never blocks closing the window). Settings > Updates has the
  two switches (automatic check, default on; pre-releases, default on while the major version is 0), a
  "Check now" button and the last result. `CPS_DISABLE_UPDATE_CHECK=1` turns the automatic check off (CI,
  offline verification). The settings ini keeps `[updates]` `auto_check`, `include_prereleases`,
  `last_check`, `last_status`, `last_version` and `skipped_version`.
- Request: one plain `GET https://api.github.com/repos/Koczalski/CameraProfileStudio/releases?per_page=20`
  with `User-Agent: CameraProfileStudio/<version>`; nothing else is sent. Drafts are ignored.
- Versions are compared as SemVer (`0.2.6` > `0.2.6-beta` > `0.2.5`; tag prefix `v` optional), so
  **every release needs a higher version than the one before** (an app that is 0.2.6 is not offered the tag
  `v0.2.6-beta`). Tags look like `v0.2.7-beta`; pre-release tags are offered only when allowed.
- Assets are found by suffix: `*-Setup.exe` and `DELIVERY_SHA256SUMS.txt` (both are needed to install),
  `*-Portable.zip` and `*-Source.zip` (download page only).
- Installed copy (the exe folder equals `InstallLocation` of the HKCU uninstall key
  `Software\Microsoft\Windows\CurrentVersion\Uninstall\{B15FF861-C98A-4F3F-B46D-143C62935FA3}_is1`):
  "Download and install" saves the sums file and Setup.exe to `%TEMP%\CameraProfileStudio-update`
  (`.part` file, resumable, renamed atomically), compares the SHA-256 with the entry in
  `DELIVERY_SHA256SUMS.txt` (mismatch or missing entry: the file is deleted and nothing is installed), asks
  for confirmation and starts `Setup.exe /SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /NORESTART /CPSRELAUNCH=1`,
  then closes the app. Portable and source copies open the release page. The sums come from the same release
  as the installer, so they catch damaged or mismatched uploads, not a compromised release (Setup.exe is not signed).
- `packaging/CameraProfileStudio.iss`: unchanged AppId (in-place upgrade), `UsePreviousAppDir`,
  `CloseApplications`, `RestartApplications`, `AppMutex=CameraProfileStudio_SingleInstance` (the app creates
  the mutex at start and releases it just before it starts Setup), `VersionInfoVersion` taken from
  `ReleaseVersion` (MAJOR.MINOR.PATCH), and a `[Run]` entry that starts the new version after a silent update
  (`/CPSRELAUNCH=1`). The compression is `lzma2/fast` (build speed first) with `LZMANumBlockThreads={#BlockThreads}`
  (`/DBlockThreads=` from the build: logical processors, at most 256, limited by free RAM; 4 when ISCC is run by
  hand) and `LZMAUseSeparateProcess=yes` (supported by ISCC 6.0.5; the script was not compiled on the Linux
  development machine, so the first Windows build is its test).

## What 0.2.5 changed

0.2.5 is 0.2.4 plus a new engine for the Image Look Creator ("image-look-v2") and a change of
the LUT sampling method:

- The look is estimated by Oklab distribution matching instead of the former RGB model. The
  engine measures tone (lightness quantiles), colour cast (mean a/b of low-chroma pixels per
  lightness band) and saturation (mean chroma per lightness band and per hue sector).
- Each image has a role. `reference` images are the finished photos whose look is wanted.
  `base` images are the user's own photos the look will be applied to. The look is the
  reference statistics minus the base statistics. Without base images a built-in
  "typical photo" baseline (pooled statistics of 45 finished photographs) is used instead;
  this is less accurate and is reported as the warning `typical_photo_baseline`.
- The setting `neutral_protection` is removed. The CLI rejects it (and any other unknown
  settings key) with `image_look_invalid`. `analysis_size` now defaults to 160. The model
  has 15 tone knots, a 5 x 2 colour cast, 5 band gains and 12 hue-sector gains.
- CLI `image-look`: a reference entry accepts an optional `"role"` (`"reference"` by default,
  or `"base"`). `image_look_report` has the new fields `baseline` (`base_images` or
  `typical_photo`) and `base_count`. The packaged smoke test (`scripts/verify_distribution_image_look.py`)
  has a third case with a base image.
- LUT sampling (preview, and the resampling of Hald/strip PNG grids) uses tetrahedral
  interpolation instead of trilinear interpolation. Grid nodes are reproduced exactly.
- Docs: `docs/IMAGE_LOOK_API.md`, `docs/IMAGE_LOOK_GUIDE.ja.md`, `docs/IMAGE_LOOK_UI.md` and
  `docs/LIMITATIONS.md` describe v2.

The build procedure is unchanged apart from the version number.

## What 0.2.4 changed

0.2.4 is 0.2.3 plus exception-handling hardening found by an adversarial scan of the
engine, the CLI and the GUI (corrupt files, missing/locked/odd paths, extreme values,
damaged settings, cancellation):

- The GUI installs a global exception hook: an unhandled error on the interface thread
  is logged and shown once instead of disappearing (the packaged app has no console).
- Export works on file systems without hard links (exFAT/FAT32 drives, some network
  shares): an exclusive create-and-copy fallback keeps the no-overwrite guarantee.
- Export failures carry specific codes and wording: file exists, no permission, folder
  missing, disk full, path too long, path is a folder, invalid path (also `None`).
- Image loading reports "not found" / "cannot be read" / "unsupported type" / "too
  large" separately instead of a raw OS message; RAW decode errors name the file.
- Batch: two inputs with the same file name in different folders are held back
  ("Not applicable") instead of one silently overwriting the other.
- A damaged or hand-edited settings file (non-numeric LUT size, unknown format/theme,
  corrupt window geometry) no longer prevents the application from starting.
- CLI: deeply nested JSON requests are rejected as invalid instead of a RecursionError
  traceback; an output path that is a directory is reported as an invalid path.
- PNG read-back decoder rejects truncated or corrupt data with a validation error.
- Common engine error codes now have Japanese and English user-facing text with a
  next step.

## What 0.2.3 changed

0.2.3 is 0.2.2 plus one image-loading fix:

- Camera JPEGs that carry an MPF preview image after the primary image (written by
  many Sony and Nikon bodies; Pillow reports them as `MPO`) were rejected with
  "Only JPEG/PNG/TIFF and supported RAW formats are accepted" in Image Look Creator,
  previews and Camera Match. They are now read as JPEG; the primary image is decoded
  and EXIF orientation applies as before. The rejection message now names the format
  that was detected.

## What 0.2.2 changed

0.2.2 is 0.2.1 plus PNG LUT export and error prevention in Batch Processing:

- Every LUT (Technical Converter, Camera Look, Camera Match, Image Look Creator,
  Batch, CLI `--format`) can be exported as `.cube`, **Hald CLUT PNG** (16-bit,
  ImageMagick `hald:` order; Hald level 4/6/8 for 17/33/65 builds; darktable,
  RawTherapee, G'MIC, ImageMagick) or **strip PNG** (8-bit, 16/32/64 tiles side by
  side, red to the right and green downwards as in Unreal Engine and ReShade, or
  flipped with green upwards). PNG grids are resampled from the built LUT with the
  same interpolation as the preview (trilinear in 0.2.2 to 0.2.4, tetrahedral from 0.2.5);
  the PNG has no colour-management chunks, is decoded again before publication, and gets
  a JSON sidecar describing its layout.

- Each added file is inspected in the background as soon as it is added. Items that
  cannot be processed with the current settings are shown as "Not applicable" (ICC in
  look mode, a DCP without data for the chosen look mode, an unsupported ICC direction)
  or "Needs input" (missing DCP white balance or colour temperature, an ICC rendering
  intent the profile does not support) with the reason, before the run.
- Such items are skipped with that reason when the batch runs, and Run is disabled
  when no item could be processed. Unreadable files are shown before the run and still
  fail independently.
- The look-extraction error for ICC profiles now says what to do instead (use
  Technical conversion).

## What 0.2.1 changed

0.2.1 is 0.2.0 plus GUI fixes found by the 0.2.0 GUI feature scan, and a version and
licence view:

- Camera Match no longer crashes when a tone fit follows a CCM-only fit (engine
  modules are imported on the GUI thread before any job, `Engine.preload`).
- Ctrl+O works on every page; button shortcuts (Ctrl+E, Esc, F5) survive language
  switches.
- Camera Match: explicit RAW white-balance multipliers when camera WB is off; Cancel
  also stops image loading, registration and fitting.
- Batch: the ICC direction no longer applies to DCP files (always sRGB).
- Settings > About shows the version; Help > Licences lists every bundled component
  with its licence text (read from `licenses/`). A licence for the application itself
  is shown when `licenses/APP-LICENSE.txt` exists.

The build procedure below is unchanged apart from the version number.

Two deliberately separate entry points exist. Neither accepts old binaries as a new
release. These instructions describe authorized future runs; packaging preparation
does not itself authorize a build or declare a release source freeze.

## Fresh checkout or extracted Source ZIP

Use a new Windows x64 directory with **no `output` directory**, not the protected
original delivery worktree. The Source ZIP includes all build scripts, tests, pinned
profile acquisition/provenance, source notices and this document. It intentionally
excludes private delivery history, camera inputs, task logs, native binaries and
interpreter/compiler caches. No old delivered artifact or immutable baseline is needed.

Prerequisites: CPython 3.12.15 x64, Visual Studio/MSVC C++ x64 toolchain and Windows SDK,
network access for pinned packages/tools/upstream sources/test profiles, and sufficient
space for Nuitka plus corresponding-source archives. Use the locked dependencies;
do not install globally. Local junction creation is used for the stage's interpreter.

From the fresh source root, in PowerShell:

```powershell
./scripts/build-fresh-source.ps1 -ValidateOnly
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-lock.txt
.venv/Scripts/python.exe -m pip install --no-deps --no-build-isolation -e .
# Only when intentionally authorizing the full build/test/install-smoke operation:
./scripts/build-fresh-source.ps1 -BuildApproved
```

Check each environment setup command's exit code before proceeding. The GitHub
workflow performs these checks explicitly. The fresh entry point rejects any existing
output tree (even in validation mode), source junctions, and missing required files.
Use a new extraction after a failed run; it does not erase or reuse existing output.
Do not delete the original delivery output to make it appear fresh.

The script collects filtered sources into
`output/releases/0.2.6/workspace`, creates a stage-local tool area, rebuilds pinned
LittleCMS, downloads all nine revision/SHA256-pinned legacy test profiles into
stage-local input, retrieves license/provenance documents, and runs the **full pytest
suite**, not a reduced replacement suite. A test failure blocks compilation. Test
profiles are never packaged. Results include the complete pytest log and JUnit XML;
inspect recorded skips rather than assuming every test ran. The suite runs once, as two
pytest invocations that together select every test: the tests not marked `gui` in parallel
(`pytest-xdist`, `-n` = logical processors, at most 16) and the tests marked `gui` serially
in one process. `source-tests.log` / `source-tests.xml` cover both runs (the parts are kept
as `source-tests-parallel.*` and `source-tests-gui.*`). A `.venv` without xdist runs the
whole suite serially instead. `CPS_DISABLE_UPDATE_CHECK=1` is set for tests and smoke runs,
so they never reach the network or show the update bar.

It snapshots fresh build inputs, compiles new source with Nuitka and the 0.2.6 Inno
Setup version, checks input stability, then runs hosted-style verification. That smoke
includes native CLI convert, DCP look, SciPy match, single/multiple-reference and base-image image-look,
offscreen GUI liveness, and exact-AppId-protected installer/uninstall (silent install into a sandbox
directory with `/DIR=`, never with `/CPSRELAUNCH=1`). The packaged-CLI gates run side by side, each with its
own `gate-*.log` in the sandbox; all are awaited and any failure fails the verification.
Normal user-initiated Setup upgrades intentionally retain the same AppId, so the test installer would
overwrite and then delete the registration of an installed copy. In this fresh-source (hosted) mode an
existing registration therefore does not stop the build: the installed copy is left untouched, only the
installer test is skipped, and `smoke-results.json` records `installer_smoke: SKIPPED` (a warning, not
acceptance evidence; the path-length budget of the default install folder is still recorded). The protected
local release route keeps blocking on an existing registration.

**Hosted smoke is not native desktop acceptance.** It does not capture or claim a
native Image Look Creator window or an end-to-end packaged GUI image workflow.
`smoke-results.json` records `hosted_ci: true`, `native_desktop_acceptance: false`, and
the skipped native-window gate. No generic release skip switch is permitted. The
hosted mode is restricted to a marked fresh-source staged workspace; the protected
local release wrapper still runs all mandatory native desktop gates.

Successful fresh outputs:

- `output/releases/0.2.6/CameraProfileStudio-0.2.6-Setup.exe`
- `output/releases/0.2.6/CameraProfileStudio-0.2.6-Portable.zip`
- `output/releases/0.2.6/CameraProfileStudio-0.2.6-Source.zip`
- `output/releases/0.2.6/DELIVERY_SHA256SUMS.txt`
- `output/releases/0.2.6/source-tests.log` and `source-tests.xml`
- `output/releases/0.2.6/fresh-build-inputs.json` and `phase-timings.json` (seconds per build phase;
  `workspace/output/distribution/build-windows-phases.json` splits the packaging phase further)
- `output/releases/0.2.6/workspace/output/distribution/` build/smoke proof

The fresh snapshot records build provenance; it is NOT the approved release freeze
of the original delivery worktree. Rebuilding functionality does not promise
byte-identical compiler output or clear upstream redistribution/legal limitations.
The pinned acquisition/network/toolchain steps must succeed at execution time.

## Build speed: parallelism and caches

Measured on the 0.2.5 build log (16-core/32-thread Ryzen 9 7950X, 30 min): Nuitka took 24 min (85 %) of the
28 min build step with `--jobs=4` and 0 C-object cache hits; tests 72 s, LittleCMS 17 s, Inno Setup 24 s, ZIP
18 s, smoke 40 s. Changes:

- `--jobs` = logical processors (reduced if free memory is below about 1.2 GB per job; `CPS_BUILD_JOBS`
  overrides; the pytest `-n` and the Inno block-thread counts are derived the same way from the processor count
  and free RAM at run time: `CPS_TEST_WORKERS`, `CPS_INSTALLER_THREADS`), `NUITKA_CACHE_DIR` in the persistent cache (clcache C objects, downloaded `depends.exe`,
  bytecode and DLL-dependency caches), pinned lock install with `uv` when available (pip otherwise), pip/uv
  caches, hash-verified cache of the pinned tool and upstream-source downloads, and a cache of the built
  `lcms2.dll` keyed by source SHA-256 + compiler/SDK versions + build flags (its functional and PE validation
  still run). The portable ZIP is built while ISCC runs; `audit_native.py` inspects binaries in parallel.
- Caches live in `%LOCALAPPDATA%\CameraProfileStudio-build-cache` (`-CacheRoot` to move it, delete the folder to
  reset it). They are only enabled by the launcher (`Build-0.2.6.cmd`, environment variable `CPS_BUILD_CACHE`);
  running the scripts directly keeps the cache-free behaviour. `Build-0.2.6.cmd -NoCache` forces a clean build.
  Every cached file is content-checked (SHA-256 or a content-derived key); the EXE is always relinked and
  the input-stability checks are unchanged.
- Expected: about 30 min before, roughly 13 to 16 min after on the machine above (Inno `lzma2/fast`
  with block threads included), a few minutes less on repeated builds. The floor is Nuitka's single-threaded front end
  (about 7 min of Python-level optimisation in the 0.2.5 report, 160 s of it in the single module
  `scipy.integrate._lebedev`), which `--jobs` and the C cache do not touch. Real-time antivirus scanning of the
  build folder and the cache folder slows many small writes; excluding them in Windows Security is optional
  and is never done by the scripts.
- `Build-0.2.6.cmd` prints `推定所要時間 約 N 分` at start and `経過 m:ss / 残り 約 N 分` at each step start. The
  estimate is the per-step seconds of the most recent successful `build-logs\*\summary.json`; without one only
  the elapsed time is shown. There is no periodic refresh.

## Protected existing delivery worktree

Never use the fresh entry point here. Existing 0.1.0 files and the entire old
`output/distribution` file set are protected by the local immutable baseline.
After the lead explicitly freezes source and authorizes compilation:

```powershell
.venv/Scripts/python.exe packaging/release_workspace.py freeze output/image-look-packaging/prep/approved-source-freeze.json
./scripts/build-release.ps1 -SourceFreezeApproved -FreezeManifest output/image-look-packaging/prep/approved-source-freeze.json
./scripts/verify_distribution_release.ps1 -BuildApproved -ImageLookNavigationLabel 'Image Look Creator'
```

The frontend-confirmed English accessible label is `Image Look Creator` (run the
English UI for this smoke). This local route retains immutable checks, no-overwrite approved-freeze publication,
original/staged source checks, and mandatory native-window/installer checks. No private
baseline is expected in the Source ZIP. Both paths refuse `SkipCompile` and produce
versioned artifacts; neither uses direct legacy `build-windows.ps1` invocation.
