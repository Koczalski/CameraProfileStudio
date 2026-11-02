"""GUI-side adapter to the headless engine (docs/API_CONTRACT.md, v1).

This is the *only* GUI module that imports engine code.  Workspaces consume
small GUI-oriented view models; the engine's frozen dataclasses are kept in
``.raw`` fields and passed back unchanged (e.g. the exact ``Lut3D`` that was
previewed is the one exported).

Rules enforced here (contract "Payloads, capabilities ..." section):

* No colour maths in the GUI.  Strength is baked by the engine
  (``convert_profile(..., strength=s)`` / ``generate_lut(..., strength=s)``)
  and only offered when input and output spaces are equal.
* Preview uses ``images.render_preview`` on the baked LUT and is only offered
  for LUTs whose input space is encoded sRGB; camera-linear / ICC-input
  technical LUTs cannot be previewed on ordinary sRGB photos.
* Missing modules or runtimes surface as translated reasons; there is never a
  dummy/identity success.
* Every call is made on a worker thread with a ``JobContext`` carrying the
  job's cancel token and a progress forwarder.
"""

from __future__ import annotations

import csv
import importlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .jobs import CancelToken, ErrorInfo, JobCancelled, default_error_mapper, is_cancellation

LUT_SIZES = (17, 33, 65)
EXPORT_FORMATS = ("cube", "hald_png", "strip_png", "strip_png_flipped")
# File-name tag and extension per export format.
FORMAT_FILES = {"cube": ("", ".cube"), "hald_png": ("_hald", ".png"), "strip_png": ("_strip", ".png"),
                "strip_png_flipped": ("_strip_flipped", ".png")}
INTENTS = ("perceptual", "relative", "saturation", "absolute")
GAMUTS = ("clip", "error")
LOOK_MODES = ("colors_only", "tone_only", "look_only", "combined")
MATCH_FITS = ("ccm", "ccm_tone", "ccm_tone_lut")
MATCH_GRIDS = (5, 9, 17)

# Image Look Creator (docs/IMAGE_LOOK_API.md v1).  RAW references are not
# offered: the contract defines no RAW recipe for reference analysis.
IMAGE_LOOK_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
IMAGE_LOOK_MAX_REFERENCES = 64
IMAGE_LOOK_AMOUNTS = ("tone_amount", "color_amount", "saturation_amount")
IMAGE_LOOK_ROLES = ("reference", "base")
IMAGE_LOOK_SETTING_KEYS = IMAGE_LOOK_AMOUNTS + ("seed", "analysis_size", "max_dimension")

# LUT health check (docs/LUT_HEALTH_API.md).  Codes and order are the engine's.
LUT_FILE_SUFFIXES = (".cube", ".3dl", ".png", ".tif", ".tiff")
HEALTH_CODES = ("range", "tone_reversal", "gray_tint", "clipping", "tone_jump", "hue_fold", "banding_risk", "flatten")
HEALTH_SEVERITIES = ("ok", "info", "warning", "error")
HEALTH_ATTENTION = ("warning", "error")
HEALTH_SOURCE_FORMATS = ("cube", "3dl", "hald_png", "strip_png", "cube_cps")
HEALTH_LOG_CURVES = ("S-Log3", "V-Log", "F-Log2", "F-Log", "N-Log", "C-Log3", "ARRI LogC3", "Apple Log")
REPAIR_KEYS = ("smooth", "fix_jumps", "round_clip", "lock_gray", "iterations")
HUE_MAP_PER_AXIS = 8         # the hue map draws a regular 8x8x8 sub-grid (512 nodes, fewer after the neutral skip)
HUE_MAP_MIN_CHROMA = 0.03    # inputs with less Oklab chroma than this are not drawn (no hue to show)
HUE_MAP_MAX_MARKED = 64      # same cap as the engine's per-finding ``locations``

# DNG tag numbers (Adobe DNG specification 1.6, chapter 6) used only to label
# which supplied tags feed which feature; never to compute colour.
DCP_TAG_NAMES = {
    50708: "UniqueCameraModel", 50721: "ColorMatrix1", 50722: "ColorMatrix2",
    50778: "CalibrationIlluminant1", 50779: "CalibrationIlluminant2", 50936: "ProfileName",
    50937: "ProfileHueSatMapDims", 50938: "ProfileHueSatMapData1", 50939: "ProfileHueSatMapData2",
    50940: "ProfileToneCurve", 50941: "ProfileEmbedPolicy", 50942: "ProfileCopyright",
    50964: "ForwardMatrix1", 50965: "ForwardMatrix2", 50981: "ProfileLookTableDims",
    50982: "ProfileLookTableData", 51107: "ProfileHueSatMapEncoding", 51108: "ProfileLookTableEncoding",
    51109: "BaselineExposureOffset", 51110: "DefaultBlackRender",
}
TAG_USAGE = {
    "ColorMatrix": "usage.technical", "ForwardMatrix": "usage.technical", "CalibrationIlluminant": "usage.technical",
    "ProfileHueSatMap": "usage.colors", "ProfileLookTable": "usage.look", "ProfileToneCurve": "usage.tone",
    "rXYZ": "usage.technical", "gXYZ": "usage.technical", "bXYZ": "usage.technical", "wtpt": "usage.technical",
    "rTRC": "usage.technical", "gTRC": "usage.technical", "bTRC": "usage.technical", "A2B": "usage.technical",
    "B2A": "usage.technical", "chad": "usage.technical",
}

# ----------------------------------------------------------------------------
# view models
# ----------------------------------------------------------------------------


@dataclass
class Capability:
    """status: supported | partial | unsupported | available | missing |
    verified | unverified | unknown."""

    status: str
    reason: str = ""
    reason_key: str = ""


@dataclass
class ProfileInfo:
    path: str
    format_label: str
    title: str
    metadata: list[tuple[str, str]]
    structure: list[tuple[str, str, str]]
    technical: Capability
    look: Capability
    match: Capability
    jpeg: Capability
    kind: str = "icc"  # icc | dcp
    look_components: dict[str, Capability] = field(default_factory=dict)
    look_modes: dict[str, Capability] = field(default_factory=dict)
    issues: list[tuple[str, str]] = field(default_factory=list)
    dual_illuminant: bool = False  # ColorMatrix2 / CalibrationIlluminant2 present
    hsm_dual: bool = False  # ProfileHueSatMapData2 present
    technical_input: Capability | None = None  # ICC: usable as input (A2B / matrix-TRC)
    technical_output: Capability | None = None  # ICC: usable as output (B2A / invertible)
    raw: Any = None


@dataclass
class LutData:
    size: int
    title: str
    input_kind: str
    output_kind: str
    strength: float
    provenance: list[tuple[str, str]]
    raw: Any = None
    input_space: Any = None  # full engine SpaceSpec (kind + profile path)
    output_space: Any = None

    # Output kinds render_preview can show on the display: sRGB directly,
    # linear sRGB / ICC RGB through an explicit conversion to display sRGB.
    DISPLAYABLE_OUTPUTS = ("srgb", "linear_srgb", "icc_rgb")

    def preview_reason(self) -> tuple[str, dict] | None:
        """None if previewable, else (reason key, kwargs)."""
        if self.input_kind != "srgb":
            return ("reason.preview_needs_srgb_input", {"kind": self.input_kind})
        if self.output_kind not in self.DISPLAYABLE_OUTPUTS:
            return ("reason.preview_output_unsupported", {"kind": self.output_kind})
        return None

    @property
    def previewable(self) -> bool:
        return self.preview_reason() is None

    @property
    def strength_supported(self) -> bool:
        # SpaceSpec equality includes the ICC/camera profile path.
        if self.input_space is not None and self.output_space is not None:
            return self.input_space == self.output_space
        return False


@dataclass
class ImageData:
    array: np.ndarray
    path: str
    width: int
    height: int
    metadata: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: Any = None


@dataclass
class ExportInfo:
    cube_path: str  # the exported LUT file (.cube or .png)
    metadata_path: str
    sha256: str
    valid: bool
    samples: int
    warnings: list[str]


@dataclass
class MatchOutcome:
    model: Any
    lut: LutData
    before: dict[str, float]
    after: dict[str, float]
    train_count: int
    holdout_count: int
    warnings: list[str]
    evidence: str


@dataclass
class AlignmentInfo:
    source_aligned: np.ndarray
    target: np.ndarray
    valid_fraction: float
    inlier_fraction: float
    raw: Any = None


@dataclass
class ImageLookRefStats:
    """Per-reference analysis statistics exactly as reported by the engine."""

    path: str
    weight: float
    roi: tuple[float, float, float, float]
    role: str  # "reference" (defines the look) or "base" (what the look is measured against)
    sha256: str
    sample_count: int
    quantiles: tuple[float, ...]  # Oklab lightness quantiles at 1, 5, 10, 20 ... 90, 95, 99 %
    cast: tuple[tuple[float, float], ...]  # Oklab (a, b) of near-neutral pixels per lightness band (5)
    saturation: float  # mean Oklab chroma
    color_support: float  # share of hue sectors with enough colourful pixels (coverage, not accuracy)
    clipped_fraction: float
    normalization: list[tuple[str, str]]
    warnings: list[str]


@dataclass
class ImageLookAnalysisView:
    references: list[ImageLookRefStats]
    warnings: list[str]
    conflicts: list[str]
    settings: dict[str, Any]
    raw: Any = None

    @property
    def baseline(self) -> str:
        """``base_images`` when base-role images were analysed, else ``typical_photo``."""
        return "base_images" if any(r.role == "base" for r in self.references) else "typical_photo"

    def by_role(self, role: str) -> list[ImageLookRefStats]:
        return [r for r in self.references if r.role == role]


@dataclass
class ImageLookBuild:
    lut: LutData
    warnings: list[str]  # fitted-model warnings (e.g. output_clipping)
    acknowledged: bool
    analysis: Any = None  # the ImageLookAnalysisView the LUT was fitted from


# ----------------------------------------------------------------------------
# LUT health check view models (plain Python / NumPy; engine records in .raw)
# ----------------------------------------------------------------------------


@dataclass
class LoadedLutView:
    """A LUT file (or an in-app LUT) as the check sees it."""

    path: str  # file path, or "" for a LUT received from another page
    size: int
    title: str
    source_format: str
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    warnings: list[str]
    raw: Any = None  # engine LoadedLut, passed back unchanged


@dataclass
class HealthFindingView:
    code: str
    severity: str  # ok | info | warning | error
    value: float
    threshold: float
    count: int
    locations: list[tuple[int, int, int]]  # grid indices of the worst nodes
    details: list[tuple[str, str]]

    @property
    def needs_attention(self) -> bool:
        return self.severity in HEALTH_ATTENTION

    def grey_positions(self, size: int) -> list[float]:
        """Input levels (0..1) of flagged nodes that lie on the grey diagonal."""
        if size < 2:
            return []
        return [i / (size - 1) for i, j, k in self.locations if i == j == k]


@dataclass
class KindView:
    kind: str  # identity | display | log_to_display | display_to_log | unknown
    confidence: float
    log_candidates: list[tuple[str, float]]
    note_key: str


@dataclass
class HealthView:
    size: int
    findings: list[HealthFindingView]
    kind: KindView
    gray_axis: np.ndarray  # (m, 7): input, R, G, B, Oklab L, a, b
    hue_samples: np.ndarray  # (k, 6): in a, in b, out a, out b, flag bits, chroma
    overall: str
    provenance: list[tuple[str, str]]
    raw: Any = None

    def finding(self, code: str) -> HealthFindingView | None:
        return next((f for f in self.findings if f.code == code), None)

    @property
    def attention(self) -> list[HealthFindingView]:
        return [f for f in self.findings if f.needs_attention]

    def attention_codes(self) -> list[str]:
        return [f.code for f in self.attention]

    def summary(self) -> dict[str, Any]:
        """Compact JSON-safe record for the export sidecar (``health``)."""
        return {"overall": self.overall,
                "findings": [{"code": f.code, "severity": f.severity, "value": float(f.value)} for f in self.findings]}


@dataclass
class RepairView:
    loaded: LoadedLutView  # the repaired LUT
    change: tuple[float, float, float]  # DeltaE2000 mean, p99, max over the grid
    before: HealthView
    after: HealthView
    settings: dict[str, Any]


@dataclass
class ExportCheck:
    """Result of the pre-export check: never blocks the export."""

    view: HealthView | None = None
    error: str = ""  # non-empty when the check itself failed (export still ran)


class EngineUnavailable(RuntimeError):
    pass


class Unsupported(RuntimeError):
    """GUI-side precondition failure with a translation key."""

    def __init__(self, key: str, detail: str = "", **kwargs: Any) -> None:
        super().__init__(detail or key)
        self.key, self.detail, self.kwargs = key, detail, kwargs


# ----------------------------------------------------------------------------
# engine adapter
# ----------------------------------------------------------------------------


class Engine:
    PACKAGE = "camera_profile_studio"
    MODULES = ("types", "errors", "service", "icc", "lcms", "lut", "images", "dcp", "look", "matching", "image_look",
               "lut_health")
    #: Native extensions that engine functions import lazily (inside function
    #: bodies).  They must also be imported on the GUI thread; see preload().
    NATIVE_DEPENDENCIES = ("scipy.optimize", "scipy.sparse.linalg", "colour.difference", "cv2", "rawpy")

    def __init__(self) -> None:
        self.unavailable_reason = ""
        self._modules: dict[str, Any] = {}
        try:
            self._mod("types")
            self._mod("errors")
            self._mod("service")
        except EngineUnavailable as exc:
            self.unavailable_reason = str(exc)

    # -- module access -----------------------------------------------------
    def _mod(self, name: str) -> Any:
        if name not in self._modules:
            try:
                self._modules[name] = importlib.import_module(f"{self.PACKAGE}.{name}")
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                raise EngineUnavailable(f"{self.PACKAGE}.{name}: {type(exc).__name__}: {exc}") from exc
        return self._modules[name]

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    def module_status(self) -> dict[str, str]:
        status = {}
        for name in self.MODULES:
            try:
                self._mod(name)
                status[name] = ""
            except EngineUnavailable as exc:
                status[name] = str(exc)
        return status

    def preload(self) -> None:
        """Import every engine module and its native dependencies now.

        Call on the GUI (main) thread before any job is submitted.  Workers
        run on QThreadPool threads whose Python thread state PySide creates
        and destroys for every job; importing SciPy's pybind11 extensions
        lazily inside such a job left them bound to that discarded thread
        state, and the next job that called into them (e.g. a Camera Match
        tone fit after a CCM-only fit) hung in ``isotonic_regression`` and
        crashed the process.  Importing up front removes that path.
        Missing optional components (rawpy) are reported where they are used.
        """
        self.module_status()
        for name in self.NATIVE_DEPENDENCIES:
            try:
                importlib.import_module(name)
            except Exception:  # noqa: BLE001 - optional; surfaced by the feature that needs it
                pass

    def probe_lcms(self) -> str:
        return str(self._mod("lcms").probe_lcms())

    def raw_support(self) -> str:
        try:
            rawpy = importlib.import_module("rawpy")
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(f"rawpy: {exc}") from exc
        return str(getattr(rawpy, "__version__", "?"))

    def _context(self, token: CancelToken, progress: Callable[..., None] | None) -> Any:
        types = self._mod("types")
        token = token if token is not None else CancelToken()

        def forward(p: Any) -> None:
            if progress is not None and not token.cancelled:
                try:
                    progress(float(p.fraction), "progress.stage", stage=str(p.stage), message=str(p.message))
                except JobCancelled:
                    pass  # the engine's own checkpoint will observe the token

        return types.JobContext(progress=forward, cancel=token)

    # -- error mapping -----------------------------------------------------
    CODE_KEYS = {
        "missing_lcms": "error.code.missing_lcms",
        "raw_decoder_missing": "error.code.raw_decoder_missing",
        "unsupported_direction": "error.code.unsupported_direction",
        "missing_reference": "error.code.missing_reference",
        "image_look_invalid": "error.code.image_look_invalid",
        "image_look_baseline": "error.code.image_look_baseline",
        "image_look_unusable": "error.code.image_look_unusable",
        "image_look_conflict": "error.code.image_look_conflict",
        "image_look_source_changed": "error.code.image_look_source_changed",
        "image_look_source_io": "error.code.image_look_source_io",
        "cancelled": "error.cancelled",
        # 0.2.4: specific wording with a next step for the failures users actually meet
        "profile_io": "error.code.profile_io",
        "profile_too_large": "error.code.profile_too_large",
        "image_not_found": "error.code.image_not_found",
        "image_io": "error.code.image_io",
        "image_decode_error": "error.code.image_decode_error",
        "image_too_large": "error.code.image_too_large",
        "unsupported_image": "error.code.unsupported_image",
        "unsupported_image_dtype": "error.code.unsupported_image",
        "raw_decode_error": "error.code.raw_decode_error",
        "invalid_embedded_icc": "error.code.invalid_embedded_icc",
        "output_exists": "error.code.output_exists",
        "output_permission": "error.code.output_permission",
        "output_dir_missing": "error.code.output_dir_missing",
        "disk_full": "error.code.disk_full",
        "path_too_long": "error.code.path_too_long",
        "invalid_export_path": "error.code.invalid_export_path",
        "export_io_error": "error.code.export_io_error",
        "partial_publication": "error.code.partial_publication",
        "invalid_cube": "error.code.invalid_cube",
        # LUT health check (0.3.0)
        "lut_unreadable": "error.code.lut_unreadable",
        "lut_1d_only": "error.code.lut_1d_only",
        "lut_png_layout": "error.code.lut_png_layout",
        "lut_size_unsupported": "error.code.lut_size_unsupported",
    }
    CLASS_KEYS = {
        "InvalidProfileError": "error.invalid_profile",
        "UnsupportedError": "error.unsupported",
        "ValidationError": "error.validation",
        "DependencyError": "error.dependency",
        "ImageError": "error.image",
        "ExportError": "error.export",
        "CancelledError": "error.cancelled",
        "StudioError": "error.engine",
    }

    def map_error(self, exc: BaseException) -> ErrorInfo:
        if isinstance(exc, Unsupported):
            return ErrorInfo(exc.key, exc.kwargs, exc.detail)
        if isinstance(exc, EngineUnavailable):
            return ErrorInfo("error.engine_unavailable", detail=str(exc))
        code = getattr(exc, "code", None)
        if isinstance(code, str) and hasattr(exc, "message"):
            details = getattr(exc, "details", ()) or ()
            detail = str(getattr(exc, "message", "") or exc)
            if details:
                detail += " (" + ", ".join(f"{k}={v}" for k, v in details) + ")"
            key = self.CODE_KEYS.get(code)
            if key is None:
                for cls in type(exc).__mro__:
                    if cls.__name__ in self.CLASS_KEYS:
                        key = self.CLASS_KEYS[cls.__name__]
                        break
            return ErrorInfo(key or "error.engine", {"code": code}, detail)
        if isinstance(exc, (FileNotFoundError, PermissionError, IsADirectoryError, OSError)):
            return ErrorInfo("error.file", detail=str(exc))
        if isinstance(exc, MemoryError):
            return ErrorInfo("error.memory", detail=str(exc))
        return default_error_mapper(exc)

    # -- inspection --------------------------------------------------------
    def inspect(self, path: str, token: CancelToken, progress: Callable[..., None] | None = None) -> ProfileInfo:
        service = self._mod("service")
        info = service.inspect_profile(Path(path), context=self._context(token, progress))
        return self.profile_view(info)

    def profile_view(self, info: Any) -> ProfileInfo:
        caps = {c.feature: c for c in info.capabilities}

        def cap(feature: str, default: str) -> Capability:
            c = caps.get(feature)
            if c is None:
                return Capability(default, reason_key="reason.capability_not_reported")
            return Capability(str(c.status), "\n".join(str(r) for r in c.reasons))

        kind = str(info.format)
        tags = [(str(k), str(v)) for k, v in info.tags]
        tag_names = [k for k, _ in tags]
        version = str(info.version)
        fmt = f"ICC v{version}" if kind == "icc" else "DCP"
        metadata = [
            ("meta.format", fmt),
            ("meta.description", str(info.description)),
            ("meta.device_class", str(info.device_class)),
            ("meta.color_space", str(info.color_space)),
            ("meta.pcs", str(info.pcs)),
            ("meta.version", version),
            ("meta.path", str(info.path)),
            ("meta.sha256", str(info.sha256)),
        ]
        structure = []
        for name, value in tags:
            usage = next((u for prefix, u in TAG_USAGE.items() if name.startswith(prefix)), "usage.other")
            short = value if len(value) <= 300 else value[:297] + "..."
            structure.append((name, short, usage))
        look_modes = {m: cap(f"look.{m}", "unsupported") for m in LOOK_MODES}
        components: dict[str, Capability] = {}
        if kind == "dcp":
            def present(*prefixes: str) -> Capability:
                found = [n for n in tag_names if any(n.startswith(p) for p in prefixes)]
                return Capability("available" if found else "missing", ", ".join(found))

            components = {
                "matrices": present("ColorMatrix", "ForwardMatrix"),
                "hue_sat_map": present("ProfileHueSatMap"),
                "look_table": present("ProfileLookTable"),
                "tone_curve": present("ProfileToneCurve"),
            }
        issues = [("warning", str(w)) for w in info.warnings]
        for feature in ("technical", "look"):
            c = caps.get(feature)
            if c is not None and c.status in ("unsupported", "partial"):
                issues.extend(("info", f"{feature}: {r}") for r in c.reasons)
        dual = any(n.startswith("CalibrationIlluminant2") or n.startswith("ColorMatrix2") for n in tag_names)
        hsm_dual = "ProfileHueSatMapData2" in tag_names
        return ProfileInfo(
            path=str(info.path),
            format_label=fmt,
            title=str(info.description) or Path(str(info.path)).name,
            metadata=metadata,
            structure=structure,
            technical=cap("technical", "unsupported"),
            look=cap("look", "unsupported"),
            match=cap("match_reference", "missing"),
            jpeg=cap("jpeg_accuracy", "unverified"),
            kind=kind,
            look_components=components,
            look_modes=look_modes,
            issues=issues,
            dual_illuminant=dual,
            hsm_dual=hsm_dual,
            technical_input=Capability(str(caps["technical.input"].status), "\n".join(caps["technical.input"].reasons))
            if "technical.input" in caps else None,
            technical_output=Capability(str(caps["technical.output"].status), "\n".join(caps["technical.output"].reasons))
            if "technical.output" in caps else None,
            raw=info,
        )

    # -- requests ------------------------------------------------------------
    def space(self, kind: str, profile_path: str | None = None) -> Any:
        types = self._mod("types")
        needs_path = kind in ("icc_rgb", "camera_linear")
        return types.SpaceSpec(kind, Path(profile_path) if needs_path and profile_path else None)

    def technical_request(self, profile: ProfileInfo, s: dict) -> Any:
        """Build a TransformRequest for the Technical Converter.

        ``s`` keys: direction (to_srgb | to_linear_srgb | from_srgb | to_icc),
        target (output ICC path for to_icc), intent, bpc, gamut, neutral
        (tuple|None), kelvin (float|None), hsm, lt, tc.
        """
        types = self._mod("types")
        path = profile.path
        if profile.kind == "dcp":
            if s.get("direction") != "to_srgb":
                raise Unsupported("reason.dcp_output_srgb_only")
            neutral = s.get("neutral")
            if not neutral or any(v <= 0 for v in neutral):
                raise Unsupported("reason.dcp_needs_neutral")
            if profile.dual_illuminant and not s.get("kelvin"):
                raise Unsupported("reason.dcp_needs_kelvin")
            dcp = types.DcpSettings(
                camera_neutral=tuple(float(v) for v in neutral),
                illuminant_kelvin=float(s["kelvin"]) if s.get("kelvin") else None,
                gamut=s.get("gamut", "clip"),
                include_hue_sat_map=bool(s.get("hsm", True)),
                include_look_table=bool(s.get("lt", True)),
                include_tone_curve=bool(s.get("tc", True)),
            )
            return types.TransformRequest(
                input_space=self.space("camera_linear", path), output_space=self.space("srgb"),
                mode="technical", profile_path=Path(path), gamut=s.get("gamut", "clip"), dcp=dcp,
            )
        direction = s.get("direction", "to_srgb")
        if direction == "from_srgb":
            inp, out = self.space("srgb"), self.space("icc_rgb", path)
        elif direction == "to_icc":
            target = s.get("target")
            if not target:
                raise Unsupported("reason.need_target_profile")
            inp, out = self.space("icc_rgb", path), self.space("icc_rgb", target)
        else:
            inp = self.space("icc_rgb", path)
            out = self.space("linear_srgb" if direction == "to_linear_srgb" else "srgb")
        return types.TransformRequest(
            input_space=inp, output_space=out, mode="technical", profile_path=Path(path),
            intent=s.get("intent", "relative"), black_point_compensation=bool(s.get("bpc", False)),
            gamut=s.get("gamut", "clip"),
        )

    @staticmethod
    def look_needs_kelvin(profile: ProfileInfo, mode: str, include_hsm: bool) -> bool:
        """Dual HueSatMap interpolation needs an explicit Kelvin value; modes
        or settings that do not use HueSatMap do not."""
        return profile.hsm_dual and include_hsm and mode in ("colors_only", "combined")

    def look_request(self, profile: ProfileInfo, mode: str, s: dict) -> Any:
        types = self._mod("types")
        if profile.kind != "dcp":
            raise Unsupported("reason.look_dcp_only")
        include_hsm = bool(s.get("hsm", True))
        if self.look_needs_kelvin(profile, mode, include_hsm) and not s.get("kelvin"):
            raise Unsupported("reason.dcp_needs_kelvin")
        dcp = types.DcpSettings(
            illuminant_kelvin=float(s["kelvin"]) if s.get("kelvin") else None,
            gamut=s.get("gamut", "clip"),
            include_hue_sat_map=include_hsm,
            include_look_table=bool(s.get("lt", True)),
            include_tone_curve=bool(s.get("tc", True)),
        )
        return types.TransformRequest(
            input_space=self.space("srgb"), output_space=self.space("srgb"), mode="look",
            profile_path=Path(profile.path), gamut=s.get("gamut", "clip"), dcp=dcp, look_mode=mode,
        )

    @staticmethod
    def strength_allowed(request: Any) -> bool:
        return request.input_space == request.output_space

    def convert(self, request: Any, size: int, strength: float, token: CancelToken,
                progress: Callable[..., None] | None = None) -> LutData:
        if size not in LUT_SIZES:
            raise Unsupported("reason.bad_size")
        if strength != 1.0 and not self.strength_allowed(request):
            raise Unsupported("reason.strength_needs_equal_spaces")
        service = self._mod("service")
        lut = service.convert_profile(request, size=size, strength=float(strength), context=self._context(token, progress))
        return self.lut_view(lut)

    def lut_view(self, lut: Any) -> LutData:
        return LutData(
            size=int(lut.values.shape[0]),
            title=str(lut.title),
            input_kind=str(lut.input_space.kind),
            output_kind=str(lut.output_space.kind),
            strength=float(lut.strength),
            provenance=[(str(k), str(v)) for k, v in lut.provenance],
            raw=lut,
            input_space=lut.input_space,
            output_space=lut.output_space,
        )

    # -- images / preview -------------------------------------------------------
    def load_image(self, path: str, max_side: int | None, token: CancelToken,
                   progress: Callable[..., None] | None = None, raw_recipe: Any = None) -> ImageData:
        images = self._mod("images")
        data = images.load_image(Path(path), max_dimension=max_side, raw_recipe=raw_recipe,
                                 context=self._context(token, progress))
        return self.image_view(data, path)

    def image_view(self, data: Any, path: str = "") -> ImageData:
        rgb = np.asarray(data.rgb)
        return ImageData(rgb, str(data.source_path or path), int(rgb.shape[1]), int(rgb.shape[0]),
                         [(str(k), str(v)) for k, v in data.metadata], [str(w) for w in data.warnings], data)

    def raw_recipe(self, use_camera_wb: bool = True, exposure_shift: float = 1.0,
                   user_wb: tuple[float, float, float, float] | None = None) -> Any:
        """Fixed RAW development recipe: camera WB, or explicit user WB
        multipliers (R, G, B, G2) when camera WB is off.  Never guessed."""
        return self._mod("types").RawRecipe(use_camera_wb=use_camera_wb, exposure_shift=exposure_shift,
                                            user_wb=None if use_camera_wb else user_wb)

    def render_preview(self, image: ImageData, lut: LutData, token: CancelToken) -> np.ndarray:
        reason = lut.preview_reason()
        if reason is not None:
            raise Unsupported(reason[0], **reason[1])
        images = self._mod("images")
        out = images.render_preview(image.raw, lut.raw, context=self._context(token, None))
        return np.asarray(out.rgb)

    def png_layout(self, fmt: str, size: int) -> dict:
        """Geometry of a PNG LUT format for a built LUT size (for hints)."""
        return dict(self._mod("lut").png_layout(fmt, size))

    def export_supports_extra_metadata(self) -> bool:
        """Feature check: does ``lut.export_lut`` accept ``extra_metadata`` (0.3.0)?"""
        try:
            return "extra_metadata" in inspect.signature(self._mod("lut").export_lut).parameters
        except (EngineUnavailable, TypeError, ValueError):
            return False

    def export(self, lut: LutData, path: str, overwrite: bool, token: CancelToken, fmt: str = "cube",
               health: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> ExportInfo:
        """Write the LUT and its JSON sidecar.  ``health`` (``HealthView.summary()``) and ``extra`` go
        into the sidecar through ``extra_metadata`` when the engine supports it."""
        lut_mod = self._mod("lut")
        meta: dict[str, Any] = {}
        if health:
            meta["health"] = health
        if extra:
            meta.update(extra)
        kwargs: dict[str, Any] = {}
        if meta and self.export_supports_extra_metadata():
            kwargs["extra_metadata"] = meta
        result = lut_mod.export_lut(lut.raw, Path(path), format=fmt, overwrite=overwrite,
                                    context=self._context(token, None), **kwargs)
        v = result.validation
        if not v.valid:
            # defensive: contract says invalid LUTs are never published
            raise Unsupported("error.export_invalid", "; ".join(v.errors))
        return ExportInfo(str(result.cube_path), str(result.metadata_path), str(result.sha256), bool(v.valid),
                          int(v.sample_count), [str(w) for w in v.warnings])

    # -- LUT health check (docs/LUT_HEALTH_API.md) -------------------------------
    def health_status(self) -> str:
        """Empty string if the lut_health engine module imports, else the reason."""
        try:
            self._mod("lut_health")
        except EngineUnavailable as exc:
            return str(exc)
        return ""

    def loaded_view(self, loaded: Any, path: str | None = None) -> LoadedLutView:
        """View of an engine ``LoadedLut``; ``path`` "" marks a LUT that came from another page."""
        return LoadedLutView(
            path=str(loaded.source_path) if path is None else path,
            size=int(loaded.size), title=str(loaded.title), source_format=str(loaded.source_format),
            domain_min=tuple(float(v) for v in loaded.domain_min), domain_max=tuple(float(v) for v in loaded.domain_max),
            warnings=[str(w) for w in loaded.warnings], raw=loaded)

    def load_lut_file(self, path: str, token: CancelToken, progress: Callable[..., None] | None = None) -> LoadedLutView:
        """Read a .cube / .3dl / Hald or strip PNG / TIFF LUT of any grid size 2..129."""
        mod = self._mod("lut_health")
        return self.loaded_view(mod.load_lut_file(Path(path), context=self._context(token, progress)))

    def hue_nodes(self, loaded: LoadedLutView, locations: Any = ()) -> tuple[np.ndarray, np.ndarray]:
        """Arrows for the hue map in the Oklab a/b plane: ``(base, marked)``.

        ``base``: at most 8x8x8 regularly spaced grid nodes of the LUT with near-neutral inputs skipped
        (so the plane stays readable).  ``marked``: exactly the given grid indices (the selected
        finding's ``locations``, at most 64).  Rows are ``(in_a, in_b, out_a, out_b)``; non-finite LUT
        values are replaced by the identity, as the engine does when it diagnoses.
        """
        raw = loaded.raw
        values = np.asarray(raw.values, dtype=np.float64)
        n = int(values.shape[0])
        to_lab = self._mod("image_look").srgb_to_oklab

        def arrows(index: np.ndarray) -> np.ndarray:
            index = np.asarray(index, dtype=np.intp).reshape(-1, 3)
            if not len(index):
                return np.zeros((0, 4))
            grid = index / max(1, n - 1)
            out = values[index[:, 0], index[:, 1], index[:, 2]]
            out = np.where(np.isfinite(out), out, grid)
            lab_in = np.asarray(to_lab(np.clip(grid, 0.0, 1.0)), dtype=np.float64).reshape(-1, 3)
            lab_out = np.asarray(to_lab(np.clip(out, 0.0, 1.0)), dtype=np.float64).reshape(-1, 3)
            return np.column_stack([lab_in[:, 1], lab_in[:, 2], lab_out[:, 1], lab_out[:, 2]])

        axis = np.unique(np.round(np.linspace(0, n - 1, min(n, HUE_MAP_PER_AXIS))).astype(np.intp))
        base_index = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3)
        base = arrows(base_index)
        base = base[np.hypot(base[:, 0], base[:, 1]) >= HUE_MAP_MIN_CHROMA]
        marked_index = [tuple(int(v) for v in loc) for loc in list(locations)[:HUE_MAP_MAX_MARKED]]
        marked_index = [loc for loc in marked_index if all(0 <= v < n for v in loc)]
        return base, arrows(np.asarray(marked_index, dtype=np.intp))

    def health_flag_bits(self) -> dict[str, int]:
        """Node flag bits of ``HealthReport.hue_samples[:, 4]`` per finding code (engine constant)."""
        try:
            return {str(k): int(v) for k, v in self._mod("lut_health").FLAG_BITS.items()}
        except (EngineUnavailable, AttributeError, TypeError, ValueError):
            return {}

    def lut_to_loaded(self, lut: LutData) -> LoadedLutView:
        """Wrap an in-app LUT so it can be diagnosed or repaired."""
        return self.loaded_view(self._mod("lut_health").from_lut3d(lut.raw), path="")

    def health_view(self, report: Any) -> HealthView:
        findings = [
            HealthFindingView(
                str(f.code), str(f.severity), float(f.value), float(f.threshold), int(f.count),
                [(int(a), int(b), int(c)) for a, b, c in np.asarray(f.locations).reshape(-1, 3)],
                [(str(k), str(v)) for k, v in f.details])
            for f in report.findings]
        k = report.kind
        kind = KindView(str(k.kind), float(k.confidence), [(str(n), float(s)) for n, s in k.log_candidates], str(k.note_key))
        return HealthView(int(report.size), findings, kind, np.asarray(report.gray_axis, dtype=np.float64),
                          np.asarray(report.hue_samples, dtype=np.float64), str(report.overall),
                          [(str(a), str(b)) for a, b in report.provenance], report)

    def diagnose(self, lut: Any, token: CancelToken | None = None,
                 progress: Callable[..., None] | None = None) -> HealthView:
        """Diagnose a ``LutData``, ``LoadedLutView`` or engine ``Lut3D`` / ``LoadedLut`` on its own grid."""
        mod = self._mod("lut_health")
        raw = lut.raw if isinstance(lut, (LutData, LoadedLutView)) else lut
        return self.health_view(mod.diagnose_lut(raw, context=self._context(token, progress)))

    def diagnose_for_export(self, lut: LutData, token: CancelToken | None) -> ExportCheck:
        """Pre-export check.  Never raises (except cancellation): a failed or
        unavailable check must not block the export."""
        try:
            self._mod("lut_health")
        except EngineUnavailable:
            return ExportCheck()
        try:
            return ExportCheck(self.diagnose(lut, token))
        except BaseException as exc:  # noqa: BLE001 - reported in the log, export continues
            if is_cancellation(exc):
                raise
            info = self.map_error(exc)
            return ExportCheck(error=(info.summary() + (f" {info.detail}" if info.detail else "")).strip())

    def repair(self, loaded: LoadedLutView, settings: dict[str, Any], token: CancelToken,
               progress: Callable[..., None] | None = None) -> RepairView:
        """Explicit repair (never automatic).  ``settings`` keys: :data:`REPAIR_KEYS`."""
        mod = self._mod("lut_health")
        unknown = set(settings) - set(REPAIR_KEYS)
        if unknown:
            raise Unsupported("error.validation", ", ".join(sorted(unknown)))
        result = mod.repair_lut(loaded.raw, mod.RepairSettings(**settings), context=self._context(token, progress))
        return RepairView(self.loaded_view(result.lut, path=loaded.path), tuple(float(v) for v in result.change),
                          self.health_view(result.before), self.health_view(result.after), dict(settings))

    def loaded_to_lutdata(self, loaded: LoadedLutView, size: int, token: CancelToken | None = None,
                          progress: Callable[..., None] | None = None) -> LutData:
        """Resample to 17/33/65 (tetrahedral) and clip, as an sRGB -> sRGB ``LutData`` for preview and export."""
        if size not in LUT_SIZES:
            raise Unsupported("reason.bad_size")
        lut = self._mod("lut_health").to_lut3d(loaded.raw, size, context=self._context(token, progress))
        return self.lut_view(lut)

    # -- matching --------------------------------------------------------------
    def read_patch_csv(self, path: str, evidence: str) -> Any:
        """Measured corresponding patches -> SamplePairs.

        Columns (header required): source_r, source_g, source_b, target_r,
        target_g, target_b, group.  Values are encoded sRGB in [0, 1].  No
        values are synthesised; malformed rows reject the whole file.
        """
        types = self._mod("types")
        required = ("source_r", "source_g", "source_b", "target_r", "target_g", "target_b", "group")
        src, tgt, groups = [], [], []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            fields = [f.strip().lower() for f in (reader.fieldnames or [])]
            missing = [c for c in required if c not in fields]
            if missing:
                raise Unsupported("error.csv_columns", ", ".join(missing))
            for lineno, row in enumerate(reader, start=2):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                try:
                    s = [float(row[c]) for c in required[:3]]
                    t = [float(row[c]) for c in required[3:6]]
                except ValueError as exc:
                    raise Unsupported("error.csv_value", f"line {lineno}: {exc}") from exc
                values = s + t
                if not all(np.isfinite(values)) or min(values) < 0.0 or max(values) > 1.0:
                    raise Unsupported("error.csv_range", f"line {lineno}")
                if not row["group"]:
                    raise Unsupported("error.csv_group", f"line {lineno}")
                src.append(s)
                tgt.append(t)
                groups.append(row["group"])
        if not src:
            raise Unsupported("error.csv_empty", path)
        if not evidence.strip():
            raise Unsupported("reason.match_needs_evidence")
        return types.SamplePairs(
            np.asarray(src, dtype=np.float32), np.asarray(tgt, dtype=np.float32), tuple(groups),
            f"{evidence.strip()} [patch CSV: {path}]",
        )

    def align(self, source: ImageData, target: ImageData, token: CancelToken,
              progress: Callable[..., None] | None = None) -> AlignmentInfo:
        matching = self._mod("matching")
        res = matching.align_pair(source.raw, target.raw, same_scene_confirmed=True,
                                  context=self._context(token, progress))
        mask = np.asarray(res.valid_mask)
        return AlignmentInfo(np.asarray(res.source_aligned), np.asarray(res.target_rgb),
                             float(mask.mean()) if mask.size else 0.0, float(res.inlier_fraction), res)

    def sample_pair(self, source: ImageData, target: ImageData, alignment: AlignmentInfo | None,
                    token: CancelToken, progress: Callable[..., None] | None = None) -> Any:
        matching = self._mod("matching")
        types = self._mod("types")
        src = source.raw
        tgt = target.raw
        mask = None
        if alignment is not None:
            src = types.ImageData(alignment.raw.source_aligned, source.raw.source_path,
                                  tuple(source.raw.metadata) + (("registration", "homography"),), source.raw.warnings)
            tgt = types.ImageData(alignment.raw.target_rgb, target.raw.source_path, target.raw.metadata, target.raw.warnings)
            mask = alignment.raw.valid_mask
        return matching.sample_image_pair(src, tgt, same_scene_confirmed=True, valid_mask=mask,
                                          context=self._context(token, progress))

    def fit(self, samples: Any, fit: str, regularization: float, grid: int, holdout: float, seed: int,
            size: int, strength: float, token: CancelToken, progress: Callable[..., None] | None = None) -> MatchOutcome:
        matching = self._mod("matching")
        types = self._mod("types")
        lut_mod = self._mod("lut")
        settings = types.MatchSettings(fit=fit, regularization=float(regularization), grid_size=int(grid),
                                       holdout_fraction=float(holdout), seed=int(seed))
        ctx = self._context(token, progress)
        model = matching.fit_match(samples, settings=settings, context=ctx)
        transform = matching.create_match_transform(model)
        try:
            lut = lut_mod.generate_lut(transform, size=size, strength=float(strength),
                                       title="Camera Profile Studio match", context=ctx)
        finally:
            close = getattr(transform, "close", None)
            if close:
                close()
        rep = model.report

        def stats(e: Any) -> dict[str, float]:
            return {"count": e.count, "mean": e.mean, "median": e.median, "p95": e.p95, "max": e.maximum}

        return MatchOutcome(model, self.lut_view(lut), stats(rep.before), stats(rep.after),
                            int(len(rep.train_indices)), int(len(rep.holdout_indices)),
                            [str(w) for w in rep.warnings], str(model.evidence))

    # -- image look creator (reference statistics vs. base statistics) ---------
    def image_look_status(self) -> str:
        """Empty string if the image_look engine module imports, else the reason."""
        try:
            self._mod("image_look")
        except EngineUnavailable as exc:
            return str(exc)
        return ""

    def analyze_image_look(self, references: list[tuple],
                           settings: dict[str, Any], token: CancelToken,
                           progress: Callable[..., None] | None = None) -> ImageLookAnalysisView:
        """``analyze_image_look`` on (path, weight, roi[, role]) tuples.

        ``role`` defaults to ``reference``; ``base`` images define what the
        look is measured against.  ``settings`` may hold only
        :data:`IMAGE_LOOK_SETTING_KEYS`; omitted keys take the engine
        defaults.  Conflicts are returned, not raised.
        """
        il = self._mod("image_look")
        unknown = set(settings) - set(IMAGE_LOOK_SETTING_KEYS)
        if unknown:
            raise Unsupported("error.validation", ", ".join(sorted(unknown)))
        refs = []
        for entry in references:
            p, w, roi = entry[:3]
            role = entry[3] if len(entry) > 3 else "reference"
            if role not in IMAGE_LOOK_ROLES:
                raise Unsupported("error.validation", str(role))
            refs.append(il.ImageLookReference(path=Path(p), weight=float(w), roi=tuple(float(v) for v in roi), role=role))
        refs = tuple(refs)
        s = il.ImageLookSettings(**settings)
        analysis = il.analyze_image_look(refs, settings=s, context=self._context(token, progress))
        return self.image_look_view(analysis)

    def image_look_view(self, analysis: Any) -> ImageLookAnalysisView:
        refs = []
        for r in analysis.references:
            ref = r.reference
            q = tuple(float(v) for v in np.asarray(r.stats.l_quantiles).ravel())
            cast = tuple(tuple(float(v) for v in row) for row in np.asarray(r.stats.cast).reshape(-1, 2))
            refs.append(ImageLookRefStats(
                path=str(ref.path), weight=float(ref.weight), roi=tuple(float(v) for v in ref.roi),
                role=str(getattr(ref, "role", "reference")),
                sha256=str(r.source_sha256), sample_count=int(r.sample_count), quantiles=q, cast=cast,
                saturation=float(r.saturation), color_support=float(r.color_support),
                clipped_fraction=float(r.clipped_fraction),
                normalization=[(str(k), str(v)) for k, v in r.normalization],
                warnings=[str(w) for w in r.warnings]))
        s = analysis.settings
        settings = {k: getattr(s, k) for k in ("baseline_version",) + IMAGE_LOOK_SETTING_KEYS + ("acknowledge_conflicts",)
                    if hasattr(s, k)}
        return ImageLookAnalysisView(refs, [str(w) for w in analysis.warnings], [str(c) for c in analysis.conflicts],
                                     settings, analysis)

    def build_image_look_lut(self, analysis: ImageLookAnalysisView, acknowledge_conflicts: bool, size: int,
                             strength: float, token: CancelToken,
                             progress: Callable[..., None] | None = None) -> ImageLookBuild:
        """fit -> transform -> ``generate_lut`` (overall strength baked once).

        ``acknowledge_conflicts=True`` is passed only after the user's
        explicit action; otherwise the engine default applies and an
        unresolved conflict is refused by ``fit_image_look``.
        """
        if size not in LUT_SIZES:
            raise Unsupported("reason.bad_size")
        il = self._mod("image_look")
        lut_mod = self._mod("lut")
        ctx = self._context(token, progress)
        kwargs = {"acknowledge_conflicts": True} if acknowledge_conflicts else {}
        model = il.fit_image_look(analysis.raw, context=ctx, **kwargs)
        transform = il.create_image_look_transform(model)
        try:
            lut = lut_mod.generate_lut(transform, size=size, strength=float(strength),
                                       title="Camera Profile Studio image look (approximate)", context=ctx)
        finally:
            close = getattr(transform, "close", None)
            if close:
                close()
        return ImageLookBuild(self.lut_view(lut), [str(w) for w in model.warnings], bool(acknowledge_conflicts), analysis)

    def regenerate_match_lut(self, model: Any, size: int, strength: float, token: CancelToken) -> LutData:
        matching = self._mod("matching")
        lut_mod = self._mod("lut")
        transform = matching.create_match_transform(model)
        try:
            lut = lut_mod.generate_lut(transform, size=size, strength=float(strength),
                                       title="Camera Profile Studio match", context=self._context(token, None))
        finally:
            close = getattr(transform, "close", None)
            if close:
                close()
        return self.lut_view(lut)


def to_qimage(array: np.ndarray):
    """Encoded-sRGB float [0,1] HxWx3 -> QImage for the screen (worker-safe).

    8-bit quantisation happens only here, for display; it never feeds LUT
    generation, preview sampling or export.
    """
    from PySide6.QtGui import QImage

    arr = np.asarray(array, dtype=np.float32)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError("expected HxWx3 image")
    rgb8 = np.ascontiguousarray((np.clip(np.nan_to_num(arr[..., :3]), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8))
    h, w, _ = rgb8.shape
    image = QImage(rgb8.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return image.copy()


