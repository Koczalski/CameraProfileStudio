# Building Camera Profile Studio 0.2.5 on Windows

## What 0.2.5 changes

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
`output/releases/0.2.5/workspace`, creates a stage-local tool area, rebuilds pinned
LittleCMS, downloads all nine revision/SHA256-pinned legacy test profiles into
stage-local input, retrieves license/provenance documents, and runs the **full pytest
suite**, not a reduced replacement suite. A test failure blocks compilation. Test
profiles are never packaged. Results include the complete pytest log and JUnit XML;
inspect recorded skips rather than assuming every test ran.

It snapshots fresh build inputs, compiles new source with Nuitka and the 0.2.5 Inno
Setup version, checks input stability, then runs hosted-style verification. That smoke
includes native CLI convert, DCP look, SciPy match, single/multiple-reference and base-image image-look,
offscreen GUI liveness, and exact-AppId-protected installer/uninstall. Existing installed
AppId registration blocks the sandbox installer test; normal user-initiated Setup
upgrades intentionally retain the same AppId.

**Hosted smoke is not native desktop acceptance.** It does not capture or claim a
native Image Look Creator window or an end-to-end packaged GUI image workflow.
`smoke-results.json` records `hosted_ci: true`, `native_desktop_acceptance: false`, and
the skipped native-window gate. No generic release skip switch is permitted. The
hosted mode is restricted to a marked fresh-source staged workspace; the protected
local release wrapper still runs all mandatory native desktop gates.

Successful fresh outputs:

- `output/releases/0.2.5/CameraProfileStudio-0.2.5-Setup.exe`
- `output/releases/0.2.5/CameraProfileStudio-0.2.5-Portable.zip`
- `output/releases/0.2.5/CameraProfileStudio-0.2.5-Source.zip`
- `output/releases/0.2.5/DELIVERY_SHA256SUMS.txt`
- `output/releases/0.2.5/source-tests.log` and `source-tests.xml`
- `output/releases/0.2.5/fresh-build-inputs.json`
- `output/releases/0.2.5/workspace/output/distribution/` build/smoke proof

The fresh snapshot records build provenance; it is NOT the approved release freeze
of the original delivery worktree. Rebuilding functionality does not promise
byte-identical compiler output or clear upstream redistribution/legal limitations.
The pinned acquisition/network/toolchain steps must succeed at execution time.

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
