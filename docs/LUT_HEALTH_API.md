# LUT health check (feature 1, 0.3.0) — engine/GUI contract

Status: contract written by the lead before implementation. Engine module:
`src/camera_profile_studio/lut_health.py` (headless; no Qt import). GUI uses it only
through `gui/backend.py` (`Engine` adapter), like the other engine modules.

## 1. Loading any LUT file

```python
@dataclass(frozen=True, slots=True)
class LoadedLut:
    values: np.ndarray            # float64 (n, n, n, 3) indexed [r, g, b], read-only
    size: int                     # n, 2..129
    title: str
    source_format: str            # 'cube' | '3dl' | 'hald_png' | 'strip_png' | 'cube_cps'
    source_path: Path
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    warnings: tuple[str, ...]     # e.g. 'domain_not_unit', 'values_outside_unit', 'shaper_1d_ignored'

def load_lut_file(path, *, context: JobContext | None = None) -> LoadedLut
```
- `.cube`: Resolve/Adobe 3D cube of ANY size 2..129 (`LUT_3D_SIZE`), `TITLE`, `DOMAIN_MIN/MAX`
  (non-unit domain: values are kept, warning `domain_not_unit`), comments, CRLF, BOM.
  `LUT_1D_SIZE`-only files → `ValidationError(code='lut_1d_only')`. A 1D shaper block before
  the 3D block → ignored with warning `shaper_1d_ignored`. Data order: red fastest.
- `.3dl`: first numeric line = input breakpoints (count n); following n³ lines integer triples
  (blue fastest per the 3dl convention); bit depth inferred from max value (10/12/16 bit →
  divide by 1023/4095/65535). Also accept float .3dl.
- `.png` / `.tif` (Pillow): Hald (square, level L → n = L², width = L³) or strip
  (width = n², height = n; also flipped variant detected by the CPS metadata if present,
  otherwise assume unflipped). 8- and 16-bit. Unknown geometry → `ValidationError(code='lut_png_layout')`.
- Values outside [0, 1] are kept for diagnosis (warning `values_outside_unit`).
- Errors use existing StudioError/ValidationError style; new codes: `lut_unreadable`,
  `lut_1d_only`, `lut_png_layout`, `lut_size_unsupported` (n < 2 or n > 129).

`to_lut3d(loaded, size=None, *, input_space=SpaceSpec('srgb'), output_space=SpaceSpec('srgb'),
title=None) -> Lut3D` resamples (tetrahedral, using `lut._tetrahedral`) to the nearest supported
size (17/33/65; `size` overrides) and clips to [0, 1], so the result can be previewed and exported
with the existing `lut` functions. `from_lut3d(lut3d) -> LoadedLut` wraps an in-app LUT.

## 2. Diagnosis

```python
@dataclass(frozen=True, slots=True)
class HealthFinding:
    code: str            # see table below
    severity: str        # 'ok' | 'info' | 'warning' | 'error'
    value: float         # main metric (meaning per code)
    threshold: float     # value at which severity becomes 'warning'
    count: int           # number of affected grid nodes/edges
    locations: np.ndarray  # (k, 3) int grid indices of the worst nodes (k <= 64), read-only
    details: tuple[tuple[str, str], ...]   # extra metrics, JSON-safe strings

@dataclass(frozen=True, slots=True)
class InputKindEstimate:
    kind: str            # 'identity' | 'display' | 'log_to_display' | 'display_to_log' | 'unknown'
    confidence: float    # 0..1
    log_candidates: tuple[tuple[str, float], ...]  # [(curve_name, score 0..1)], best first, <= 3
    note_key: str        # message key for the GUI, e.g. 'health.kind_note.display_709_srgb'

@dataclass(frozen=True, slots=True)
class HealthReport:
    size: int
    findings: tuple[HealthFinding, ...]     # fixed order of the table below
    kind: InputKindEstimate
    gray_axis: np.ndarray       # (m, 7): input t, out R, G, B, Oklab L, a, b  (m = 257)
    hue_samples: np.ndarray     # (k, 6): input Oklab a, b ; output Oklab a, b ; flag bits ; chroma
    overall: str                # worst severity
    provenance: tuple[tuple[str, str], ...]

def diagnose_lut(lut: LoadedLut | Lut3D, *, context=None) -> HealthReport
```
All metrics are computed on the LUT's own grid (no resampling), vectorised NumPy, Oklab via
`image_look.srgb_to_oklab` semantics (encoded sRGB in [0,1]; values outside are clipped only for
the Oklab conversion). 65³ must finish in < 2 s on a 4-core laptop.

| code | what | metric `value` (unit) | warning when |
|---|---|---|---|
| `range` | NaN/inf or values outside [0,1] | fraction of nodes outside (0..1) | > 0 (error if NaN/inf) |
| `tone_reversal` | output Oklab L decreases while input increases along r/g/b axes and along the grey diagonal | fraction of edges reversing by > 0.002 L (0..1) | > 0.5 % of edges, or any reversal on the grey axis |
| `gray_tint` | Oklab chroma of outputs for grey inputs (R=G=B) | max Oklab chroma on the grey axis (Oklab chroma units) | info only (creative LUTs tint on purpose); warning if chroma varies non-smoothly along the axis (2nd difference > 0.02) |
| `clipping` | nodes whose output hits 0 or 1 in a channel while the input is not at that bound | fraction of such nodes (0..1) | > 5 % |
| `tone_jump` | discontinuities: places where the 2nd difference of the output along an axis is unusual for this LUT | largest Oklab step of an unusual bend (unit **L**: Oklab lightness, black = 0, white = 1; 0 when no node is unusual). `threshold` = 0.03, so `value > threshold` ⇔ warning. The robust ratio (\|2nd diff\| / median abs dev, limit 12) is in `details['robust_ratio']`; the lightness part of the worst step in `details['step_L_component']` | robust ratio > 12 **and** Oklab step > 0.03 |
| `hue_fold` | output hue rotates by > 60° between neighbouring nodes where output chroma > 0.04 | count of such edges (edges) | count > 0 |
| `banding_risk` | local gain: Oklab-L step between neighbours / input step | max gain (ratio: output Oklab distance / input Oklab distance) | gain > 4 (8-bit quantisation becomes visible) |
| `flatten` | local Jacobian determinant ≈ 0 (many inputs collapse to one colour) | fraction of cells with \|det\| < 1e-3 × median (0..1) | > 2 % |

Severity 'ok' when below threshold. Thresholds are module constants, recorded in provenance.
`value` is meant to be shown to the user as is (units in the table; the module docstring of `lut_health.py` carries the same table).
`value > threshold` is the warning condition, except: `tone_reversal` (also warns for any grey-axis reversal) and `gray_tint`
(severity 'info' follows `value` > 0.005; its 'warning' threshold 0.02 applies to the largest second difference of the grey path,
`details['max_second_difference']`, not to `value`).

### Input kind estimate
Grey-axis response features: output at input 0, input level where output first exceeds 0.02
(black code), input where output Oklab L ≈ 0.5 (mid), slope at mid, chroma gain for mid-saturated
inputs. `log_to_display` when black code ≥ 0.06 and slope at mid ≥ 1.4 and chroma gain ≥ 1.2.
Candidates are ranked by distance of (black code, 18 % grey code) to published log curves:
S-Log3, V-Log, F-Log2, F-Log, N-Log, C-Log3, ARRI LogC3 (EI800), Apple Log. Formulas from the
manufacturers' public white papers (document the source and constants in the module).
`display` LUTs get `note_key='health.kind_note.display_709_srgb'` because Rec.709 vs sRGB cannot be
told apart from the LUT data alone (the GUI says so plainly).

## 3. Repair (never automatic)

```python
@dataclass(frozen=True, slots=True)
class RepairSettings:
    smooth: float = 0.0          # 0..1, global Laplacian smoothing of the residual (out - identity)
    fix_jumps: bool = False      # local smoothing only around tone_jump / hue_fold nodes (+1 ring)
    round_clip: bool = False     # local smoothing at the border of clipped plateaus (soft shoulder)
    lock_gray: bool = False      # grey inputs → neutral output (keep Oklab L), correction fades with input chroma
    iterations: int = 8          # 1..64

@dataclass(frozen=True, slots=True)
class RepairResult:
    lut: LoadedLut               # repaired, same size
    change: tuple[float, float, float]   # ΔE00 mean, p99, max over the grid (before vs after)
    before: HealthReport
    after: HealthReport

def repair_lut(lut, settings: RepairSettings, *, context=None) -> RepairResult
```
- Smoothing acts on the residual so an identity-like LUT does not shrink; grid boundary nodes
  keep their position on the cube faces (no inward drift). Output clipped to [0,1].
- All-default settings → bit-identical copy.
- Provenance gains `('repair', json)` with the settings and the change metrics.

## 4. CLI
```
camera-profile-studio diagnose --lut FILE [--json OUT]
camera-profile-studio repair --lut FILE --output OUT [--smooth 0.3] [--fix-jumps] [--round-clip]
                              [--lock-gray] [--format cube|hald-png|strip-png|strip-png-flipped]
                              [--size 17|33|65] [--overwrite]
```
`diagnose` prints a JSON report (findings without `locations` arrays unless `--json` given).
Exit code 0 even when warnings exist; non-zero only on errors (unreadable file etc.).

## 5. GUI (workspace key `diagnose`, nav label 「LUT 診断」/ "LUT Check")
- Open a LUT file (button, drag & drop; `.cube .3dl .png .tif .tiff`) or receive the current LUT
  from another workspace ("診断" button in the LUT output group of every LutWorkspace).
- Left: summary list (one row per finding, severity icon + plain sentence + value), kind estimate
  with log candidates, plain-language explanation of the selected finding.
- Centre: tabs — grey-axis chart (R, G, B and L vs input, identity line), hue map (Oklab a/b plane:
  input→output arrows for sampled nodes, flagged ones highlighted), image preview (existing
  PreviewPanel, before = original LUT, after = repaired LUT when a repair is active).
- Right/side: repair controls (smooth slider 0–100 %, three checkboxes), "Apply" builds a repaired
  LUT in the background and shows the change (ΔE00 mean / max) and the after-report; export via the
  standard LUT output group (size/format) — exports the repaired LUT, or the original resampled LUT
  when no repair is active.
- Before every export in any LutWorkspace, the export path runs `diagnose_lut` on the LUT being
  exported (background, cheap) and writes one log line: "診断: 問題なし" or "診断: 注意 N 件（…）"
  with a link-like action to open it in the diagnose workspace. Export is never blocked by warnings.
- The export JSON sidecar gains `"health": {overall, findings: [{code, severity, value}]}`.
- All strings EN + JA (same keys/placeholders), accessible names, keyboard operable, follows
  docs/UI_DESIGN.md and the existing workspace patterns.
