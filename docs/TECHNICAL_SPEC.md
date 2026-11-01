# Camera Profile Studio — Technical specification

Version 0.1.0 (pre-release). This document describes the behaviour that the
current source implements or that the frozen contract
([API_CONTRACT.md](API_CONTRACT.md)) requires. Each section says which of the two
it is. The goals in `input/Astra_Prompts/03_Camera_Profile_Studio_Development.md`
and `input/Astra_Prompts/04_Windows11_Fluent_UI_UX.md` are the target. Nothing
here declares a goal finished. Acceptance evidence is tracked in
[FINAL_REPORT.md](FINAL_REPORT.md).

Markers: **[impl]** = present in the source tree at the time of writing (not by
itself accepted); **[contract]** = required by the contract, implementation not
yet present or not yet verified; **PENDING** = awaiting evidence.

## 1. Architecture

Package root: `src/camera_profile_studio`. Headless modules never import Qt. The
GUI reaches the engine only through `gui/backend.py`.

| Module | Role | State at draft |
|---|---|---|
| `types.py`, `errors.py` | Frozen records, validation, error classes, `checkpoint` | [impl] |
| `color.py` | sRGB encode/decode, sRGB → Lab (D65), ΔE00 | [impl] |
| `icc.py`, `lcms.py` | ICC parser; LittleCMS 2 C API via ctypes, float32 RGB | [impl], frozen; non-invertible TRC defect fixed (section 4) |
| `dcp.py`, `look.py` | DCP parser/renderer, look extraction | [impl], frozen; 83 targeted tests pass |
| `matching.py` | Alignment, sampling, CCM/tone/residual fit | [impl]; 38 targeted tests pass |
| `lut.py` | LUT generation, sampling, validation, `.cube` I/O | [impl], frozen; 57 LUT/image/preview tests pass |
| `images.py` | Image/RAW loading, preview rendering | [impl], frozen; real D70s RAW validation complete |
| `service.py` | Dispatch: inspect / create transform / convert | [impl] |
| `cli.py`, `__main__.py` | Command line | [impl]; `inspect`/`convert` smoke-run; `match` covered by `tests/test_cli.py::test_real_cli_matching_holdout` (synthetic NPZ); packaged smoke ran SciPy matching |
| `gui/**` | PySide6 Widgets application | [impl], frozen; tested from source (section 11). First packaged build smoke-tested narrowly (section 13) |
| `packaging/`, `scripts/`, `.github/workflows/` | Nuitka standalone, Inno Setup, CI | [impl]; first local build and smoke PASS; CI not run |

## 2. Colour spaces

`SpaceSpec(kind, profile_path)` **[impl]**:

| kind | Meaning | profile_path |
|---|---|---|
| `srgb` | Rec.709/sRGB primaries, D65 white, **sRGB piecewise transfer** (not the Rec.709/BT.1886 video transfer) | forbidden |
| `linear_srgb` | Same primaries and white, linear transfer | forbidden |
| `icc_rgb` | Device RGB of the given ICC profile | required |
| `camera_linear` | DCP camera-linear RGB: black-subtracted, demosaiced, normalized, white-balance applied by the supplier | required |

No ICC gamma or camera encoding is guessed. ICC transforms go through the
profile PCS (D50) via LittleCMS. DCP rendering follows the DNG matrix
directions (section 5). Lab for ΔE00 is computed as sRGB → XYZ D65 → Lab D65
**[impl]**.

## 3. LUT model and `.cube` format **[impl]**

- Memory layout: `Lut3D.values` has shape `(N, N, N, 3)`, float32, indexed `[r, g, b]`.
  Generated/exported sizes are N ∈ {17, 33, 65}.
- Domain: `[0, 1]³`. The grid is a uniform unit cube. The transform must return
  finite values in `[0, 1]`, otherwise generation fails (`invalid_transform_output`).
- Sampling: tetrahedral (each grid cell is split into six tetrahedra that share the cell's
  grey diagonal; 0.1.0 to 0.2.4 used trilinear), input clipped to `[0, 1]` by default
  (`out_of_domain='clip'`) or rejected (`'error'`). Grid nodes are reproduced exactly.
- Strength: baked exactly once as `(1 − s)·identity + s·full`, `0 ≤ s ≤ 1`. The full
  transform is evaluated even at `s = 0`, so an unsupported transform cannot
  turn into an identity "success". Non-unit strength requires equal input and
  output spaces (`strength_space_mismatch`).
- Cube text written by `export_cube`:

  ```text
  TITLE "Camera Profile Studio"
  # CPS_INPUT {"kind": "srgb", "profile_path": null}
  # CPS_OUTPUT {"kind": "srgb", "profile_path": null}
  # CPS_STRENGTH 1
  LUT_3D_SIZE 33
  DOMAIN_MIN 0 0 0
  DOMAIN_MAX 1 1 1
  <N³ rows "R G B", red index fastest, then green, then blue>
  ```

  Values use `%.9g`. The file is UTF-8 with LF line endings.
- Import (`read_cube`): accepts only sizes 17/33/65, unit domains, a quoted
  `TITLE`, no shapers/1D sections, no duplicate or misplaced headers, and exactly
  N³ rows. An imported cube carries `jpeg_accuracy: unverified`. Without
  sidecar provenance it is treated as unverified.
- Validation (`validate_lut`): shape, finiteness, unit bounds, strength rules,
  unique provenance keys. Export refuses invalid LUTs.

### Export and sidecar **[impl]**

`export_cube(lut, path, overwrite=False)` writes `<stem>.cube` and `<stem>.json`
to temporary files in the target folder, fsyncs them, then publishes under a
`.lock` file. Without `overwrite`, it uses exclusive hard links, so an existing
file is never replaced. The target folder must already exist. If only one of
the two files could be published, the error is `partial_publication`, never
success. The sidecar holds `schema_version: 1`, `cube_sha256`, `title`, `size`,
`input_space`, `output_space`, `strength`, `domain_min/max`, `provenance`,
`software` and `validation`. Provenance includes source profile hashes, the
sampling note and `jpeg_accuracy: unverified` unless real evidence says
otherwise.

### Grid size comparison

Method: `scripts/validate_lut_quality.py --output output/lut-quality`.

- An analytic D65 / Rec.709-primaries / gamma-1.8 ICC fixture is created.
- Exported and re-read cubes are compared with the direct LittleCMS float
  transform to encoded sRGB.
- 28,228 colours are evaluated: 20,000 seeded random, 24 synthetic chart
  patches, 8,196 gradient samples and 8 corners.
- ΔE00 is computed in D65.

Source: `output/lut-quality/quality.json` and
[LUT_IMAGE_IMPLEMENTATION.md](LUT_IMAGE_IMPLEMENTATION.md).

| Grid | ΔE00 mean | Median | p95 | Max |
|---|---:|---:|---:|---:|
| 17 | 0.05097598 | 0.01605709 | 0.18709845 | 2.04543115 |
| 33 | 0.01230520 | 0.00379391 | 0.03806272 | 0.86718033 |
| 65 | 0.00342995 | 0.00098392 | 0.01160792 | 0.26068291 |

This is interpolation fidelity for one known non-identity transform. It is not
camera JPEG accuracy, and not a bound for every profile. The task 01 production
reports its own per-profile 17/33/65 comparison, in which dark linear-input
regions can show larger errors.

## 4. ICC path (LittleCMS 2)

- Runtime **[impl]**: LittleCMS 2.17 x64 DLL built from the `lcms2.17` tag with
  MSVC (static CRT, imports only KERNEL32).
  - `scripts/build-lcms.ps1` rebuilds it reproducibly from the SHA-256-pinned
    source archive using the official VC2022 Release|x64 project. It validates
    the result (version 2170, float32 identity and non-identity D50 transform)
    and writes it to `.tools/packaging/lcms-runtime/` with a `SOURCE.json`.
    Byte-identical rebuilds are not guaranteed.
  - Library lookup order: explicit path → `CPS_LCMS2_DLL` → the package
    `vendor/lcms2` folder → platform loader.
  - The filtered source ZIP excludes `.dll` files. A source install must
    therefore run `build-lcms.ps1` and point `CPS_LCMS2_DLL` at its output, or
    otherwise provide the DLL.
  - A missing runtime raises `DependencyError(code='missing_lcms')`, and only for
    operations that need it. Nothing is downloaded at run time.
- Native validation **[impl]**: `TYPE_RGB_FLT` sRGB → sRGB identity on 4,096
  random plus edge samples, maximum absolute error 3.7e-9 (tolerance 1e-4),
  perceptual and relative intent, plus a non-identity sRGB white → XYZ D50
  control. Result PASS, recorded in `SOURCE.json`. This validates the DLL, not
  the application transform paths.
- Transform rules **[contract]/[impl]**: float32 in and out, with no 8-bit
  intermediate. Intent: perceptual, relative, saturation or absolute. Black
  point compensation is optional. The engine rejects CMYK/Lab device inputs,
  device links, missing directions (no A2B/B2A or matrix/TRC inverse), absent
  intents and incompatible classes (`unsupported_direction`,
  `unsupported_intent`). Capabilities include `technical.input` and
  `technical.output` for ICC profiles.
- Per-intent capabilities are also reported (for example
  `technical.output.relative`). Constant, non-monotonic or flat-interval sampled
  TRCs have no inverse, so the output direction is reported as unsupported.
- **Fixed defect.** The audit found P2: a constant sampled TRC was previously
  advertised as output-capable and mapped everything to black
  (`output/engine-audit/REPORT.md`). The closure check
  (`output/engine-audit/closure/REPORT.md`, 2026-10-20) shows the following on
  the frozen core:
  - The original reproducer is now refused with `unsupported_direction` for
    all four intents and through service conversion.
  - All six supplied ICC profiles still convert 4,100 float32 samples, with
    maximum difference 0.0 from direct native LittleCMS calls (D200 uses
    perceptual, because it only has A2B0).

## 5. DCP rendering and look extraction **[impl], in progress**

Source-grounded decisions are recorded in
[DCP_IMPLEMENTATION.md](DCP_IMPLEMENTATION.md). It cites Adobe DNG SDK
`dng_color_spec.cpp`, `dng_reference.cpp`, `dng_render.cpp` and RawTherapee
`rtengine/dcp.cc`. These are references, not a claim of bitwise SDK
equivalence. Summary:

- Parser: classic little-endian header with DCP magic `0x4352`, single IFD
  (multiple IFDs or embedded DNG images are rejected), 32 MiB limit. Duplicate,
  out-of-range, nonfinite and zero-denominator tags are rejected.
- Technical: `camera_linear` → encoded sRGB. Requires a positive
  `camera_neutral`. Dual-illuminant profiles require an explicit
  `illuminant_kelvin` (interpolation is linear in reciprocal CCT). ColorMatrix
  maps XYZ → camera; ForwardMatrix maps white-balanced camera → D50 XYZ.
  Singular matrices fail. The engine assumes identity per-camera calibration and
  rejects calibration tags it cannot honour (`unsupported_calibration`).
- Tables: HueSatMap/LookTable shape `(V, H, S, 3)`, hue wraps, trilinear
  interpolation; sRGB value encoding supported for table addressing. Processing
  order: camera → linear ProPhoto → HueSatMap → (exposure) → LookTable → RGB
  tone → output encoding. Unsupported active encodings, nonzero
  BaselineExposureOffset and unknown processing-affecting tags refuse the
  affected mode.
- Look modes (sRGB → sRGB, not inversion of a camera matrix):
  `colors_only` = HueSatMap + LookTable, `tone_only` = ToneCurve,
  `look_only` = LookTable, `combined` = all look elements present. A mode with no
  applicable elements raises `UnsupportedError(code='missing_look_components')`.
  No default Adobe tone or look is substituted when the profile lacks one.
- Tests: the final combined suite (frozen engine and GUI, real RAW enabled)
  gives 281 passed, 0 skipped (`output/final-validation/REPORT.md`). Within
  it, `tests/test_dcp.py` and `tests/test_look.py` give 83 passed
  ([DCP_IMPLEMENTATION.md](DCP_IMPLEMENTATION.md)). The independent audit
  (`output/engine-audit/test_independent_audit.py`, which uses real binary DCP
  fixtures and published Bradford/sRGB coefficients) matched analytic
  expectations:
  - ForwardMatrix with nonunit WB: maximum absolute RGB error 5.9e-8.
  - ColorMatrix inverse with Bradford and exposure normalization: 2.9e-8.
  - Dual-illuminant reciprocal-kelvin interpolation: 4.5e-8.
  - HSV table addressing and encoding, and hue-preserving tone: passed.

  These are algorithm checks, not camera JPEG accuracy.

## 6. Camera matching **[impl], in progress**

Design notes: [MATCHING_IMPLEMENTATION.md](MATCHING_IMPLEMENTATION.md).

- Inputs: paired N×3 encoded sRGB samples (`SamplePairs`) with a group id per
  row and an evidence string. Sources of correspondence: measured chart patches
  (reference XYZ/Lab converted to sRGB first), user-selected patches, or a
  same-scene pair confirmed by the user (`same_scene_confirmed=True`).
  Registration reports a homography, valid mask and inlier fraction, and
  rejects low-confidence results. Sampling uses spatial-block groups.
- Split: a deterministic group split happens before any fitting. The holdout is
  never used for selection. Fitting requires at least 2 groups, at least 6
  training and 6 holdout samples, and a full-rank fit.
- Model: 3×3 CCM in linear light → encode → per-channel piecewise-linear tone
  (K×4 knots) → trilinear residual grid (5, 9 or 17) with ridge and smoothness
  regularization → clamp. Fit levels: `ccm`, `ccm_tone`, `ccm_tone_lut`.
- Report: holdout ΔE00 before and after (count, mean, median, p95, max), plus
  indices and warnings. A single-scene or chart holdout does not establish
  cross-scene camera accuracy.
- The export LUT is generated from the match transform through the shared LUT
  path.
- Tests: `tests/test_matching.py` and `tests/test_matching_diagnostics.py` give
  38 passed ([MATCHING_IMPLEMENTATION.md](MATCHING_IMPLEMENTATION.md)). These are
  synthetic and independent-target checks. No real chart or camera pair has
  been fitted.

## 7. Images, RAW and preview **[impl]**

- Formats: JPEG, PNG and single-page RGB/RGBA TIFF (8/16-bit unsigned). Output
  is orientation-corrected H×W×3 float32 encoded sRGB in `[0, 1]`. Embedded RGB
  ICC is converted through LittleCMS before resizing. Untagged images are
  assumed to be sRGB, with a warning. Alpha is composited on white and recorded.
  Malformed ICC, CMYK/Lab, floating-point/HDR, multipage TIFF and undeclared
  fourth channels are rejected.
- RAW (optional `rawpy==0.25.1`, LibRaw): AHD demosaic, 16-bit, sRGB output,
  gamma `(2.4, 12.92)`, `no_auto_bright`, no auto WB, camera WB *or* explicit
  4-value user WB, `exp_shift` from 0.25 to 8, highlight clip, no noise
  reduction, full size. Every option and the rawpy/LibRaw versions are recorded
  in the metadata. If rawpy is missing, the loader raises
  `DependencyError(code='raw_decoder_missing')`. This is developed sRGB for
  matching, not camera-linear DCP input or an OEM JPEG pipeline.
- Preview: `render_preview` accepts only LUTs with `srgb` input
  (`preview_input_space`) and samples the same baked LUT that is exported. A
  `linear_srgb` or ICC output is converted to display sRGB explicitly;
  otherwise it is rejected (`preview_output_space`).

## 8. Capabilities **[impl]**

Each `ProfileInfo.capabilities` entry has a feature key, a status and reasons:

| Feature | Statuses |
|---|---|
| `technical` | supported / unsupported (ICC also has `technical.input`, `technical.output`) |
| `look` | supported / partial / unsupported, plus `look.colors_only`, `look.tone_only`, `look.look_only`, `look.combined` |
| `match_reference` | available / missing |
| `jpeg_accuracy` | verified / unverified |

Parse-only inspection reports `match_reference: missing` and
`jpeg_accuracy: unverified`. The GUI may overlay evidence the user has selected.
ICC profiles always report `look: unsupported` ("ICC camera correction is not an
isolated camera JPEG look").

## 9. Errors, cancellation and exit codes

- Classes **[impl]**: `StudioError` (`message`, `code`, `details`) with the
  subclasses `InvalidProfileError`, `UnsupportedError`, `ValidationError`,
  `DependencyError`, `ImageError`, `ExportError` and `CancelledError`. Stable
  codes include `missing_lcms`, `raw_decoder_missing`, `unsupported_direction`,
  `missing_look_components`, `missing_wb`, `missing_illuminant`,
  `invalid_cube`, `partial_publication` and `cancelled`. I/O errors are wrapped
  with their cause.
- Cancellation **[impl]**: `checkpoint()` checks the token, then reports
  progress, at entry, per chunk (32,768 samples in LUT code) and before
  publication. A native call cannot be interrupted mid-call. Once files are
  published, the export is committed. A cancel arriving after that returns the
  actual success with the file paths (CLI and GUI alike). A genuine
  `partial_publication` error stays a failure even if cancellation was
  requested.
- CLI exit codes **[contract]**: 0 success; 2 validation/unsupported;
  3 dependency/I/O; 130 cancelled. JSON errors are serialized as
  `{code, message, details}`.

## 10. CLI **[impl]**

Commands:

- `inspect PROFILE [--json]`
- `convert --request REQUEST.json --output OUT.cube [--size 17|33|65] [--strength FLOAT] [--overwrite]`
- `match --samples SAMPLES.npz --output OUT.cube [--settings SETTINGS.json] [--size ...] [--strength ...] [--overwrite]`
- `gui`

Defaults are size 33 and strength 1.0.

Request and settings files:

- JSON that mirrors `TransformRequest`/`DcpSettings`/`MatchSettings`, with
  snake_case fields and defaults for omitted fields.
- Unknown fields, duplicate keys, NaN/Inf and files over 1 MiB are rejected.
- `schema_version` is optional, and only `1` is accepted.
- Paths resolve against the current directory.

NPZ: exactly `source_rgb`, `target_rgb`, `groups` (Unicode vector) and
`evidence` (Unicode scalar). It is loaded with `allow_pickle=False`, with a
256 MiB uncompressed limit.

Outputs and errors:

- The CLI refuses outputs whose cube or sidecar path equals an input file
  (`source_output_collision`), and refuses existing outputs without
  `--overwrite` (`output_exists`).
- On success, stdout gets `{"schema_version": 1, "export": ...}`, plus
  `holdout_report` for `match`. On error, stderr gets
  `{code, message, details}`.
- Exit-code mapping in code: `DependencyError`, `ExportError`, `ImageError` and
  codes ending in `_io` map to 3; other `StudioError` to 2; `OSError` to 3;
  cancellation or Ctrl+C to 130.

Smoke run on 2026-10-20 (drafting check, not acceptance):

- The README examples 1 (ICC technical, `Nikon_D70.icc`) and 2 (D70s DCP
  `combined` look, 5000 K, strength 0.8) exited 0 with valid 33³ exports.
- Re-running without `--overwrite` exited 3 (`output_exists`).
- `--strength 0.5` on the technical request exited 2
  (`strength_space_mismatch`).

Examples are in [README.md](../README.md#command-line).

## 11. GUI **[impl], frozen; accepted from source at 100/150/200%; packaged GUI smoke-tested narrowly (section 13)**

Design rationale, sources and validation notes: [UI_DESIGN.md](UI_DESIGN.md).

- PySide6 6.9.1 Qt Widgets with model/view (`QAbstractTableModel`) and layouts.
  No application-wide QSS.
- Style order: `windows11` → `windowsvista` → `Fusion`. The environment
  variable `CPS_STYLE` can request a specific style.
- Theme: System/Light/Dark through `QStyleHints.setColorScheme` (Qt ≥ 6.8). The
  palette comes from the active style.
- High DPI: `HighDpiScaleFactorRoundingPolicy.PassThrough`, so fractional
  scales such as 150% are kept.
- Language: live EN/JA switching through bound string keys. The default follows
  the system locale. Settings are stored as a user-scope INI file
  (`CameraProfileStudio`).
- Jobs: thread-pool workers with a cancel token and progress forwarded through
  Qt signals.
  - States: Queued, Running, Succeeded, Failed, Cancelled, Replaced.
  - Superseded preview results are dropped by generation, so a slow preview
    cannot overwrite a newer one.
  - Jobs with side effects (export, batch) always report their real outcome,
    as described in section 9.
  - Batch runs queue independent service calls with per-item results. Existing
    outputs trigger a Replace / Skip existing / Cancel choice.
- Icons: Microsoft Fluent UI System Icons (MIT, pinned commit), recoloured from
  the palette at runtime.
- Workspaces: Profile Inspector, Technical Converter, Camera Look Extractor,
  Camera Match Studio, Batch Processing and Settings. The Converter has a
  separate output-ICC picker for ICC → ICC conversions.
- Disabled commands state their reason. Examples include the strength lock for
  unequal spaces, preview refusal for non-sRGB-input LUTs, the camera neutral
  and kelvin that DCP needs (never guessed), ICC output directions without an
  inverse, and Match before same-scene confirmation and registration review.
- Source-run validation (`tests/gui_screenshots.py --scale {100,150,200}`,
  `output/gui-validation/manifest_scale*.json`):
  - Native Windows platform with the `windows11` style.
  - Effective DPR 1.0/1.5/2.0, emulated with `QT_SCALE_FACTOR` on a 225% host.
  - Each scale has 24 screenshots (6 workspaces × EN/JA × Light/Dark) plus 5
    narrow-window captures.
  - Recorded actions:
    - inspection, including one invalid profile;
    - ICC → sRGB, ICC → ICC with export, and sRGB → ICC with preview;
    - D200 inverse refusal;
    - DCP without and then with operator-entered WB;
    - D70s Look Only at 60% with export and preview;
    - Combined look refused without kelvin;
    - Match from patch CSV, and from a confirmed, registered pair with export;
    - batch with mixed success and failure, then skip and replace re-runs;
    - diagnostics.
  - GUI tests: `tests/test_gui_core.py` and `tests/test_gui_workflows.py` give
    27 passed within the final combined suite
    (`output/final-validation/REPORT.md`). The 150% and 200% runs were
    recaptured on the frozen source with exit 0 and no shown errors.
  - A real D70s RAW was decoded through the GUI preview panel (optional probe,
    PASS, `output/final-validation/probe_result.json`). This checks decoding
    only, not camera accuracy.
- Verified limitations:
  - Engine reasons and diagnostics stay in English in the JA UI.
  - Scaling was emulated, not set through Windows display settings.
  - Captures are client-area widget grabs without the title bar.
  - Drag & drop was tested with simulated drop events, not a physical drag.
  - No Narrator or high-contrast pass was done.
  - Match fixtures are synthetic.
  - Around 760 px wide, the right panel scrolls horizontally. The minimum usable
    window is about 690×680 logical px.

## 12. Dependencies and environment **[impl]**

CPython 3.12.15 x64 (`.venv`). Pinned versions: numpy 2.2.6, scipy 1.15.3,
colour-science 0.4.6, Pillow 11.2.1, opencv-python-headless 4.11.0.86,
tifffile 2025.5.10. GUI: PySide6 6.9.1. RAW: rawpy 0.25.1. Test: pytest 8.3.5,
pytest-qt 4.4.0. Build: Nuitka 2.7.12, ordered-set 4.1.0, zstandard 0.23.0.
`requirements-lock.txt` pins the base, GUI, RAW (`rawpy==0.25.1`), test and build
sets plus transitive packages. `scripts/build-windows.ps1` regenerates it from
the build environment with `pip freeze`.

## 13. Packaging **[impl]; built; smoke PASS (bounded)**

Nuitka standalone (`--mode=standalone`, PySide6 plugin, Qt plugins
`platforms,styles,imageformats`). Qt stays as separate DLLs. The payload
includes:

- the LittleCMS runtime with its notices;
- the GUI icons with their LICENSE/NOTICE;
- rawpy;
- the licences;
- a filtered `CameraProfileStudio-Source.zip` (no `.dll`, `.exe`, `.pyd` or other
  native build products);
- `third-party-source`.

`third-party-source` holds the exact upstream source archives for
PySide6/Shiboken6 6.9.1, the Qt 6.9.1 modules used, LibRaw with its build
dependencies, and LittleCMS 2.17. The current list, sizes and SHA-256 values
are in `output/distribution/third-party-source/manifest.json`. That manifest
records the publisher review (`public_redistribution_cleared`, see `licenses/DISTRIBUTION-NOTICES.md`); the source archives accompany the binaries, so no separate written offer is used.

Inno Setup 6.0.5 produces a per-user installer
(`{localappdata}\Programs\CameraProfileStudio`, Start-menu shortcut off by
default). The portable ZIP contains `launcher.dist/CameraProfileStudio.exe`:
with no arguments it starts the GUI, with arguments it runs the CLI.
`verify_distribution.ps1` runs the smoke checks. Their scope is described in
[../packaging/README.md](../packaging/README.md); they are not a clean-VM,
network-isolated, Narrator or visual-acceptance test.

Release artifacts in `output/`, all built with bounded checks:

- `CameraProfileStudio-Setup.exe` and `CameraProfileStudio-Portable.zip`: smoke PASS.
- `CameraProfileStudio-Source.zip`: filtered; byte-identical to the payload and
  Portable copies.
- `CCD_Profile_LUTs.zip`: verified (68 entries).
- `Medium_Format_LUT_Research.zip`: delivered (28 entries), with the
  delivery-safe source indices from `tasks/research/delivery_safe.py`.
- `Validation_Evidence.zip`: verified (`tasks/delivery/logs/verify_validation.txt`); its `BUNDLE_MANIFEST.json` records the exact delivered inventory.

Final sizes and SHA-256 values are in `DELIVERY_SHA256SUMS.txt` and
`DELIVERY_MANIFEST.json` next to the artifacts, not in this document.

Smoke (`output/distribution/first-build-results.json`, `repack-results.json`,
`smoke-results.json`, `final-smoke/`), run on the build machine with Python
removed from `PATH`, on the first build and again after the `-SkipCompile`
repack (source fingerprint and EXE unchanged). Everything below passed:

- `--help`, generated-ICC `inspect`, sRGB to linear sRGB at 17³ (max error
  2.67e-8);
- D70s Look Only at 17³ (finite, non-identity) and SciPy `ccm_tone_lut` matching
  on synthetic grouped samples (192/64 disjoint; held-out mean ΔE00 4.69993 to
  0.06843, not OEM accuracy);
- 8 s offscreen GUI. The native window was opened and UI Automation selected
  only the Jobs and Log tabs. The lead visually accepted that one dark, English
  Inspector window.
- Exact-AppId registry preflight found nothing. Install and uninstall exited
  with 0, and the registration was removed.
- Inventory: 178 native binaries; 18 Qt mappings; 28 SVG icons plus licence; LittleCMS 2.17.0 packaged; no camera inputs.

Not covered: clean VM, network isolation, packaged RAW GUI, packaged Japanese
UI, packaged DPI, signing and legal clearance. The optional Matplotlib API is
absent. Final packaging gate: a documentation-only wrapper refresh reuses the same
compiled executable (`-SkipCompile`) and reruns the full smoke test before
publication.
