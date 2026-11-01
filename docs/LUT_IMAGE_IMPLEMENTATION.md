# LUT and image implementation

## Frozen interfaces and ownership

Implemented `lut.py` and `images.py` signatures exactly as published in API_CONTRACT.md, using shared types/errors. No independent public records, dependencies, HTTP APIs or GUI imports. `generate_lut` does not close its caller-owned transform; the service remains the owner. `render_preview` owns and closes only its temporary output-to-display transform. Arrays remain float32; 8-bit encoding is used only for report visualization PNGs.

## Behavior

- LUT memory is `[r,g,b,channel]`; cube rows are B outer / G middle / R inner (red fastest). Generation/export supports 17/33/65. Tetrahedral interpolation (trilinear before 0.2.5) is chunked, endpoint-correct, reproduces grid nodes exactly and bounds only arithmetic roundoff after validating LUT data. Channel ordering is independently tested using an asymmetric transform with cross terms; arbitrary cube values cannot reveal the author's intended channel semantics.
- Strength is baked exactly once. Nonunit strength requires equal input/output spaces. Zero strength still evaluates the underlying transform, so it cannot conceal an unsupported conversion. Preview samples the baked LUT, never blends again.
- Cube parsing rejects shapers, nonunit domains, misplaced/duplicate/unknown headers, invalid counts/channels and out-of-range/nonfinite values. Titles use escaped quoted text; comments cannot inject lines. Imported files remain unverified in the absence of accuracy evidence. A present companion JSON must match content hash, spaces, domain, size, title and strength; matching provenance is preserved. Import computes the hash over the exact bytes parsed.
- Export stages and fsyncs cube and JSON beside the destination, under an exclusive per-target lock. Exclusive hard-link creation closes the no-overwrite publication race. Sidecars record schema, cube hash, spaces, domain, strength, preserved provenance, software versions and validation. Profile hashes already captured by a transform are retained rather than replaced by a later filesystem read.
- Publication is the commit boundary: cancellation is checked immediately before publication, including after the last progress callback. Cancellation arriving during a successful commit cannot turn that committed pair into a cancellation error. Partial-publication or cleanup failures explicitly raise ExportError with published paths; they never return success. No rollback can overwrite pre-existing user data.
- Raster loading preserves unsigned 8/16-bit RGB/RGBA, normalizes embedded RGB ICC through LCMS float, applies EXIF orientation, unpremultiplies declared associated TIFF alpha, composites against encoded sRGB white after ICC normalization, then resizes. Metadata records these choices. Untagged images explicitly assume sRGB with a warning. Malformed ICC, including a broken PNG iCCP represented by Pillow as None, is never silently ignored. OpenCV decodes RGB16 PNG and common compressed RGB TIFF without an 8-bit intermediate.
- Image normalization, alpha composition, LCMS conversion, LUT generation and interpolation have bounded chunks and cancellation checks. Nested transforms receive cancellation-only contexts to avoid resetting reported stage progress. Native image decode, native RAW development and native resize are not immediately interruptible; checkpoints bracket those operations.
- AREA resize is mathematically unit-bounded but can overshoot white by a float32 ULP. Normalization/resize reject nonfinite or deviations beyond 1e-6, then bound only that numerical roundoff. This fixes the real Nikon D70s max_dimension=1200 regression without masking large invalid values.
- RAW uses explicit AHD, uint16, sRGB, gamma (2.4,12.92), no auto-bright, no auto-scale override, camera or explicit user WB, exposure shift and fixed supporting options. Effective options, recipe, rawpy/LibRaw versions, available sensor/camera metadata, orientation and actual camera WB are recorded. Camera WB requires finite positive RGB multipliers (a zero fourth coefficient is legitimate for three-color LibRaw cameras). The AHD recipe requires a three-color Bayer CFA; X-Trans, monochrome and stacked sensor data are explicitly rejected rather than silently changing demosaic algorithms.
- Preview accepts only sRGB-input LUTs. ICC/linear-sRGB outputs explicitly convert to display sRGB through LCMS. Camera-linear cubes cannot be applied to ordinary sRGB photos.

## Verification and evidence

Environment: Python 3.12.15, NumPy 2.2.6, real LittleCMS 2.17 runtime, rawpy 0.25.1; dependency versions are pinned by the core/packaging owners.

Commands (repository root):

```powershell
$env:CPS_TEST_RAW=(Resolve-Path input/raw_validation/raw_pixls_us_1117_Nikon_D70s.NEF).Path
.venv/Scripts/python.exe -m pytest tests/test_lut_engine.py tests/test_images_engine.py tests/test_preview_engine.py -q
.venv/Scripts/python.exe scripts/validate_lut_quality.py --output output/lut-quality
.venv/Scripts/python.exe scripts/validate_raw_input.py
```

- Targeted tests: 57 passed with the real RAW fixture enabled; log `output/lut-quality/pytest-targeted.txt`. Without CPS_TEST_RAW, only the optional real-RAW test skips.
- Full-suite integration snapshot: 231 passed, one harmless colour-science warning that optional Matplotlib is absent; log `output/lut-quality/pytest-integration.txt`. Concurrent workers own later integration changes.
- Real RAW acceptance rerun: COMPLETE, including file hash/license evidence, 16-bit recipe, WB/version metadata, normalized immutable RGB, max_dimension=1200 and direct-recipe pixel agreement. The authorized script's evidence is under `output/raw-validation/`; that script and output belong to the batch worker, not this implementation.
- Regression coverage includes asymmetric red-fastest ordering, independent tetrahedral reference values, strength/export/re-read/preview equivalence, malformed cubes and sidecar tampering, competing no-overwrite exports, injected partial publication failure, cancellation before/during commit, interpolation at white, resize roundoff, nontrivial embedded ICC plus alpha, associated alpha, 16-bit PNG/LZW TIFF, orientation, RAW recipe mocks, invalid actual WB, unsupported CFA and chunk cancellation/progress.

### Real LCMS interpolation comparison

The script creates a redistributable analytic D65 / Rec.709-primaries / gamma-1.8 ICC fixture and compares exported/re-read LUTs against direct LCMS float transformation to encoded sRGB. It evaluates 28,228 colors: 20,000 seeded random samples, 24 original synthetic chart patches, 8,196 gradient samples and eight corners. It records per-set and aggregate RGB channel errors, per-color maximum RGB error and DeltaE00 D65 distributions (mean, median, p95, max) in `output/lut-quality/quality.json`.

| Grid | DeltaE00 mean | Median | p95 | Max | RGB channel mean | RGB channel max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 17 | 0.05097598 | 0.01605709 | 0.18709845 | 2.04543115 | 0.000659279 | 0.01398577 |
| 33 | 0.01230520 | 0.00379391 | 0.03806272 | 0.86718033 | 0.000156350 | 0.00544322 |
| 65 | 0.00342995 | 0.00098392 | 0.01160792 | 0.26068291 | 0.000044549 | 0.00163134 |

Actual visual evidence: `output/lut-quality/direct-vs-lut-chart.png`, inspected as a four-panel chart/gradient comparison. The output directory also contains the generated ICC, three real cube/JSON pairs, numerical JSON and execution logs. All numerical measurements are float32, not measured from the 8-bit visualization. The chart is original synthetic data, not a licensed ColorChecker dataset.

## Known limitations

- These measurements establish interpolation fidelity for a known nonidentity transform, NOT camera JPEG reproduction accuracy, OEM processing fidelity or general profile accuracy. The real RAW validates decoding only; it has no paired OEM JPEG/color-chart accuracy claim.
- Two independent filesystem paths cannot be published as a globally atomic pair. Partial failures are explicit and may leave the reported published file. Cooperating exporters are locked; unrelated software can still change files. Filesystems without hard-link support fail rather than silently weakening no-overwrite. A process crash may leave a lock requiring manual recovery after confirming no active writer.
- Only unit-domain 3D cubes and generated/exported sizes 17/33/65 are supported; no 1D shapers, Log/HDR domains or inferred channel semantics.
- Floating/HDR, CMYK/Lab device images, multipage TIFF and ambiguous TIFF extra channels are rejected. Compressed alpha/planar TIFF requiring unavailable tifffile codecs fails explicitly; the OpenCV TIFF fallback is restricted to contiguous RGB and verifies source bit depth.
- Alpha compositing and AREA resize operate in encoded display sRGB as explicitly recorded; they are not a linear-light photographic compositing workflow.
- RAW support is conditional on a valid three-color Bayer sensor, available camera WB or explicit user WB, and LibRaw's format support. It is developed sRGB for preview/matching, not camera-linear DCP input. Unsupported sensor patterns never silently substitute another demosaic algorithm.
- Native calls and large immutable-result copies cannot guarantee immediate cancellation. Cancellation after the export commit boundary is too late for that export.
