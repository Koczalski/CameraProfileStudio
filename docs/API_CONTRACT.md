# Camera Profile Studio: stable worker contract v1

This document freezes imports and seams, not implementation claims. Python 3.12/3.13; package root `src/camera_profile_studio`. No Qt imports in headless modules. All public symbols below must exist at the indicated module path. Implementations may add private helpers, not incompatible signatures.

## Ownership and conventions
- Core: `__init__.py`, `__main__.py`, `types.py`, `errors.py`, `color.py`, `icc.py`, `lcms.py`, `lut.py`, `images.py`, `service.py`, `cli.py` and their tests.
- DCP worker: `dcp.py`, `look.py` and their tests. Matching worker: `matching.py` and its tests. GUI worker: `gui/**` and GUI tests. Packaging: build/installer/workflows/resources integration; coordinate shared metadata edits with lead.
- Research/production: evidence, licensed assets, reproducible production scripts and reports; call these APIs, never invent camera transforms. No worker edits another worker's modules without agreement.
- All result/settings classes below are `@dataclass(frozen=True, slots=True)`. Array fields are copied to owned, C-contiguous, read-only storage. Input arrays are never mutated. RGB arrays have shape `(...,3)`, dtype float32; fitting may use float64 internally. Reject nonfinite values, invalid shapes and invalid settings.
- Signatures are Python notation; `...` denotes implementation to be written, not permitted dummy success. All imports used below are explicitly declared. Use keyword-only optional arguments as shown.

## Shared types: `camera_profile_studio.types`
```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol
import numpy as np
from numpy.typing import NDArray
RGB = NDArray[np.float32]
Matrix = NDArray[np.float64]
Indices = NDArray[np.int64]
Mask = NDArray[np.bool_]
TagValue = int | float | str | bytes | tuple[int | float, ...]
Intent = Literal['perceptual', 'relative', 'saturation', 'absolute']
Gamut = Literal['clip', 'error']
LookMode = Literal['colors_only', 'tone_only', 'look_only', 'combined']
@dataclass(frozen=True, slots=True)
class SpaceSpec:
    kind: Literal['srgb', 'linear_srgb', 'icc_rgb', 'camera_linear']; profile_path: Path | None = None
@dataclass(frozen=True, slots=True)
class Progress:
    stage: str; fraction: float; message: str = ''
class CancelToken(Protocol):
    def is_cancelled(self) -> bool: ...
@dataclass(frozen=True, slots=True)
class JobContext:
    progress: Callable[[Progress], None] | None = None; cancel: CancelToken | None = None
@dataclass(frozen=True, slots=True)
class Capability:
    feature: str; status: Literal['supported', 'partial', 'unsupported', 'available', 'missing', 'verified', 'unverified']; reasons: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class ProfileInfo:
    path: Path; sha256: str; format: Literal['icc', 'dcp']; description: str; device_class: str; color_space: str; pcs: str; version: str; tags: tuple[tuple[str, str], ...]; capabilities: tuple[Capability, ...]; warnings: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class DcpProfile:
    info: ProfileInfo; tags: tuple[tuple[int, TagValue], ...]
@dataclass(frozen=True, slots=True)
class DcpSettings:
    camera_neutral: tuple[float, float, float] | None = None; illuminant_kelvin: float | None = None; gamut: Gamut = 'clip'; include_hue_sat_map: bool = True; include_look_table: bool = True; include_tone_curve: bool = True
@dataclass(frozen=True, slots=True)
class TransformRequest:
    input_space: SpaceSpec; output_space: SpaceSpec; mode: Literal['technical', 'look'] = 'technical'; profile_path: Path | None = None; intent: Intent = 'relative'; black_point_compensation: bool = False; gamut: Gamut = 'clip'; dcp: DcpSettings | None = None; look_mode: LookMode = 'combined'
class RGBTransform(Protocol):
    input_space: SpaceSpec
    output_space: SpaceSpec
    def apply(self, rgb: RGB, *, context: JobContext | None = None) -> RGB: ...
    def close(self) -> None: ...
@dataclass(frozen=True, slots=True)
class Lut3D:
    values: RGB; input_space: SpaceSpec; output_space: SpaceSpec; title: str = 'Camera Profile Studio'; strength: float = 1.0; provenance: tuple[tuple[str, str], ...] = ()
@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool; sample_count: int; minimum: float; maximum: float; errors: tuple[str, ...] = (); warnings: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class RawRecipe:
    use_camera_wb: bool = True; user_wb: tuple[float, float, float, float] | None = None; exposure_shift: float = 1.0; demosaic: Literal['AHD'] = 'AHD'
@dataclass(frozen=True, slots=True)
class ImageData:
    rgb: RGB; source_path: Path | None; metadata: tuple[tuple[str, str], ...] = (); warnings: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class ExportResult:
    cube_path: Path; metadata_path: Path; sha256: str; validation: ValidationReport
@dataclass(frozen=True, slots=True)
class SamplePairs:
    source_rgb: RGB; target_rgb: RGB; groups: tuple[str, ...]; evidence: str
@dataclass(frozen=True, slots=True)
class MatchSettings:
    fit: Literal['ccm', 'ccm_tone', 'ccm_tone_lut'] = 'ccm_tone_lut'; regularization: float = 0.01; grid_size: int = 9; holdout_fraction: float = 0.2; seed: int = 42
@dataclass(frozen=True, slots=True)
class ErrorStats:
    count: int; mean: float; median: float; p95: float; maximum: float
@dataclass(frozen=True, slots=True)
class MatchReport:
    train_indices: Indices; holdout_indices: Indices; before: ErrorStats; after: ErrorStats; warnings: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class MatchModel:
    ccm: Matrix; tone_curve: Matrix; residual: RGB; report: MatchReport; evidence: str
@dataclass(frozen=True, slots=True)
class AlignmentResult:
    source_aligned: RGB; target_rgb: RGB; valid_mask: Mask; homography: Matrix; inlier_fraction: float
```
`SpaceSpec('srgb')` means Rec.709 primaries, D65, sRGB transfer (not Rec.709 video transfer). `linear_srgb` has the same primaries/white and linear transfer. `icc_rgb` and `camera_linear` require the applicable profile path; other kinds forbid it. No guessed ICC gamma or camera encoding. DCP tags retain TIFF tag numbers; numeric arrays flatten in TIFF storage order and dimensions remain separate tags; rationals become floats after denominator validation. Unknown tags are preserved when representable, otherwise reported in warnings.

## Errors and execution: `camera_profile_studio.errors`
```python
from camera_profile_studio.types import JobContext
class StudioError(Exception):
    def __init__(self, message: str, *, code: str = 'engine_error', details: tuple[tuple[str, str], ...] = ()) -> None: ...
class InvalidProfileError(StudioError): ...
class UnsupportedError(StudioError): ...
class ValidationError(StudioError): ...
class DependencyError(StudioError): ...
class ImageError(StudioError): ...
class ExportError(StudioError): ...
class CancelledError(StudioError): ...
def checkpoint(context: JobContext | None, *, stage: str, fraction: float, message: str = '') -> None: ...
```
Exceptions expose `message`, `code`, `details`; use meaningful stable codes (e.g. `missing_lcms`, `raw_decoder_missing`, `unsupported_direction`, `missing_reference`, `cancelled`). I/O errors are wrapped with cause, never swallowed. `checkpoint` checks cancellation then emits progress; fractions are 0..1, monotonic within each stage. Call at entry, bounded chunks/iterations, before publication and after native calls. Native calls cannot promise immediate interruption. Callbacks run on worker threads; GUI forwards via Qt signals. Each job owns its transform/native handles, calls `close()` in `finally`, and receives exactly one GUI terminal state: success/error/cancelled. No Qt objects cross engine seams. Cancellation never returns a success artifact.

## Core API signatures
The following imports apply to every signature block: `from pathlib import Path` and `from camera_profile_studio.types import *` (implementations should use explicit imports).
```python
# icc.py: metadata parser; lcms.py: direct C API, no 8-bit intermediate
# icc
 def inspect_icc(path: Path, *, context: JobContext | None = None) -> ProfileInfo: ...
# lcms
 def probe_lcms(*, library_path: Path | None = None) -> str: ...
 def create_icc_transform(input_space: SpaceSpec, output_space: SpaceSpec, *, intent: Intent = 'relative', black_point_compensation: bool = False, gamut: Gamut = 'clip', context: JobContext | None = None) -> RGBTransform: ...
# color
 def srgb_decode(rgb: RGB) -> RGB: ...
 def srgb_encode(rgb: RGB) -> RGB: ...
 def srgb_to_lab(rgb: RGB) -> Matrix: ...
 def delta_e00(first_lab: Matrix, second_lab: Matrix) -> Matrix: ...
# lut
 def generate_lut(transform: RGBTransform, *, size: int = 33, strength: float = 1.0, title: str = 'Camera Profile Studio', context: JobContext | None = None) -> Lut3D: ...
 def sample_lut(lut: Lut3D, rgb: RGB, *, out_of_domain: Gamut = 'clip', context: JobContext | None = None) -> RGB: ...
 def validate_lut(lut: Lut3D) -> ValidationReport: ...
 def read_cube(path: Path, *, input_space: SpaceSpec, output_space: SpaceSpec, context: JobContext | None = None) -> Lut3D: ...
 def export_cube(lut: Lut3D, path: Path, *, overwrite: bool = False, context: JobContext | None = None) -> ExportResult: ...
# images
 def load_image(path: Path, *, max_dimension: int | None = None, raw_recipe: RawRecipe | None = None, context: JobContext | None = None) -> ImageData: ...
 def render_preview(image: ImageData, lut: Lut3D, *, context: JobContext | None = None) -> ImageData: ...
# service: lazy dispatch, never import Qt; inspection also returns actual runtime capabilities
 def inspect_profile(path: Path, *, context: JobContext | None = None) -> ProfileInfo: ...
 def create_transform(request: TransformRequest, *, context: JobContext | None = None) -> RGBTransform: ...
 def convert_profile(request: TransformRequest, *, size: int = 33, strength: float = 1.0, context: JobContext | None = None) -> Lut3D: ...
```
The leading space on `def` lines is visual grouping only, not Python indentation. LCMS must use float32 RGB buffers/`TYPE_RGB_FLT`, validate ICC v2/v4 RGB matrix/TRC and directional CLUT support, and honor intent/BPC; reject CMYK/Lab device inputs, device links, missing inverse paths and incompatible profile classes rather than guess. LCMS native DLL is a separately provisioned/licensed runtime: explicit probe path, `CPS_LCMS2_DLL`, bundled `vendor/lcms2`, then platform loader; missing runtime raises `DependencyError` only for operations needing it. No DLL downloads at runtime.
`Lut3D.values` is `(N,N,N,3)` indexed `[r,g,b]`; sizes 17/33/65 for generated/exported LUTs. Cube rows loop B outer/G middle/R inner: red fastest. Domain is [0,1]^3; preserve this contract when parsing (reject shapers/non-unit domains rather than silently reinterpret). Sampling is trilinear. Validate finite data, N^3 rows, RGB order, bounds and metadata before export. Strength is baked exactly once as `(1-s)*identity+s*full`, `0<=s<=1`; non-unit strength is allowed only for equal input/output spaces. Preview samples the baked LUT, never blends again. Technical camera-device LUTs cannot be applied to ordinary sRGB photos. Preview accepts sRGB input LUTs only; convert an ICC/linear-sRGB output to display sRGB explicitly, or reject unsupported outputs. Export writes validated temporary files then publishes; no overwrite by default. Report any partial-publication I/O failure, never success.
Images are orientation-correct HxWx3 normalized float32 encoded sRGB. JPEG/PNG/TIFF embedded RGB ICC is converted through LCMS before resize; untagged RGB assumes sRGB with warning/metadata. Malformed/unsupported embedded ICC raises `ImageError`; no silent ignore. Preserve 16-bit TIFF precision, reject ambiguous floating/HDR/CMYK data; composite alpha against white and record it. `max_dimension=None` means full size. RAW uses optional rawpy/LibRaw with AHD, 16-bit output, sRGB output and gamma `(2.4,12.92)`, no auto-bright, no auto-scale override, no exposure auto-adjust, camera WB or explicit user WB, `exp_shift=exposure_shift`; record every effective option, LibRaw/rawpy versions and camera metadata. Reject conflicting WB, absent required WB and unsupported RAW formats. This develops RAW to sRGB for matching, NOT camera-linear DCP input or OEM JPEG emulation. No optional raw extra raises `DependencyError`, not a fake preview. DCP technical input must already be black-subtracted, demosaiced, normalized camera-linear RGB with supplied WB; no sensor pipeline is inferred.

## DCP/look worker API
```python
# dcp.py
 def read_dcp(path: Path, *, context: JobContext | None = None) -> DcpProfile: ...
 def render_dcp(rgb: RGB, profile: DcpProfile, *, settings: DcpSettings, context: JobContext | None = None) -> RGB: ...
 def create_dcp_transform(profile: DcpProfile, *, settings: DcpSettings, context: JobContext | None = None) -> RGBTransform: ...
# look.py
 def look_capabilities(profile: DcpProfile) -> tuple[Capability, ...]: ...
 def apply_look(rgb: RGB, profile: DcpProfile, *, mode: LookMode = 'combined', settings: DcpSettings = DcpSettings(), context: JobContext | None = None) -> RGB: ...
 def create_look_transform(profile: DcpProfile, *, mode: LookMode = 'combined', settings: DcpSettings = DcpSettings(), context: JobContext | None = None) -> RGBTransform: ...
```
DCP renderer: camera-linear input to encoded sRGB output; require positive `camera_neutral`; dual illuminant needs explicit positive Kelvin (never invent scene WB). Respect DNG matrix directions, ForwardMatrix/ColorMatrix, illuminant interpolation, white adaptation, table interpolation/encoding and tone ordering. Settings select real optional components; explicitly disabled components may be omitted, absent optional components are recorded, unsupported active encodings fail. Invalid/singular matrices fail. Additional camera calibration/analog balance beyond a standalone profile is not inferred: report this limitation. `apply_look` is sRGB to sRGB using reference working-space conversion, NOT inversion of a camera input matrix. Colors Only = HueSatMap + LookTable, Tone Only = ToneCurve, Look Only = LookTable, Combined = all present look elements. Requesting a mode with no applicable elements raises `UnsupportedError`. Missing components give partial capability with reasons. All such photo looks are profile-derived approximations, not verified camera JPEG reproduction. Service dispatches DCP technical/look to these factories, validates request spaces and rejects unsupported options rather than ignore them.

## Matching worker API
```python
# matching.py
 def align_pair(source: ImageData, target: ImageData, *, same_scene_confirmed: bool = False, context: JobContext | None = None) -> AlignmentResult: ...
 def sample_image_pair(source: ImageData, target: ImageData, *, same_scene_confirmed: bool = False, valid_mask: Mask | None = None, max_samples: int = 20000, seed: int = 42, context: JobContext | None = None) -> SamplePairs: ...
 def fit_match(samples: SamplePairs, *, settings: MatchSettings = MatchSettings(), context: JobContext | None = None) -> MatchModel: ...
 def apply_match(rgb: RGB, model: MatchModel, *, context: JobContext | None = None) -> RGB: ...
 def create_match_transform(model: MatchModel) -> RGBTransform: ...
```
All matching samples are paired Nx3 encoded sRGB; chart users construct `SamplePairs` from measured corresponding patches (convert documented reference XYZ/Lab to sRGB first). Arbitrary reference photographs without correspondences are unsupported for supervised matching. Same-scene flag defaults false: registration/sampling refuses unconfirmed pairs. Sampling requires equal geometry or explicit successful registration, excludes invalid/occluded areas via mask, and assigns spatial-block groups (not independent adjacent pixels). Alignment reports homography, validity mask and inlier quality and rejects low confidence/nonplanar failure. User must inspect registration; geometry alone does not prove color correspondence. Evidence records RAW recipe/ICC normalization and pairing conditions.
Split deterministic groups before all fitting; never use holdout for CCM/tone/LUT selection. Require >=2 groups and >=6 train/6 holdout samples with full-rank fit; reject insufficiency. Single-scene/block or chart-patch holdout does not establish cross-scene camera accuracy. `ccm` is 3x3, `tone_curve` Kx4 (shared increasing x, three output channels), `residual` GxGxGx3 additive correction on unit sRGB cube. Apply: decode sRGB -> CCM (`linear @ ccm.T`) -> encode/clamp -> per-channel piecewise-linear tone -> trilinear residual -> clamp. Disabled tone is identity knots; disabled residual is zeros as a documented fitted model component, never dummy successful training. Grid size 5/9/17; regularization positive; holdout fraction strictly between 0 and 1. Report before/after holdout DeltaE00 using sRGB -> XYZ D65 -> Lab D65 consistently, plus actual indices/counts and warnings; no training-error-only claims. Matching transform is sRGB->sRGB; core generates its export LUT.

## Payloads, capabilities, executable integration and acceptance
- No HTTP server. GUI calls typed jobs. JSON uses `schema_version: 1`, snake_case dataclass fields, enum strings, POSIX-or-Windows path strings, arrays as nested lists only when explicitly needed; reject NaN/Inf. Exceptions serialize `{code,message,details}` with details as an object. Metadata tuples serialize as objects with unique keys. Profile tags may serialize as ordered pairs to retain names/order.
- Export companion `<stem>.json` includes schema_version, cube_sha256, title, size, input_space/output_space, strength, domain_min/max, provenance and validation; cube comments include spaces/strength. Provenance includes source hashes, settings, software/runtime versions, processing conditions, experimental status and evidence. A cube imported without provenance is unverified. Never claim verified merely because parsing or fitting succeeded.
- Profile capabilities have fixed feature keys `technical`, `look`, `match_reference`, `jpeg_accuracy`; technical supported/unsupported, look supported/partial/unsupported, reference available/missing, JPEG verified/unverified. Parse-only inspection defaults missing/unverified; GUI overlays actual selected evidence. Per-look modes use additional `look.<mode>` capabilities. Unsupported combinations explain why and disable execution; no identity/dummy LUT success. Unrecognized tags affecting processing downgrade/refuse the affected mode, not all unrelated modes.
- CLI `camera-profile-studio = camera_profile_studio.cli:main`, `main(argv: list[str] | None = None) -> int`; `python -m camera_profile_studio` invokes same. Commands: `inspect PROFILE [--json]`; `convert --request REQUEST.json --output OUT.cube [--size 17|33|65] [--strength FLOAT] [--overwrite]`; `match --samples SAMPLES.npz --output OUT.cube [--settings SETTINGS.json] [--size 17|33|65] [--strength FLOAT] [--overwrite]`; `gui`. Request/settings JSON exactly mirrors the corresponding dataclass, absent optional fields take defaults; NPZ contains source_rgb, target_rgb, Unicode groups, scalar Unicode evidence, loaded with allow_pickle=False. Exit 0 success, 2 validation/unsupported, 3 dependency/I/O, 130 cancellation; inspect reports unsupported capability with exit 0 if parsing succeeded.
- GUI entry `camera-profile-studio-gui = camera_profile_studio.gui.app:main`, `main() -> int`. Install `[gui,raw]` for full desktop input support. PySide6 Widgets, official windows11 QStyle with fallback, model/view, Qt layouts, JA/EN, light/dark, accessible keyboard and DPI behavior remain required. GUI implements batch by queued independent service calls with per-item terminal status, not one shared native transform.
- Pinned pyproject uses NumPy/SciPy/colour/Pillow/headless OpenCV/tifffile; no competing OpenCV Qt plugins. rawpy 0.25.1 has verified PyPI cp312/cp313 Windows x64 wheels and numpy>=1.26 requirement compatible with NumPy 2.2.6. Native LCMS and installer tools are NOT supplied by pip metadata. Packaging owns licensing, DLL provenance, Nuitka/pyside6-deploy, offline portable ZIP/setup EXE and Windows CI. These entry points are declarations until workers implement them.
- Required tests: real LCMS float32 references and invalid direction; DCP matrix/table/dual-WB/encoding fixtures against independent references; red-fastest asymmetric cube and trilinear endpoints; preview/export strength identity; embedded ICC/16-bit/RAW recipe; grouped holdout and DeltaE00; cancellation/no partial success; 17/33/65 error distributions. Synthetic algorithm fixtures do not prove camera JPEG accuracy. Windows GUI visual/manual checks and clean-machine installer checks remain separate acceptance gates.
- External blockers: no supplied real RAW/JPEG/patch references means JPEG accuracy remains unverified; redistributed camera profiles/LUTs need license review. OEM RAW/JPEG pipelines, unsupported DNG features, Log/HDR LUT inputs and noncorresponding references must be explicitly unavailable until validated. CCD production may export meaningful technical or `_EXPERIMENTAL` photo looks only with documented domains/evidence; never rename camera-linear correction as an sRGB camera-look LUT. Medium-format research remains evidence classification, not invented LUT production.
