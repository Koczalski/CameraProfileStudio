# Camera Matching Implementation

## Status and boundary

The five frozen functions in `camera_profile_studio.matching` are implemented:
`align_pair`, `sample_image_pair`, `fit_match`, `apply_match`, and
`create_match_transform`. No Qt, file exporter, image decoder, RAW developer, or
parallel public type hierarchy is introduced. Shared types are immutable;
returned arrays are owned, C-contiguous, correctly typed, and read-only.

All matching data is normalized **encoded sRGB** (Rec.709 primaries, D65, sRGB
transfer). This is not raw sensor RGB or camera-linear DCP profile space. The
core `images.load_image` owns JPEG/ICC normalization and optional fixed rawpy
RAW development. Matching preserves supplied source/target metadata and
warnings in its evidence JSON; it does not invent a RAW recipe. In particular,
RAW-to-sRGB development is not OEM camera JPEG reconstruction.

## Correspondence and registration

- Both image functions refuse the default `same_scene_confirmed=False`.
- `sample_image_pair` requires equal geometry and an explicit user assertion
  that pixels correspond. It does not claim to detect unrelated scenes from
  equal image dimensions. Supply aligned images and the registration validity
  mask when necessary. A user assertion is not automatically verified evidence.
- `align_pair` actually estimates source-to-target homography: ORB on images
  reduced to at most 1500 pixels on the long edge, at most 4000 features,
  mutual 0.75-ratio descriptor matches, and RANSAC (3000 iterations maximum).
  A fixed RNG seed and a registration lock make repeated calls reproducible.
- Acceptance requires at least 16 tentative matches, 12 inliers, 50% inliers,
  8% convex-hull coverage in each image, median reprojection error <=2.5 reduced
  pixels, nondegenerate/nonfolding geometry, and at least 20% overlap/64 pixels.
  Failure raises `ValidationError(code='alignment_failed')`; it never silently
  falls back to unregistered pixel pairing. OpenCV failures become `ImageError`.
- Source RGB is bilinearly warped into target coordinates. The boolean overlap
  mask is eroded by two pixels to exclude interpolation/border contamination.
- A homography cannot guarantee correct nonplanar parallax, occlusion, local
  subject movement, or identical illumination. Users must inspect alignment
  and intersect the validity mask with their own exclusion mask. No dense flow
  or automatic occlusion detector is claimed.

When constructing a new `ImageData` from `AlignmentResult.source_aligned`, keep
its original source path, metadata and warnings. The frozen `AlignmentResult`
has no metadata fields, so the caller retains this information explicitly.

Sampling uses bounded chunked random priorities, not a full image-sized random
index list. It returns at most 20000 samples. Pixels receive one of 8x8 spatial
block group identifiers, so adjacent pixels within a block cannot leak across
the training/holdout boundary. Different scenes/captures need distinct group
prefixes when combining sample sets.

## Charts and user-selected patches

Construct `SamplePairs(source_rgb, target_rgb, groups, evidence)` from measured
corresponding RGB patches. For a ColorChecker, use actual paired patch means,
not guessed nominal camera values. Convert documented reference Lab/XYZ to the
specified D65 sRGB space before submission; values must be finite and [0,1].
Clipped/out-of-gamut references need an explicit documented policy upstream.
Repeated observations of the same patch/capture should share a group.

The frozen API has **no CSV file-ingestion function**. CSV reading remains an
adapter/integration requirement for the parent CLI/GUI owner, not an invented
matching API. A suitable adapter schema is:

```csv
source_r,source_g,source_b,target_r,target_g,target_b,group
0.21,0.32,0.19,0.23,0.30,0.18,chart_capture_01_patch_01
```

RGB columns are normalized encoded sRGB, not 0..255 integers or Lab. Evidence
must separately record the chart/reference origin, normalization and grouping.
The same `SamplePairs` seam supports user-selected corresponding patches in
otherwise unrelated photographs; arbitrary unpaired references are not a
supervised fitting input. A standard 24-patch chart supports 18 training and
six held-out patches at default settings; this is extremely sparse gamut
coverage, and the model emits a corresponding warning.

## Split, model and bounded computation

1. Validate shapes, range, evidence, settings, group identifiers and counts.
   Require at least two groups and at least six samples in each split.
2. Sort group identifiers and apply a seeded group permutation. Select the
   feasible whole-group prefix whose sample fraction is nearest the requested
   holdout fraction, based only on group cardinalities. No RGB values or errors
   choose the split. If the seeded ordering cannot satisfy the minimum counts,
   fail clearly instead of repeatedly searching for a favorable split.
3. Before any fitting, deterministically subsample within each split if the
   input exceeds 20000 samples. Report indices in the original input array.
   Training and evaluation together remain <=20000 samples. Check six distinct
   colors, rank three and condition <=1e6 on training source and target only.
4. Fit a true 3x3 CCM using linear-light samples and weak identity ridge. Matrix
   direction is exactly `linear @ ccm.T`; matrices use float64. A singular or
   unstable fitted matrix is rejected.
5. For `ccm_tone`/`ccm_tone_lut`, jointly refine CCM and channel power curves on
   at most 4096 **training-only** samples (bounded least-squares, <=80 function
   evaluations, CCM entries [-4,4], powers [0.35,3]). Floating-point encoding
   inside optimization remains float64 to keep numerical derivatives useful.
   Refine each channel to 65 piecewise-linear knots with ridge-to-power and
   second-difference smoothing, then isotonic projection into [0,1]. Tone
   curves are monotone nondecreasing, not necessarily strictly increasing.
6. For `ccm_tone_lut`, fit a true additive residual on the **tone-output** cube,
   using eight-corner trilinear weights. Grid settings 5, 9 and 17 all execute.
   With W the interpolation design and D the grid-neighbor difference matrix,
   solve `min ||W r - (target-base)||^2 + lambda*(||r||^2 + ||D r||^2)` for each
   channel, where `lambda = regularization * train_count / grid_size^3`.
   Matrices are sparse, never dense sample-by-sample kernels. Diagonally
   preconditioned conjugate gradient is capped at 400 iterations per channel;
   nonconvergence fails rather than publishing a partial model.
7. Holdout is used only for reporting, never for model selection, hyperparameter
   tuning, tone knots, residual fitting or choosing whether to retain a model.
   A model that does not improve holdout mean emits a warning; it is not silently
   replaced by identity or a different fitted model.

`MatchSettings` exposes all supported fitting modes, positive regularization,
coarse grid, holdout fraction and seed. No hidden inaccessible tuning modes are
claimed. `ccm` intentionally has identity tone/zero residual; `ccm_tone` has a
zero residual. These are explicit model components, not dummy training results.
Defaults regularize a small dataset but cannot infer unsampled camera colors.

Application order is frozen and shared by direct evaluation and exported LUT
sampling:

```text
sRGB decode -> linear @ CCM.T -> sRGB encode/clip
            -> per-channel monotone piecewise-linear tone
            -> trilinear additive residual -> clip [0,1]
```

Application is chunked at 8192 RGB samples. Output has the same `(...,3)` shape,
float32 dtype and read-only owned storage. Model fields have contract types:
CCM/tone/homography float64, residual/RGB float32, indices int64, mask bool.

## Diagnostics, cancellation and export

`MatchReport` exposes exact train/holdout indices; `before` is identity on the
holdout, and `after` is the full fitted prediction on that same holdout. Each
has count, mean, median, p95 and maximum CIEDE2000, evaluated consistently in
D65 Lab from encoded sRGB. Warnings include sample counts, requested settings,
sparse coverage, disabled components, clipping and accuracy limitations.

Callbacks receive shared `Progress` records. Checks occur at entry, around
native calls, in bounded sampling chunks, optimizer evaluations, CG iterations,
application chunks and immediately before results. A callback that cancels at
the final progress event still raises `CancelledError`; no model/result is
published. Individual OpenCV/BLAS/SciPy native calls cannot be interrupted
immediately. No output artifact is written by matching itself.

`create_match_transform` exposes sRGB->sRGB. Core `generate_lut` samples it at
17/33/65; core `sample_lut` is the preview path, and core `export_cube` writes
cube/metadata. The matching transform supplies provenance containing evidence,
model SHA256, processing order, experimental/unverified classification,
settings/warnings, train/holdout counts and baseline/matched metrics. Strength
is baked by the shared LUT generator, never applied a second time by matching.

## Verification evidence

Executed with `.venv/Scripts/python.exe`, `PYTHONPATH=src`:

```text
python -m pytest tests/test_matching.py tests/test_matching_diagnostics.py -q
38 passed, 1 warning in 2.60s
```

Coverage includes independent known-CCM direction and recovery, actual nonlinear
and tone fitting, all 5/9/17 residual settings, 24-patch charts, immutable arrays,
seed reproducibility, group isolation, changed held-out targets having no effect
on any fitted parameter, rank/cardinality/range/nonfinite rejection, failed and
successful ORB/RANSAC registration, overlap masks, unrelated-pair rejection,
metadata evidence, <=20000 fitting/evaluation samples, cancellation including
final publication, monotonic per-stage progress, independent Colour DeltaE00
reference, and finite expected-size 17/33/65 shared export/readback equivalence.

Measured on the independent synthetic CCM+channel-tone+cross-channel target in
`tests/test_matching.py::references(1800, nonlinear=True)`, seed 42, 1440 train
samples and 360 holdout samples:

| Transform | Held-out mean DE00 | p95 | maximum |
| --- | ---: | ---: | ---: |
| Identity baseline | 6.474854 | 12.217309 | 18.652519 |
| Match, residual grid 5 | 0.077650 | 0.198551 | 0.558842 |
| Match, residual grid 9 | 0.060890 | 0.145637 | 0.382885 |
| Match, residual grid 17 | 0.074566 | 0.191519 | 0.527019 |

These grid comparisons are measurements only; the implementation never chooses
a grid using holdout scores. A larger fitted grid is not automatically better.

Shared exported-grid interpolation versus the direct grid-5 match, evaluated
on 5000 independent uniform RGB samples in [0.025,0.96], RNG seed 811:

| Export grid | mean DE00 | p95 | maximum |
| --- | ---: | ---: | ---: |
| 17 | 0.062097 | 0.163143 | 0.522219 |
| 33 | 0.028559 | 0.077131 | 0.320251 |
| 65 | 0.009869 | 0.031036 | 0.172214 |

The latest full-suite integration run reported `196 passed, 1 skipped, 1 failed`
in 4.41s. The unrelated failure was
`test_service.py::test_lazy_missing_module_is_explicit`: its monkeypatch replaces
`importlib.import_module` with a function requiring `package`, but LCMS resource
lookup calls it with one argument. Service/LCMS tests are outside matching
ownership and were not edited. Earlier DCP/LCMS failures were resolved by their
owners between integration runs. The skip requests `CPS_TEST_RAW`, an independently
supplied real RAW fixture. Compilation of matching and its tests succeeded;
`git diff --check` for scoped paths reported no errors. Colour reports a benign
missing-Matplotlib warning; matching does not plot.

## Remaining limitations and integration requirements

- No supplied independently captured RAW/JPEG/chart benchmark: actual camera
  JPEG accuracy remains **unverified**. Synthetic recovery is algorithm evidence
  only. Spatial block/patch holdout is not independent cross-scene validation.
- CSV file parsing/UI patch selection is not a frozen matching API; parent
  adapters must supply validated `SamplePairs`. No CSV file-loader is claimed.
- RAW decoding/recipe enforcement and embedded ICC handling are core-owned and
  not independently reimplemented or certified by these matching tests.
- Registration is global planar homography, not dense nonplanar registration
  or automatic occlusion/motion masking. It can reject difficult valid scenes.
- No local tone mapping, OEM sharpening/noise reduction, metamerism correction,
  illuminant invariance, Log/HDR domain or perfect camera emulation is claimed.
- No matching-worker changes to CLI, GUI, shared types, deployment, dependencies,
  credentials, external publications or system configuration were made.
