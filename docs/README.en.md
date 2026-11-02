# Camera Profile Studio

[日本語](DETAILS.ja.md)

> **Source version 0.2.5** is 0.2.4 plus a new Image Look Creator engine (Oklab distribution matching;
> optional *base* images, the user's own photos the look will be applied to; no more neutral-protection
> setting) and tetrahedral LUT interpolation; see docs/BUILD_0.2.5.md and
> [docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md).
> 0.2.4 was 0.2.3 plus exception-handling hardening (specific, localised error
> messages; exports on exFAT/FAT32; damaged settings no longer block start-up).
> 0.2.3 was 0.2.2 plus a fix for camera JPEGs with an MPF preview (they were
> rejected as unsupported). 0.2.2 was 0.2.1 plus PNG LUT export (Hald CLUT and strip PNG besides .cube)
> and error prevention in Batch Processing (items that cannot
> run with the current settings are flagged with the reason before the run); 0.2.1 added GUI fixes
> and an About/licences view (see docs/BUILD_0.2.5.md). 0.2.0 added the **Image Look Creator** (approximate, unverified;
> see [docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md)). For the validation
> scope of 0.2.0, consult the 0.2.0 release report supplied with its artifacts. The
> historical evidence below describes **0.1.0**, not the Image Look Creator.
>
> **Version 0.1.0:** the engine, CLI and GUI are implemented and tested **from source**, and the GUI
> workflows were exercised on Windows 11. **The installer and portable ZIP are built.
> They passed a bounded smoke test on the build machine (not a clean VM).** Implemented
> features are not the same as camera accuracy: **camera JPEG reproduction is
> unverified for every camera.**
> See [docs/FINAL_REPORT.md](FINAL_REPORT.md) for the evidence-backed status.

Camera Profile Studio reads ICC, ICM and DCP camera profiles and turns the colour
processing they actually contain into 3D LUTs (`.cube`). It has three profile-based
jobs, plus the reference-only Image Look Creator added in 0.2.0:

| Function | What it produces | What it is not |
|---|---|---|
| **Technical Convert** | A LUT for the colour transform a profile defines, for example profile device RGB → sRGB, computed with LittleCMS 2 in 32-bit float. | Not a look you can drop onto a finished JPEG. |
| **Camera Look Extractor** | An sRGB → sRGB LUT from the look elements stored in a DCP (HueSatMap, LookTable, ToneCurve). | Not a reproduction of the camera's own JPEG engine. Profiles without these tags produce no look. |
| **Camera Match Studio** | An sRGB → sRGB LUT fitted to corresponding colour samples (colour chart patches or confirmed same-scene image pairs), with held-out ΔE00. | Not possible from an unrelated "reference photo". |
| **Image Look Creator** (0.2.0) | One approximate sRGB → sRGB LUT estimated from 1–64 finished reference photos (JPEG/PNG/TIFF), measured against your own photos (base images) or, if none are given, a typical photo. No before image is needed. | Not a recovered original, camera, OEM or film look. No accuracy or ΔE score exists. Global tone, colour cast and saturation only. |

The colour engine runs without the GUI (Python API and CLI), so it can be tested
automatically.

## Before you export a LUT: five rules

1. **Device RGB LUTs do not go onto JPEGs.** A technical LUT made from a camera
   ICC/DCP expects *camera device RGB* (the profile's own input coordinates, or
   demosaiced, black-subtracted, white-balanced camera-linear RGB for DCP). An
   ordinary JPEG or PNG is already rendered sRGB, so the result would be wrong.
   The preview refuses such LUTs, and the export metadata records the input space.
2. **sRGB here means the sRGB transfer function, not Rec.709 video gamma.**
   `srgb` = Rec.709/sRGB primaries, D65 white, sRGB transfer curve.
   `linear_srgb` = the same primaries and white, linear transfer. If your editor
   works in a Rec.709 (BT.709/BT.1886) video timeline, the tone curve differs
   from what the LUT expects.
3. **Grid presets are 17, 33 and 65.** A larger grid follows the direct transform
   more closely and produces a bigger file (17³ = 4,913, 33³ = 35,937,
   65³ = 274,625 rows). One measured example compared exported LUTs with the
   direct LittleCMS transform on an analytic test profile, over 28,228 colours:

   | Grid | Mean ΔE00 | Max ΔE00 |
   |---|---:|---:|
   | 17 | 0.050976 | 2.045431 |
   | 33 | 0.012305 | 0.867180 |
   | 65 | 0.003430 | 0.260683 |

   These figures are interpolation error only, not camera accuracy. Other
   profiles, especially dark linear-input regions, can differ. Source:
   `output/lut-quality/quality.json`.
4. **Strength is baked into the file once.** `output = (1 − s) × input + s × full`,
   with 0 ≤ s ≤ 1. Strength other than 1 is allowed only when the input and output
   spaces are the same, which in practice means sRGB → sRGB looks and matches. The
   preview samples the baked LUT, so what you preview is what you export. Do not
   expect the file to contain the full-strength version.
5. **SDR only.** Values are limited to the 0–1 domain. HDR, Log/V-Log and
   floating-point HDR images are not supported. Cubes with shapers or non-unit
   domains are rejected on import.

## Typical workflow (GUI, run from source)

The left navigation has six workspaces: **Profile Inspector, Technical
Converter, Camera Look Extractor, Camera Match Studio, Batch Processing,
Settings**. A bottom panel shows Jobs and Log. Source version 0.2.0 adds a
seventh workspace, **Image Look Creator** (see [below](#image-look-creator-020-approximate)); it is not
part of the validation described in this section.

The workflow below was exercised from source on Windows 11, with the native
`windows11` style, at emulated 100/150/200% scaling; see
`output/gui-validation/` and [docs/UI_DESIGN.md](UI_DESIGN.md). The GUI was
accepted from source at all three scales. The packaged application was checked
much more narrowly: one native window was reviewed, and only the Jobs and Log
tabs were selected (see [Build the Windows artifacts](#build-the-windows-artifacts)).

1. **Open a profile.** Drag an `.icc`, `.icm` or `.dcp` file onto the window or
   use the file dialog. The Inspector shows format, version, device class, colour
   space, PCS, tags and SHA-256.
2. **Check what the profile can do.** Each profile shows four states:

   | Item | Possible states |
   |---|---|
   | Technical conversion | supported / unsupported |
   | Camera look extraction | supported / partial / unsupported (also per mode) |
   | Camera match reference | available / missing |
   | Camera JPEG accuracy | verified / unverified |

   Unsupported combinations are disabled and the reason is shown. The
   application never writes a dummy or identity LUT in place of a failed
   conversion.
3. **Choose the input domain and mode.** The Converter is ordered Profile →
   Conversion/colour spaces → LUT size/strength → Advanced (collapsed) →
   Status/Export.
   - Technical directions: profile RGB → sRGB or linear sRGB, sRGB → profile RGB,
     or ICC → ICC, which uses a separate output-profile picker.
   - A direction the profile cannot do is disabled, with the engine's reason.
     For example, `Nikon_D200.icc` cannot be an output because it has no inverse
     path.
   - Look modes: Colors Only, Tone Curve Only, Look Only, Combined.
   - For DCP technical conversion you must enter the camera neutral (white
     balance). Dual-illuminant profiles also need a temperature in kelvin.
     Export stays disabled until you do; white balance is never guessed.
4. **Preview.** The before/after preview accepts only LUTs whose input is sRGB.
   You can load JPEG, PNG and TIFF images (embedded RGB ICC is converted;
   untagged images are treated as sRGB with a warning) and, with the optional
   `raw` component, camera RAW files.
5. **Export `.cube`.** You choose the size and strength. Export writes
   `NAME.cube` and a sidecar `NAME.json`. The save dialog confirms replacing the
   cube, and an existing sidecar is confirmed separately. Without confirmation
   nothing is overwritten.

**Camera Match** needs real correspondence. Two inputs were exercised:

- A patch CSV with columns `source_r, source_g, source_b, target_r, target_g,
  target_b, group`, holding encoded sRGB values from 0 to 1.
- Two images that you confirm show the same scene. They are then registered;
  the GUI shows the inlier and valid-area percentages, and you must check the
  preview and confirm it before fitting.

Samples are split into training and held-out groups before fitting, and ΔE00
is reported on the held-out data only, with warnings. A match can fail to
improve on identity, and the report says so. Held-out data from the same scene
or chart does not prove accuracy on other scenes.

**RAW files** are developed with one fixed recipe (LibRaw/rawpy AHD
demosaic, 16-bit, sRGB output, camera WB or explicit user WB, no
auto-brightness). This is a consistent starting point for matching, not the
camera maker's JPEG rendering.

**Batch Processing** queues independent jobs, each with its own result: a mix of
succeeded and failed items is normal (for example, an invalid profile fails
while the others succeed). If outputs already exist, you choose Replace, Skip
existing or Cancel.

Cancellation is cooperative, and a running native call finishes its current
chunk first. If you cancel after the files have been committed, the job reports
the actual success and the file paths. A genuine partial-publication error
stays a failure even if you cancelled.

**Language and theme**:

- The interface switches between Japanese and English without restarting
  (Ctrl+Shift+L). Raw engine reasons and technical diagnostics may remain in
  English.
- The theme can be System, Light or Dark. The app uses Qt's `windows11` style
  where available and falls back to `windowsvista` or `Fusion`.
- In a narrow window you may need to scroll the settings column horizontally or
  adjust the splitter.
- A real Nikon D70s RAW file was decoded through the GUI preview in a
  validation probe. That checks decoding only, not camera accuracy.

## Image Look Creator (0.2.0, approximate)

The Image Look Creator estimates **one** global sRGB → sRGB look from 1 to 64
finished reference photos (0.2.5: engine v2, optionally relative to your own base photos). It is a separate pathway from Camera Match Studio: it
builds no sample pairs, needs no before image (a single reference is enough) and
reports no ΔE, because no ground truth exists.

- **What it estimates:** a smooth monotone tone curve, a small colour cast per
  lightness band, and a saturation change (per lightness band and hue sector),
  from Oklab colour statistics. The look is the statistics of the **reference**
  images minus those of the **base** images: your own photos that the look will be
  applied to (optional, but recommended). Without base images a built-in
  "typical photo" baseline is used, which is less accurate (warning
  `typical_photo_baseline`). No pixel correspondence is used. The ranges are
  deliberately narrow, so strong looks come out weaker.
- **References:** JPEG, PNG or TIFF (8/16-bit RGB; embedded ICC is converted to
  sRGB). RAW files are not a supported reference format; the GUI offers JPEG,
  PNG and TIFF only. Each image has a positive weight
  (equal by default; normalised per image, never per pixel) and an optional
  normalised crop `[x0, y0, x1, y1]`. Diverse scenes give a less scene-biased
  result.
- **Controls:** tone, colour and saturation amounts are
  independent (0–1) and are part of the analysis, so changing them means
  analysing again. Conflicting references (tone, colour or saturation) block the
  LUT until you change the selection or explicitly acknowledge the conflict.
- **Output:** preview on a separate target image that is not analysed. Overall
  strength is baked once in LUT generation, and the preview uses the exported
  LUT. Grid 17, 33 or 65. The sidecar records reference basenames (never absolute
  paths), SHA-256, weights, crops, seed, settings and warnings, with status
  `approximate_unverified`.
- **Validation:** the 0.2.0 release report supplied with the artifacts states what
  was validated for this feature; it does not cover the engine v2 of 0.2.5. The
  0.1.0 evidence in this README does not cover it either.

User guide (Japanese): [docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md).
Developer contract: [docs/IMAGE_LOOK_API.md](IMAGE_LOOK_API.md).

## Install and run from source (developers)

> The first standalone build ran with no Python on `PATH` (not on a clean VM).
> A source installation does need Python.

Requirements: Windows x64, CPython **3.12** x64 (the packaging scripts require
3.12; `pyproject.toml` also allows 3.13). Run all commands from the repository
root in PowerShell.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
# Build the pinned native LittleCMS 2.17 (needs Visual Studio 2022 C++ x64 tools)
powershell -NoProfile -File scripts/build-lcms.ps1
$env:CPS_LCMS2_DLL = (Resolve-Path .tools\packaging\lcms-runtime\lcms2.dll).Path
```

- **Dependencies:** `requirements-lock.txt` is the full pinned set (base, GUI,
  RAW via `rawpy==0.25.1`, test, build). The planned standalone build bundles
  rawpy. Only a minimal developer install can leave rawpy out; RAW files are
  then refused with `raw_decoder_missing`, and everything else still works.
- **LittleCMS:** the source ZIP excludes native binaries (`.dll`), so the native
  LittleCMS runtime is **not** in it.
  - `scripts/build-lcms.ps1` downloads the pinned `lcms2.17` source archive,
    checks its SHA-256, builds it with the official VC2022 project (static CRT),
    and validates it into `.tools/packaging/lcms-runtime/` (with a
    `SOURCE.json`). Use `-OutputDirectory` to choose another folder.
  - Point `CPS_LCMS2_DLL` at the built DLL. A checkout that already has
    `src/camera_profile_studio/vendor/lcms2/lcms2.dll` uses that copy
    automatically.
  - Nothing is downloaded when the application runs.

Run the tests. The GUI tests need pinned external profile fixtures, so fetch
them first (network, revision- and SHA-256-pinned, written to
`input/ccd_profiles/`):

```powershell
.venv\Scripts\python.exe tasks/ccd/acquire.py
$env:QT_QPA_PLATFORM = 'offscreen'
.venv\Scripts\python.exe -m pytest
```

Start the GUI:

```powershell
.venv\Scripts\python.exe -m camera_profile_studio.gui
```

## Command line

The CLI follows the contract in [docs/API_CONTRACT.md](API_CONTRACT.md).
Examples 1 and 2 below were run from the repository root on 2026-10-20 and
produced valid 33³ cubes. The `match` command is covered by an automated test
(`tests/test_cli.py::test_real_cli_matching_holdout`). That test uses synthetic
NPZ samples: the held-out error improves, and the training and validation
indices are disjoint. A synthetic test says nothing about camera or OEM accuracy.
The packaged smoke test also ran a SciPy `ccm_tone_lut` match on synthetic data.

```text
python -m camera_profile_studio inspect PROFILE [--json]
python -m camera_profile_studio convert --request REQUEST.json --output OUT.cube [--size 17|33|65] [--strength FLOAT] [--overwrite]
python -m camera_profile_studio match --samples SAMPLES.npz --output OUT.cube [--settings SETTINGS.json] [--size 17|33|65] [--strength FLOAT] [--overwrite]
python -m camera_profile_studio image-look --request REQUEST.json --output OUT.cube [--size 17|33|65] [--strength FLOAT] [--overwrite]
python -m camera_profile_studio gui
```

The installed console script `camera-profile-studio` is the same program.
Defaults: `--size 33`, `--strength 1.0`. On success, a JSON summary (`export`,
plus `holdout_report` for `match`, or `image_look_report` with
`status: "approximate_unverified"`, `warnings`, `conflicts`, `baseline` and `base_count` for `image-look`) is
printed to stdout. Errors are printed to
stderr as JSON `{"code", "message", "details"}`.

Exit codes: `0` success; `2` validation or unsupported request (for example
`strength_space_mismatch`); `3` dependency, image, export or I/O error (for
example `output_exists` when the cube or its sidecar already exists and
`--overwrite` is not given); `130` cancelled. `inspect` returns 0 when parsing
succeeds, even if some capabilities are unsupported. `image-look` returns 2 with
`image_look_conflict` when references conflict and the request does not set
`"acknowledge_conflicts": true`.

**Example 1: technical ICC conversion** (profile device RGB → sRGB). Save as
`d70-technical-request.json` in the repository root:

```json
{
  "schema_version": 1,
  "input_space": {"kind": "icc_rgb", "profile_path": "input/ccd_profiles/Nikon_D70.icc"},
  "output_space": {"kind": "srgb"},
  "mode": "technical",
  "profile_path": "input/ccd_profiles/Nikon_D70.icc",
  "intent": "relative",
  "black_point_compensation": false,
  "gamut": "clip"
}
```

```powershell
.venv\Scripts\python.exe -m camera_profile_studio inspect input\ccd_profiles\Nikon_D70.icc --json
.venv\Scripts\python.exe -m camera_profile_studio convert --request d70-technical-request.json --output output\d70-technical.cube --size 33
```

The resulting LUT's input is profile device RGB, not sRGB. Do not apply it to
JPEGs (rule 1).

**Example 2: DCP look** (sRGB → sRGB). `Nikon_D70s.dcp` has two illuminants, so
a temperature must be given explicitly. 5000 K is only an example value, not a
recommendation. Save as `d70s-look-request.json`:

```json
{
  "schema_version": 1,
  "input_space": {"kind": "srgb"},
  "output_space": {"kind": "srgb"},
  "mode": "look",
  "profile_path": "input/ccd_profiles/Nikon_D70s.dcp",
  "dcp": {"illuminant_kelvin": 5000},
  "look_mode": "combined"
}
```

```powershell
.venv\Scripts\python.exe -m camera_profile_studio convert --request d70s-look-request.json --output output\d70s-look.cube --size 33 --strength 0.8
```

Rules for request and settings files:

- They mirror the engine dataclasses (`TransformRequest`, `DcpSettings`,
  `MatchSettings`) with snake_case fields. Omitted optional fields take their
  defaults.
- Unknown fields, duplicate keys, NaN/Inf and files over 1 MiB are rejected.
- `schema_version` is optional. Only `1` is accepted.
- Relative paths are resolved against the current directory, so run the
  commands from the repository root. **Exception:** in an `image-look` request,
  relative reference paths are resolved against the folder that contains the
  request JSON. A complete `image-look` example is in
  [docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md) (section 10).
  Each `references` entry takes an optional `"role"` (`"reference"` by default, or `"base"`).
- The output folder must already exist.
- Do not give the request file the same name as the output's sidecar
  (`OUT.json` next to `OUT.cube`). The CLI refuses this with
  `source_output_collision`.

`SAMPLES.npz` must contain exactly `source_rgb` and `target_rgb` (N×3 encoded
sRGB in 0–1), `groups` (a Unicode vector, one group id per row) and `evidence`
(a Unicode scalar describing where the correspondences came from). It is loaded
with `allow_pickle=False`, and the uncompressed limit is 256 MiB.

## Build the Windows artifacts

> **Building 0.2.5:** follow [docs/BUILD_0.2.5.md](BUILD_0.2.5.md). It covers the
> fresh-checkout route (`scripts/build-fresh-source.ps1`) and the protected local
> release route (`scripts/build-release.ps1`). The `build-windows.ps1` commands and the
> results in the rest of this section are the historical **0.1.0** build, not current
> 0.2.5 instructions.

Build-machine requirements: Visual Studio 2022 C++ x64 tools and a Windows SDK
(used only on the build machine). Then:

```powershell
powershell -NoProfile -File scripts/build-windows.ps1
powershell -NoProfile -File scripts/verify_distribution.ps1
```

The scripts use Nuitka 2.7.12 standalone and Inno Setup 6.0.5. Release
artifacts (the status column records the historical **0.1.0** build; for 0.2.0,
consult the 0.2.0 release report supplied with its artifacts):

| File | Contents | Status |
|---|---|---|
| `output/CameraProfileStudio-Setup.exe` | Per-user installer, unsigned | Built; smoke PASS |
| `output/CameraProfileStudio-Portable.zip` | Standalone folder, no Python needed | Built; smoke PASS |
| `output/CameraProfileStudio-Source.zip` | Filtered source (no native binaries) | Built; identical to the copies inside the payload and Portable ZIP |
| `output/CCD_Profile_LUTs.zip` | Task 01 LUTs and evidence | Built and verified (68 entries) |
| `output/Medium_Format_LUT_Research.zip` | Task 02 report, supplement, delivery-safe source indices and scripts | Built and delivered (28 entries) |
| `output/Validation_Evidence.zip` | Test, audit and GUI validation evidence | Built and verified; exact inventory in its `BUNDLE_MANIFEST.json` |

SHA-256 values are published in `DELIVERY_SHA256SUMS.txt` and
`DELIVERY_MANIFEST.json` next to the artifacts, not inside these documents. `build-windows.ps1` also rewrites
`requirements-lock.txt` from the build environment.

GitHub Actions: `.github/workflows/windows-distribution.yml` (manual trigger,
`windows-2022`). It builds LittleCMS, fetches the pinned test profiles, runs
the tests and then builds. It has not been run yet. Details and the limits of
the smoke test are in [packaging/README.md](../packaging/README.md).

### Packaged build: smoke results

Evidence: `output/distribution/first-build-results.json`,
`output/distribution/repack-results.json`, `output/distribution/smoke-results.json`
and `output/distribution/final-smoke/`. The checks ran on the build machine with
Python removed from `PATH`, on the first build and again after a `-SkipCompile`
repack (source fingerprint and EXE unchanged). All of
them passed:

- Portable `--help`; ICC `inspect` on a generated profile; sRGB → linear sRGB at
  17³ (4,913 rows, maximum error 2.67e-8 against the direct curve).
- D70s Look Only at 17³: finite values, not an identity LUT.
- SciPy `ccm_tone_lut` matching at 17³ on synthetic grouped samples (192
  training, 64 held out, disjoint): mean held-out ΔE00 fell from 4.69993 to
  0.06843. This is synthetic data, not OEM accuracy.
- The GUI stayed alive for 8 seconds offscreen. A native window opened, and UI
  Automation selected the Jobs and Log tabs. The lead reviewed the native window
  capture: one dark, English Profile Inspector window with legible text and
  icons, and no error panel.
- The installer checked for an existing registration under its exact AppId
  (HKCU/HKLM, 32/64-bit) and found none. Silent per-user install, installed
  `--help` and uninstall all exited with 0, and the uninstall removed the
  registration.
- Inventory: 178 native binaries, 18 Qt files mapped to their sources, 28
  Fluent SVG icons with their licence notice, ZIP CRC passed, and no camera
  profiles or RAW files in the payload.

Not covered: packaged Japanese UI, other packaged workflows, DPI settings, RAW
in the packaged GUI, clean VM, network isolation, code signing and legal
clearance. The optional Matplotlib API is unavailable in the build; still-image
matching passed without it. No engine or GUI code changed for the build.

Final packaging gate: a documentation-only wrapper refresh reuses the same
compiled executable (`-SkipCompile`) and reruns the full smoke test before
publication. Authoritative final sizes and SHA-256 values are in
`DELIVERY_SHA256SUMS.txt` and `DELIVERY_MANIFEST.json`, delivered next to the
artifacts.

## Screenshots

Source-run captures on Windows 11 with the `windows11` style, as client-area
widget grabs. Each scale folder has every workspace in EN/JA × Light/Dark, plus
narrow-window captures:

Screenshots are not stored in the repository. Run `python tests/gui_screenshots.py` to generate them in `output/gui-validation/screens/`.

Scaling was emulated with `QT_SCALE_FACTOR` on a 225% host; Windows display
scaling itself was not changed. The action log for each run is in
`output/gui-validation/manifest_scale*.json`. These are not screenshots of the
packaged application. The packaged-app capture is
`output/distribution/final-smoke/native-app-window.png`.

## Licences and redistribution

- **Camera profiles are not bundled.** Use profiles you are entitled to use.
  The sample profiles used by the tests are not in the repository; see
  [input/ccd_profiles/README.md](../input/ccd_profiles/README.md). Do not redistribute them or LUTs
  derived from them without checking their original terms.
- **Qt / PySide6 6.9.1** are used under LGPL-3.0-only. In the standalone layout,
  Qt libraries stay as separate, replaceable DLLs. The notices state that the
  application imposes no restriction on reverse engineering for debugging such
  replacements.
- **rawpy / LibRaw**: rawpy is MIT. The LibRaw runtime ships under LGPL-2.1 as a
  separate, replaceable DLL.
- **Source archives**: the exact upstream source archives for PySide6/Shiboken6,
  the Qt 6.9.1 modules used, LibRaw with its build dependencies, and
  LittleCMS 2.17 are collected in `output/distribution/third-party-source/`.
  The current list, sizes and SHA-256 values are in its `manifest.json`.
- **Redistributing the binaries.** The source archives and licence texts above ship in both the
  portable and Setup payloads. The build checks that every shipped Qt DLL/plugin maps to an
  included source archive, and excludes the unused Qt6Pdf. The basis and a publishing checklist are
  in [licenses/DISTRIBUTION-NOTICES.md](../licenses/DISTRIBUTION-NOTICES.md) (publisher review, not legal advice).
- **LittleCMS 2.17**: MIT. **Fluent UI System Icons**: MIT (pinned commit in
  `assets/icons/fluent/NOTICE.txt`). Other dependency notices are in
  `licenses/manifest.json`.
- **Camera Profile Studio itself is released under the [MIT License](../LICENSE).** Third-party components keep
  their own licences. The application lists every bundled component with its licence text under
  **Help > Licences** (also **Settings > About**), read from the `licenses/` folder.

## Related deliverables

- **CCD profile LUTs (task 01, accepted):** `output/01_CCD_LUTs/production_final/`
  holds 14 cubes, each with a JSON sidecar and a chart PNG, all marked
  `_EXPERIMENTAL`:
  - 12 technical LUTs: 6 ICC models × 33/65. Their input is profile device RGB,
    so do not apply them to JPEGs.
  - 2 Nikon D70s photo looks: sRGB → sRGB, built from LookTable and ToneCurve
    only.

  Read `production_final/README_JA.md` and `RESEARCH_AND_RIGHTS_JA.md` before
  use. The rights to derived LUTs are unresolved.
- **Medium-format LUT research (task 02, accepted):**
  `output/02_Medium_Format_Research.md` with
  `output/02_Medium_Format_Sources.json`, plus a verified supplement
  (`output/02_Medium_Format_Supplement.md` / `.json`). The supplement covers
  targeted checks such as a 2012 paper and a 2017 official Phase One guide;
  RAW/JPEG samples were checked only by their first 64 KiB, not as full pairs.
  The report classifies evidence and does not rank cameras by accuracy. The web
  search was **not exhaustive**, and camera colour accuracy is unverified.

  The delivered `Medium_Format_LUT_Research.zip` stores **delivery-safe** versions
  of the two source-index JSON files at their usual paths. These are produced by
  `tasks/research/delivery_safe.py`; see `output/delivery-safe/README_JA.md`.
  All 105 quoted source passages (21,326 words) are replaced with omission
  records that keep each passage's SHA-256, word count and location. URLs, HTTP
  status, hashes, numeric facts and the analysis are kept. The original long
  quotations stay local and are not distributed separately. This is a bounded
  copyright precaution, not a legal clearance.

## Documentation

| Document | Contents |
|---|---|
| [docs/TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) | Colour spaces, LUT format, engine behaviour, errors, packaging |
| [docs/LIMITATIONS.md](LIMITATIONS.md) | What the application does not and cannot do (EN/JA) |
| [docs/FINAL_REPORT.md](FINAL_REPORT.md) | Status of every deliverable, with evidence |
| [docs/REQUIREMENTS.md](REQUIREMENTS.md) | Requirement and acceptance ledger (JA) |
| [docs/UI_DESIGN.md](UI_DESIGN.md) | GUI design rationale and validation notes |
| [docs/API_CONTRACT.md](API_CONTRACT.md) | Engine and CLI contract (developers) |
| [docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md) | Image Look Creator user guide (JA), 0.2.0 |
| [docs/IMAGE_LOOK_API.md](IMAGE_LOOK_API.md) | Image Look Creator engine and CLI contract (developers) |
| [packaging/README.md](../packaging/README.md) | Build and packaging details |
