# Core Implementation and Verification

Status: COMPLETE for assigned headless core scope; core files frozen after the inverse-TRC audit fix. This is not a claim that the complete desktop distribution or camera JPEG reproduction has been validated.

## Implemented files

- `src/camera_profile_studio/__init__.py`, `__main__.py`: package version and executable module entry.
- `types.py`, `errors.py`: contract records, immutable copied array storage, numerical/settings validation, structured errors, progress and cooperative cancellation. Array backing bytes prevent re-enabling WRITEABLE. No Qt imports.
- `color.py`: float sRGB transfer functions, D65 Lab conversion and CIEDE2000.
- `icc.py`: bounded ICC v2/v4 parser, profile classes/spaces/PCS, descriptions, XYZ/colorant matrices, curves/parametric curves, LUT8/LUT16/mAB/mBA bounds and directional capabilities.
- `lcms.py`: actual LittleCMS 2 C API, float32 RGB buffers, intent/BPC, Unicode-safe memory profile loading, native errors, per-transform contexts and deterministic close/cancellation cleanup.
- `service.py`: lazy ICC/DCP/look routing, domain/settings checks, runtime-aware inspection, source hashes/settings/runtime provenance, guaranteed transform close after LUT generation.
- `cli.py`: inspect/convert/match/gui commands, strict JSON schema fields and duplicate-key rejection, non-pickle NPZ input, structured errors/exit codes, no implicit overwrite or source/settings overwrite.
- `tests/test_types.py`, `test_color.py`, `test_icc.py`, `test_lcms.py`, `test_service.py`, `test_cli.py`: assigned core tests.

LUT/image, DCP/look, matching, GUI, native DLL and packaging files remain their respective workers' property. Integration tests call their real implementations without modifying them.

## Runtime and installation

Installed editable project and declared RAW extra with workspace Python 3.12.15:

```text
.venv\Scripts\python.exe -m pip install -e ".[raw]"
Successfully installed camera-profile-studio-0.1.0 rawpy-0.25.1
```

rawpy 0.25.1 Windows x64 CPython 3.12/3.13 wheels were confirmed before pinning. No additional dependencies were introduced. `pip check` reports `No broken requirements found.`

Native tests use the runtime worker's bundled official LittleCMS 2.17 (`probe_lcms() == '2.17.0'`). Loading order is explicit probe path, `CPS_LCMS2_DLL`, packaged resource, platform loader. Explicit missing/invalid overrides fail rather than silently falling back. The wrapper uses `TYPE_RGB_FLT = 0x0044001C`, disables transform optimization/cache and never inserts an 8-bit image or buffer. Built-in sRGB and linear-sRGB profiles are created through LCMS. External ICC encodings remain profile-specific, never universally labelled linear.

## Independent audit correction

The auditor supplied a validly structured sRGB ICC whose three sampled TRCs were replaced by `[0, 0]`. LCMS's `cmsIsIntentSupported` alone accepted this degenerate inverse and previously produced an all-black LUT.

The parser now independently validates matrix/TRC direction suitability:

- Constant and nonmonotonic sampled TRCs cannot authorize matrix/TRC conversion in either direction.
- Nonconstant sampled curves with plateaus may be used forward, but cannot authorize a unique inverse.
- Parametric curves are checked for positive gamma, branch scale/domain, decreasing slopes, constant segments, finite endpoints and branch continuity. Tiny s15Fixed16 boundary differences are tolerated at two fixed-point units; substantial discontinuities cannot authorize an inverse.
- Singular colorant matrices fail as invalid profiles.
- Capabilities include per-direction/per-intent checks. A CLUT for a different intent cannot legitimize an invalid matrix/TRC fallback. Factories enforce these capabilities before native profile/transform construction, in addition to native intent checks.

Regression coverage includes zero/nonzero constant curves, decreasing sampled curves, sampled plateaus, a positive strictly increasing control, parametric black plateaus, singular matrices and prevention of size-17 LUT generation from a noninvertible profile.

Rechecked the auditor's actual `output/engine-audit/noninvertible-trc.icc` without changing it:

```text
technical.input: unsupported
technical.output: unsupported
PASS: no LUT generated
UnsupportedError(code='unsupported_direction'):
No supported RGB matrix/TRC or directional CLUT path.;
rTRC/gTRC/bTRC: Sampled TRC is constant and has no inverse.
```

This deliberately conservative inverse policy can reject quantized monotonic curves containing plateaus. There is no silent approximate/generalized inverse option.

## Actual verification results

Final full-workspace run at handoff:

```text
.venv\Scripts\python.exe -m pytest -q
238 passed, 1 skipped, 1 warning in 5.75s

SKIPPED: tests/test_images_engine.py real RAW fixture check;
         requires independently supplied CPS_TEST_RAW.
WARNING: colour-science optional Matplotlib plotting API is unavailable.
         Plotting is not used by the color engine.

.venv\Scripts\python.exe -m pip check
No broken requirements found.

git diff --check
No whitespace errors reported.
```

Tests exercise the real native runtime, not only mocks: 4096 fractional RGB identity samples, nonidentity sRGB-to-linear-sRGB against the analytic transfer function, inverse round trip, sub-8-bit distinctions, Unicode paths, invalid/truncated profiles, missing DLL, lifecycle/cancellation and invalid domains. Serialized ICC colorants have s15Fixed16 precision; file-profile versus built-in-profile comparison allows that actual profile quantization, not pixel-buffer quantization. CIEDE2000 is checked against the published Sharma/Wu/Dalal test pair.

Actual subprocess commands through `python -m camera_profile_studio` also passed:

| Operation | Input | Result |
| --- | --- | --- |
| inspect | supplied Nikon D200 ICC | JSON metadata, unverified JPEG accuracy |
| convert technical | Nikon D200 ICC to sRGB, perceptual intent, size 17 | 4,913 validated rows and provenance sidecar |
| convert look | Nikon D200 DCP, Colors Only, strength 0.5, size 17 | 4,913 rows, experimental/unverified metadata, baked strength 0.5 |
| match | 160 synthetic paired patches, grouped holdout, CCM, size 17 | 4,913 rows; 32 holdout samples; mean DeltaE00 3.22954 -> 0.10161 |

Temporary subprocess artifacts were automatically removed after verification; these were acceptance fixtures, not production deliverables or photographic evidence.

## Supported behavior and remaining limits

- Supplied Sony F828/V3, Nikon D70, Kodak DCS760/ProBack ICCs successfully parse and transform. Supplied Nikon D200 CLUT is perceptual-intent-only: callers must select `intent='perceptual'`; relative/saturation/absolute requests fail explicitly.
- RGB device ICC profiles with supported matrix/TRC or directional CLUT paths are supported. Device links, CMYK/Lab device inputs, unsupported active processing structures and missing inverse paths are rejected.
- DCP service dispatch requires explicit camera-linear domain and WB for technical conversion, or encoded sRGB for experimental photo looks; it does not invent sensor decoding or inverse JPEG rendering.
- Matching NPZ is a strict paired-sample interchange format. Chart CSV adaptation belongs to the GUI. Correspondence, held-out metrics and sparse/single-scene limitations belong to the real matching engine and remain visible.
- rawpy is installed, but no independently supplied real RAW fixture was available in this run. RAW processing is the image worker's fixed-recipe development path, not OEM rendering or DCP camera-linear sensor input.
- No camera JPEG accuracy is established by synthetic fixtures, profile parsing, a successful native transform or held-out synthetic calibration. External references and redistribution licensing still require independent acceptance.
- GUI appearance/accessibility, offline portable/installer execution and clean-machine acceptance remain the GUI/packaging workers' gates.
