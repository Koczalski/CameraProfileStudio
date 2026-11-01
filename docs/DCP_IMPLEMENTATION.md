# DCP implementation notes

## Source-grounded decisions

Independent inspection on 2026-10-20 of:

- Adobe DNG SDK `source/dng_color_spec.cpp`, `SetWhiteXY`, `NeutralToXY`: https://github.com/aizvorski/dng_sdk/blob/master/source/dng_color_spec.cpp
- Adobe DNG SDK `source/dng_reference.cpp`, `RefBaselineRGBTone`: https://github.com/yanburman/dng_sdk/blob/master/source/dng_reference.cpp
- Adobe DNG SDK `source/dng_render.cpp`, `dng_render_task::ProcessArea`: https://github.com/yanburman/dng_sdk/blob/master/source/dng_render.cpp
- RawTherapee `rtengine/dcp.cc`, `hsdApply`, matrix assembly: https://github.com/RawTherapee/RawTherapee/blob/dev/rtengine/dcp.cc

These source links are references, not a claim of bitwise SDK equivalence or photographic validation. No third-party implementation source is copied into the application.

1. ColorMatrix maps scene XYZ to camera coordinates. ForwardMatrix maps white-balanced reference-camera RGB to D50 XYZ; it is not inverted in the forward render. With nontrivial individual calibration, the SDK uses `FM * inverse(diag(referenceNeutral)) * inverse(AnalogBalance * CameraCalibration)`. Standalone profile rendering must state its identity-calibration assumption or reject meaningful calibration tags that cannot be honored.
2. Dual-illuminant interpolation is linear in reciprocal correlated color temperature, with endpoint clamping. Neutral-to-white requires iterating with the interpolated ColorMatrix, not multiplying an already white-balanced triplet by an arbitrary matrix.
3. SDK normalizes camera neutral by its largest channel. The ColorMatrix-only inverse path includes white adaptation and the corresponding exposure normalization. Tests must distinguish this from merely inverting ColorMatrix.
4. DCP HueSatMap and LookTable triples are saturation-fastest, then hue, then value: NumPy shape `(V, H, S, 3)`. Hue wraps periodically. The reference renderer uses bilinear/trilinear interpolation, not tetrahedral interpolation.
5. Encoding 1 applies sRGB encoding to V for table addressing, scales V in the encoded domain, then decodes it. It does not simply use encoded V to address a linear-V multiplier. Hue and saturation are computed from linear ProPhoto RGB.
6. Reference order: camera to linear ProPhoto RGB; HueSatMap; exposure/shadow stage; LookTable; RGB tone; final color-space conversion and output transfer function. No default Adobe tone/shadow aesthetic should be invented when absent from the supplied profile.
7. Reference RGB tone maps minimum and maximum channels through the curve and linearly positions the middle channel between those mapped extremes. Applying the curve independently to all channels changes hue and is not the reference behavior. Choice of monotone interpolator must be documented if it differs from SDK spline solving.
8. sRGB look extraction is an explicitly experimental operation on already-rendered pixels, not inversion of JPEG rendering, recovery of sensor data, or reproduction of camera JPEGs. Matrix-only profiles contain no camera JPEG-look design evidence.

## Supplied profile structural inspection

A bounded read-only header/IFD inspection found:

| File | Bytes | Notable tags |
| --- | ---: | --- |
| Nikon_D200.dcp | 1502 | ColorMatrix1, Illuminant1, HueSatMapDims/Data1 |
| Olympus_E1.dcp | 1502 | ColorMatrix1, Illuminant1, HueSatMapDims/Data1 |
| Nikon_D70s.dcp | 1102942 | ColorMatrix1/2, ForwardMatrix1/2, Illuminant1/2, HueSatMap1/2, LookTable, ToneCurve, LookTableEncoding, DefaultBlackRender |

All three start with little-endian `II` and magic `0x4352`, not TIFF magic 42. The structural inspection above is distinct from the subsequently completed renderer smoke tests below. No JPEG-reference accuracy is established.

## Implemented API and rendering contract

`dcp.py` exports `read_dcp`, `render_dcp`, `create_dcp_transform`; `look.py`
exports `look_capabilities`, `apply_look`, `create_look_transform`, with exactly
`docs/API_CONTRACT.md` signatures. No additional dependency or shared API change.

- Technical input is already black-subtracted, demosaiced, normalized,
  **not yet white-balanced**, camera-linear RGB. Output is encoded sRGB.
  Positive camera neutral is mandatory and is normalized by its largest channel.
  Values at or below 1e-12 after normalization are numerically degenerate and refused.
- For ColorMatrix-only conversion, invert the selected/interpolated XYZ-to-camera
  matrix, infer XYZ white from the supplied neutral, normalize white Y to one,
  Bradford-adapt that white to D50, then convert to linear ProPhoto RGB.
- ForwardMatrix takes precedence. With standalone identity calibration/balance,
  the mapping is `ForwardMatrix @ diag(1 / normalized_camera_neutral)` to D50 XYZ.
  Each ForwardMatrix must map unit neutral to within 0.005 of D50 per component;
  accepted coefficient-rounding discrepancies are normalized to exact D50 before
  endpoint interpolation. Singular/ill-conditioned endpoint or interpolated
  matrices are refused (condition number >1e10 or smallest singular value <=1e-12).
- Dual rendering requires explicit positive Kelvin; no scene CCT or camera WB is
  guessed. Kelvin selects the reciprocal-temperature matrix/map interpolation;
  camera neutral determines the actual WB. These are caller-supplied independent
  conditions, not an iterative neutral-to-CCT solver. Their photographic
  consistency must be established by the caller. Fixed calibration temperatures
  supported: illuminant codes 17/18/19/20/21/22/23/24 =
  2856/4874/6774/5503/6504/7504/5003/3200 K. Endpoints clamp in either tag order.
- Stored ColorMatrix precision and relative endpoint scale are retained. The
  renderer does not duplicate SDK profile-authoring normalization/rounding to
  four decimals. Consequently it does not claim bitwise SDK equivalence.
- Photo looks decode sRGB and Bradford-adapt D65 to D50 linear ProPhoto directly;
  **no camera inverse mapping** is used. HSM on already-rendered photos is an
  explicitly experimental reuse of calibration, not JPEG-look design evidence.
- `colors_only` selects HueSatMap plus LookTable; `tone_only` selects ToneCurve;
  `look_only` selects LookTable; `combined` selects all present elements. Explicit
  settings switches can disable these components. An empty selection fails.
  Missing components never acquire substitute tables or curves.
- HSM, LookTable, then hue-preserving tone are applied in that order. No external
  exposure, host default tone, or black/shadow processing is inserted. Matrices
  use D50 xy=(0.3457,0.3585), D65 xy=(0.3127,0.3290), standard ProPhoto/sRGB
  primaries and Bradford adaptation. Output uses the sRGB transfer, not video
  Rec.709 transfer. Tone uses monotone PCHIP rather than SDK spline solving.
- HSV interpolation is saturation-fastest `(V,H,S,3)`, periodic hue, trilinear
  interpolation, hue shifts in degrees, and saturation/value multipliers. In
  encoding 1, V is sRGB encoded for addressing AND multiplication, then decoded.
  Internal HSV saturation/value are bounded to [0,1] as in reference processing.
  `gamut='clip'` clamps input and working/output RGB; `gamut='error'` rejects
  input/working/output excursions beyond a 2e-7 roundoff tolerance. These are not
  perceptual gamut-mapping algorithms.
- Legacy `skipSat0` payloads are supported for HSM1/HSM2/LookTable. The SDK
  `ReadHueSatMap` accepts `H*(S-1)*V*3` floats. `SetDelta` reconstructs missing
  zero-saturation entries with the saturation-one hue shift and saturation scale,
  and value scale 1. This behavior was independently checked in
  `dng_camera_profile.cpp` and `dng_hue_sat_map.cpp`; neutral entries are not
  blindly filled with identity hue/saturation.
- Work is chunked at 32768 pixels with cancellation/progress checkpoints. Public
  results preserve input shape, are float32, and never modify caller input.
  Closed transforms reject further use. No Qt, network, or native runtime needed.

## Parser bounds and unsupported processing

Parser supports classic TIFF byte orders, magic 42 or 0x4352, and TIFF scalar
field types 1-13. Limits: 32 MiB file and cumulative declared tag payload, 1024
IFD entries, 4 million values per tag/table. It validates complete header/IFD/
payload bounds, field types/counts for processing tags, duplicate tags, finite
numbers, rational denominators, matrix shape/invertibility, table dimensions,
nonnegative saturation/value scales, unit zero-saturation value scale, and
monotone tone knots with endpoints (0,0)/(1,1). Malformed data is refused even
when a malformed component would not be selected. Unknown representable tags
remain in `DcpProfile.tags`, but are not assumed harmless.

Known unsupported cases, explicitly refused rather than ignored:

| Feature | Affected operation |
| --- | --- |
| BigTIFF, linked/multiple IFDs, embedded DNG-image containers | File parsing; standalone DCP only |
| Missing ColorMatrix1, non-three-channel matrices | Technical conversion |
| Nonidentity CameraCalibration1/2, AnalogBalance, ReductionMatrix1/2 | Technical; unrelated photo components can still run |
| AsShotNeutral/AsShotWhiteXY embedded in standalone profile; nonzero BaselineExposure | Technical; an individual RAW pipeline is required |
| Nonzero BaselineExposureOffset | Technical, or active LookTable/ToneCurve; isolated HSM can run |
| Third-illuminant ColorMatrix/ForwardMatrix/ReductionMatrix or custom illuminant data | Technical; third/custom HSM-related tags also block active HSM, not isolated tone/look tables |
| Unknown fixed calibration CCT, or equal dual endpoint CCTs | Affected dual matrix/HSM selection |
| Table encoding other than 0/1 | Only the active affected table |
| Encoding 1 with one value slice | Only the active affected table; inspected SDK/RawTherapee 2.5D branches omit encoding yet apply decoding, so compatibility is not guessed |
| DefaultBlackRender other than 0/1 | Active ToneCurve |
| ProfileGainTableMap / ProfileGainTableMap2, RGBTables, ProfileDynamicRange | Rendering; unsupported scene/HDR/spatial stages cannot be flattened silently |
| Other unrecognized potentially processing-affecting tags | Rendering, conservatively; tag is retained for diagnosis |

`DefaultBlackRender=0` (Auto, also the absent-tag default) is a host-dependent
black/shadow instruction, not a defined camera-specific curve. The application
explicitly omits this host behavior, reports a warning, and exposes photo look
capabilities as **partial**. Value 1 requests no default black rendering and is
compatible with that omission. No claim of complete Adobe/OEM rendering is made.

The three supplied profiles report technical `supported` under the explicit
camera-linear/WB recipe, look `partial`, match_reference `missing`, jpeg_accuracy
`unverified`. Nikon D200/Olympus E-1 support only colors/combined photo modes
because their only nonmatrix component is HSM. Nikon D70s supports all four
photo modes. Its recommended task01 experimental photo recipe is
`DcpSettings(include_hue_sat_map=False)` with `mode='combined'`, selecting only
LookTable and ToneCurve. This needs no invented neutral/CCT because neither
selected component is dual-illuminant. Matrix-only profiles cannot yield a look.

## Verification, 2026-10-20

Executed offline after implementation:

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m pytest tests/test_dcp.py tests/test_look.py -q
```

Result: **83 passed, 1 warning in 1.73s**. The warning is Colour's optional missing
Matplotlib notice; plotting is not needed. Tests use independently authored
little/big-endian binary fixtures, hand-computed HSV/tone expectations, and the
installed Colour library's independent XYZ/Bradford/sRGB reference functions.
They cover nonidentity ColorMatrix/ForwardMatrix direction, neutral scaling,
unequal dual matrices/maps, reciprocal CCT/clamping/reversed endpoints,
interpolated singularity, encoding 0/1, hue wrap, trilinear ordering, skipSat0,
mode combinations, malformed/truncated/oversized data, unsupported active versus
disabled tags, zero neutral, cancellation, ownership, and all real local DCPs.
No camera-JPEG accuracy metric is inferred from algorithm tests.

Additional direct transform smoke used a full 65^3 float32 grid (274625 samples)
per profile and operation. Each result retained shape/dtype, was finite and within
[0,1]. Technical settings: camera_neutral=(0.5,1,0.7), illuminant_kelvin=5000 K
(**test conditions only, not measured camera WB**). Photo settings: D70s HSM
disabled; D200/E1 defaults.

| Profile | Technical | Photo |
| --- | --- | --- |
| Nikon_D70s.dcp | pass, [0,1], 0.288s | pass, [0,1], 0.208s |
| Nikon_D200.dcp | pass, [0,1], 0.133s | pass, [0,1], 0.139s |
| Olympus_E1.dcp | pass, [0,1], 0.145s | pass, [0,1], 0.139s |

These are direct-transform smoke checks, not cube interpolation-error studies,
OEM comparison, installer checks, or photographic validation. No RAW/JPEG pairs
or measured corresponding patches were supplied; JPEG reproduction remains
unverified. Profile redistribution/licensing and task01 artifact generation are
outside this module's ownership.
