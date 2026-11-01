# Image Look Creator API v2 (engine `image-look-v2`, 0.2.5)

Headless module: `camera_profile_studio.image_look`. This is a separate, approximate,
unverified global encoded-sRGB -> encoded-sRGB look estimator. It never constructs
SamplePairs, reports reference DeltaE accuracy, or needs a before image. The look is
measured in Oklab as a difference between two sets of colour statistics:

- `reference` images: finished photos whose look is wanted (1..64).
- `base` images: the user's own photos the look will be applied to (0..64).

Look = statistics of the reference images minus statistics of the base. With no base
image a built-in "typical photo" baseline is used instead (less accurate, flagged by the
warning `typical_photo_baseline`). Multiple references produce ONE common look, equal
image weights by default. No pixel correspondence, scene, camera, local, HDR or Log
reconstruction is claimed; the look is a statistical match of distributions.

v2 replaces the v1 RGB model (`cps-designed-prior-v1`). Differences that affect callers:
`ImageLookReference.role` and `ImageLookModel.baseline` are new; `neutral_protection`
is removed (there is no replacement setting); `analysis_size` defaults to 160; the model
fields `tone_coefficients`, `band_bias` and `saturation_scale` are replaced by the fields
listed below; `luminance_quantiles` has 13 entries and the per-image statistics are in
`ImageLookReferenceAnalysis.stats`.

## Public records and functions
All records are frozen, slotted dataclasses. Arrays are owned immutable NumPy
float64 buffers; sequence inputs are converted to tuples. Invalid input raises
ValidationError, never a successful dummy identity. Paths accept Path or str.

```python
from pathlib import Path
import numpy as np
from camera_profile_studio.types import JobContext, RGB, RGBTransform

BASELINE_VERSION = 'cps-distribution-v2'
ROLES = ('reference', 'base')

@dataclass(frozen=True, slots=True)
class ImageLookReference:
    path: Path
    weight: float = 1.0                 # finite, strictly positive
    roi: tuple[float, float, float, float] = (0., 0., 1., 1.) # x0,y0,x1,y1 normalized
    role: str = 'reference'             # 'reference' | 'base'; anything else is image_look_invalid

@dataclass(frozen=True, slots=True)
class ImageLookSettings:
    baseline_version: str = BASELINE_VERSION
    seed: int = 42                      # 0..2**32-1
    analysis_size: int = 160            # square stratified sample grid, 16..256
    max_dimension: int = 2048           # shared loader resize limit, 256..4096
    tone_amount: float = 1.0            # each independent amount 0..1
    color_amount: float = 1.0
    saturation_amount: float = 1.0
    acknowledge_conflicts: bool = False

@dataclass(frozen=True, slots=True)
class LookStatistics:                   # Oklab statistics of one image or one weighted pool
    l_quantiles: np.ndarray             # (13,) lightness at p = .01 .05 .1 .2 .3 .4 .5 .6 .7 .8 .9 .95 .99
    cast: np.ndarray                    # (5, 2) mean Oklab (a, b) of near-neutral pixels per lightness band
    cast_mass: np.ndarray               # (5,) share of samples behind each cast estimate
    chroma: np.ndarray                  # (5,) mean chroma (cast removed) per lightness band
    chroma_mass: np.ndarray             # (5,) share of samples in each band
    sector_chroma: np.ndarray           # (12,) mean chroma of colourful pixels per hue sector
    sector_mass: np.ndarray             # (12,) share of samples that are colourful, per sector
    mean_chroma: float
    clipped_fraction: float

@dataclass(frozen=True, slots=True)
class ImageLookReferenceAnalysis:       # one record per image, reference or base
    reference: ImageLookReference       # carries path, weight, roi and role
    source_sha256: str
    sample_count: int                   # effective unique sampled pixel count
    stats: LookStatistics
    normalization: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...] = ()      # machine-readable warning codes
    # Display properties: luminance_quantiles, saturation (= mean chroma),
    # color_support (0..1 share of hue sectors with enough colourful pixels;
    # coverage, NOT accuracy), clipped_fraction.

@dataclass(frozen=True, slots=True)
class ImageLookAnalysis:
    references: tuple[ImageLookReferenceAnalysis, ...] # deterministic canonical order, both roles
    settings: ImageLookSettings
    warnings: tuple[str, ...]
    conflicts: tuple[str, ...]          # conflict codes, not accuracy metrics
    # property base_count: number of records with role 'base'

@dataclass(frozen=True, slots=True)
class ImageLookModel:
    tone_x: np.ndarray                  # (15,) increasing base-lightness knots, 0 .. 1
    tone_y: np.ndarray                  # (15,) non-decreasing reference-lightness knots
    cast: np.ndarray                    # (5, 2) Oklab (a, b) shift per lightness band, |.| <= .08
    band_gain: np.ndarray               # (5,) chroma gain per lightness band, .5 .. 2
    sector_gain: np.ndarray             # (12,) relative chroma gain per hue sector, .6 .. 1.6
    baseline: str                       # 'base_images' | 'typical_photo'
    settings: ImageLookSettings
    warnings: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]

def analyze_image_look(references: tuple[ImageLookReference, ...], *,
                       settings: ImageLookSettings = ImageLookSettings(),
                       context: JobContext | None = None) -> ImageLookAnalysis: ...
def fit_image_look(analysis: ImageLookAnalysis, *,
                   acknowledge_conflicts: bool | None = None,
                   context: JobContext | None = None) -> ImageLookModel: ...
def apply_image_look(rgb: RGB, model: ImageLookModel, *,
                     context: JobContext | None = None) -> RGB: ...
def create_image_look_transform(model: ImageLookModel) -> RGBTransform: ...
```

Helpers `srgb_to_oklab`, `oklab_to_srgb`, `look_statistics(rgb_samples)` and
`pool_statistics(records, weights)` are public for diagnostics and tests. A model whose
coefficients exceed the limits above (even by one ulp) is rejected by its constructor.

## Limits and sampling
`analyze_image_look` takes at most 64 images with role `reference` (at least 1) and at
most 64 with role `base` (128 in total). A request without any `reference` image is
`image_look_invalid`, even if base images are present. Analysis consumes files
sequentially, retaining only bounded statistics, hashes, and normalization provenance; no
retained full images. The shared loader may decode one full-resolution image before
resizing. ROI applies to the orientation-correct, normalized, resized sRGB image.
Quantization is floor for lower ROI bounds and ceil for upper bounds. Stratified
normalized spatial sampling (`analysis_size` x `analysis_size` seeded positions) uses the
same seeded positions for each image, independent of file order, so every image
contributes the same number of samples regardless of resolution. Weights are normalized
per image, never by pixel count. Fit settings/amounts are frozen with analysis: rerun
analysis after editing controls. Conflicts may instead be explicitly acknowledged in fit;
the effective acknowledgement is recorded.

Warnings of `base` images stay on their own analysis record and in the provenance. Only
`reference` images contribute to `ImageLookAnalysis.warnings`, and conflicts are checked
between `reference` images only; a base photo can differ freely from the references.

## GUI worker sequence
1. Build references (role `reference`), optional base images (role `base`) and settings;
   call `analyze_image_look` in a worker JobContext.
2. Show warnings and conflicts. Fit refuses unresolved conflicts. Ask for explicit
   acknowledgement or change references; never silently average statistical conflicts.
   Conflicts mean measured distribution disagreement, not identified editing styles.
   Show `typical_photo_baseline` as a recommendation to add base images.
3. Call `fit_image_look(analysis, acknowledge_conflicts=True)` only after that action
   (omit override normally). Call `create_image_look_transform(model)`.
4. Call existing `generate_lut(transform, size=17|33|65, strength=overall_strength)`;
   close transform in finally. Overall strength exists ONLY in generate_lut, not
   ImageLookSettings. Model component amounts are independent, not overall strength.
5. Preview with existing `render_preview(image, lut)` and export the SAME baked LUT
   with `export_cube(lut, path, overwrite=False)`. Never blend preview again.

All stages checkpoint cancellation. No Qt imports or network/native new dependency.
Transform carries `provenance` consumed by existing generate_lut. Metadata records
source basenames (never absolute source paths), SHA256/role/weights/ROI, baseline kind
and values, seed, effective settings, normalization, per-image and pooled statistics,
fitted model, algorithm constants, warnings and approximate/unverified status
(`engine: image-look-v2`). Metadata never embeds image/sample arrays. Each metadata
collection is capped at 128 unique keys and 1 MiB of text; analysis never retains a
decoded image after its statistics.

## Baseline and algorithm
All statistics are measured in Oklab (D65) after converting encoded sRGB. Per image:

- **Tone:** 13 lightness quantiles (L at p = .01 ... .99).
- **Colour cast:** per lightness band (centres .15 .35 .55 .75 .92, Gaussian width .12) the
  mean Oklab (a, b) of near-neutral pixels (weight `exp(-(chroma/.05)^2)`), with the share
  of samples behind it (`cast_mass`).
- **Saturation:** per lightness band the mean chroma with the band's cast removed; per
  hue sector (12 sectors) the mean chroma of colourful pixels (chroma > .03 after cast
  removal), so a global tint does not appear as one strongly saturated hue.

Images of one role are pooled: weights are the image weights (normalized by the largest)
times a confidence `min(1, sample_count/256)`; quantile functions are averaged (the 1-D
Wasserstein barycentre) and band/sector means are weighted by their sample mass.

**Base.** With `base` images the base statistics are the pooled statistics of those
images (`baseline: base_images`). Without them the base is a built-in typical photo
(`baseline: typical_photo`, `BASELINE_VERSION` `cps-distribution-v2`): pooled Oklab
statistics of 45 varied finished photographs (landscape, portrait, night, interiors,
product; sRGB and Adobe RGB sources) sampled at 160 x 160 per image. Its colour cast is
set to neutral grey, not to the average warmth of photographs. The actual numbers are
written to the export metadata. They are an average over unknown subject matter, so
the result for a user photo that differs strongly from that average (for example a
night scene) carries the difference between the photo and the average as well as the
look; base images remove that error.

**Fit** (reference statistics R, base statistics B, each amount 0..1):

- Tone knots: quantile map B -> R at the 13 probabilities, extended to 0 and 1 with end
  slopes limited to .5..3, giving 15 knots; blended toward identity by `tone_amount`;
  monotone with a minimum slope of .15; applied as a PCHIP curve.
- Cast per band: R.cast - B.cast where both `cast_mass` >= .02; other bands take the
  nearest valid band (zero if none); limited to +-.08; times `color_amount`.
- Band gain: R.chroma / B.chroma per band (valid when both masses >= .02 and B.chroma >
  .001), limited to .5..2, blended toward 1 by `saturation_amount`.
- Sector gain: the sector chroma relative to the mean chroma, R over B (valid when both
  sector masses >= .03 and the B sector chroma > .001), limited to .6..1.6, smoothed
  circularly with (.25, .5, .25), blended toward 1 by `saturation_amount`.

**Apply** (per pixel): encoded sRGB -> Oklab -> L' = tone curve(L) -> (a, b) multiplied by
band gain(L') x sector gain(hue), the product limited to .4..2.5 -> add the interpolated
band cast, tapered linearly to zero toward black (L' below .08) and toward white (L' above
.95) -> sRGB. Out-of-gamut colours are brought inside by reducing chroma toward neutral
(x.75, x.5, x.25, 0), then clipped. All-zero component amounts intentionally yield
identity. The cast shifts a and b by lightness band, so greys are not kept neutral: if the
reference has a warm highlight cast relative to the base, neutral highlights become warm.
There is no neutral-protection setting in v2.

These are heuristics with scene/style ambiguity, not recovered editing parameters.
Hue rotation, local edits and subject-dependent grading cannot be recovered from
distributions.

The exported LUT is sampled by the shared `sample_lut` (tetrahedral interpolation);
the preview uses the exported LUT so it shows that approximation, which is largest at
size 17.

## Stable errors and warnings
Errors use existing StudioError subclasses with `code` and `message`:
- `image_look_invalid`: malformed references, ROI, role, settings, records or RGB input;
  no `reference` image; more than 64 images of one role. The CLI also reports unknown
  `settings` keys (including the removed `neutral_protection`) with this code.
- `image_look_baseline`: unsupported `baseline_version`.
- `image_look_unusable`: empty/too-small/constant crop (no inferred look).
- `image_look_conflict`: fit refused until explicit acknowledgement or reselection.
- `image_look_source_changed`: source changed during analysis.
- `image_look_source_io`: cannot hash/read source (I/O exit 3).
- Shared image loader errors and `cancelled` propagate unchanged.

Warning codes:
- `approximate_unverified`: always present.
- `typical_photo_baseline`: no base image was given; the typical-photo baseline was used.
- `limited_luminance_coverage`: lightness range between p = .01 and .99 is below .35.
- `limited_color_coverage`: fewer than 3 hue sectors have enough colourful pixels.
- `insufficient_samples`: fewer than 256 unique sampled pixels.
- `reference_clipping`: more than 5 % of the samples have a channel <= 1/255 or >= 254/255.
- `output_clipping`: more than 2 % of a 17^3 grid needs gamut chroma reduction (judged at
  full strength, before the overall strength).
- `conflicting_tone`: reference median lightness differs by more than .2 or the p = .01..99
  range by more than .3.
- `conflicting_color`: in a lightness band that both references measure, the casts differ
  by more than .03 and point in opposite directions.
- `conflicting_saturation`: mean chroma differs by more than a factor of 1.6.
- `conflicts_acknowledged`: conflicts were acknowledged explicitly.

Loader warnings also propagate as strings. Analysis returns conflicts without throwing;
fit enforces refusal. The per-image warnings are collected from `reference` images only.

## CLI / project JSON
`camera-profile-studio image-look --request project.json --output look.cube
[--size 17|33|65] [--strength 0..1] [--format cube|hald-png|strip-png|strip-png-flipped]
[--overwrite]`

```json
{
  "schema_version": 1,
  "references": [
    {"path": "photos/reference-a.png", "weight": 1.0, "roi": [0, 0, 1, 1]},
    {"path": "photos/reference-b.jpg", "weight": 0.5, "roi": [0.1, 0.1, 0.9, 0.9], "role": "reference"},
    {"path": "mine/street-01.jpg", "role": "base"},
    {"path": "mine/portrait-02.jpg", "role": "base", "weight": 2.0}
  ],
  "settings": {
    "baseline_version": "cps-distribution-v2",
    "seed": 42,
    "analysis_size": 160,
    "max_dimension": 2048,
    "tone_amount": 1.0,
    "color_amount": 1.0,
    "saturation_amount": 1.0,
    "acknowledge_conflicts": false
  }
}
```
`role` is optional per entry: `"reference"` (default) or `"base"`; any other value is
`image_look_invalid`. A request needs at least one `reference` entry (1..64) and may have
up to 64 `base` entries. Relative paths resolve against the project JSON directory.
Optional settings and optional reference fields take dataclass defaults. Unknown
reference fields, invalid JSON/nonfinite numbers and duplicate keys are rejected.
Unknown `settings` keys are rejected with `image_look_invalid`; this includes
`neutral_protection`, which v1 accepted. Output must not overwrite the request or any
image of either role, even with --overwrite. Existing CLI exit conventions apply: 0 success,
2 validation/conflict, 3 dependency/I/O, 130 cancel.

Success JSON contains `schema_version`, `export`, and `image_look_report`; no holdout
report. `image_look_report` fields:

| field | meaning |
|---|---|
| `status` | always `"approximate_unverified"` |
| `warnings` | warning codes of the fitted look (sorted) |
| `conflicts` | conflict codes between reference images (sorted) |
| `baseline` | `"base_images"` or `"typical_photo"` |
| `base_count` | number of images with role `base` |
