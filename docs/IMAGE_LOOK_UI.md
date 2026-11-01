# Image Look Creator — GUI design and validation evidence

Scope: `src/camera_profile_studio/gui/**` (source-tree GUI only; packaged acceptance is
validated separately). Engine contract: `docs/IMAGE_LOOK_API.md` v2 (engine `image-look-v2`,
0.2.5), consumed only through `gui/backend.py`. The layout and state rules below describe
0.2.5. The audit and validation sections at the end were produced for 0.2.0 and are marked
as such.

## Placement

* A seventh peer workspace is **appended** after Settings: `nav.imagelook`.
  * Native accessible navigation label: EN "Image Look Creator", JA "画像ルック作成". The same text is the page title.
  * Shortcut **Ctrl+7**. Ctrl+1…6 and list adjacency are unchanged, since `MainWindow.WORKSPACES` drives the menu and the navigation list.
* Icon: the vendored Fluent `image` (Match keeps `image_multiple`), so the navigation icons stay distinct.
* This pathway is separate from the Camera Look Extractor (DCP tables) and Camera Match Studio (paired correspondences). There is no before image, no `SamplePairs`, and no ΔE or accuracy figure anywhere in the UI.

## Image roles (0.2.5)

Every listed image has a role, shown in the "Role" column and stored as the engine's `role`:

| Role (EN / JA) | Engine `role` | Meaning |
|---|---|---|
| Reference / 参考 | `reference` | A finished photo whose look is wanted. At least one included reference is required. |
| Base / 適用先 | `base` | One of the user's own photos that the look will be applied to. Optional, but recommended. |

* The look is the reference statistics minus the base statistics. Without any included base image the engine uses a built-in typical-photo baseline (warning `typical_photo_baseline`); that is much less accurate.
* Recommendation: while no base image is listed, a hint line above the table says that a typical photo is used as the baseline and asks the user to add base images (in a narrow centre the hint is not shown inline). After analysis, the diagnostics state the baseline in use ("statistics of m base image(s)" or "statistics of a typical photo"), and the LUT domain line repeats it.
* Images dropped on the list, and images added with "Add references…", are references. "Add base images…" adds base images. "Use preview image as base", next to "Open preview image…" in the preview pane, adds the opened preview image as a base image (it is disabled until a preview image is open). The role of an existing row is changed in the Role column (combo editor).
* Reference and base images are pooled separately, so the Share column is normalised within the role. Conflicts are judged between reference images only.
* The preview image is never analysed unless it is explicitly added as a base image.

## Layout (`workspaces/image_look.py`, subclass of `LutWorkspace`)

**Centre column**

* An approximate/unverified banner at the top.
* A vertical splitter with two panes:
  1. **Reference and base images (analysed)**
     * Toolbar: Add references…, Add base images…, Remove, and an "n reference(s) (k used) · m base image(s)" count. The base-image recommendation sits above the table.
     * A Model/View `QTableView` over `ReferencesModel`. Columns: include checkbox + thumbnail + name, Role, Weight (delegate 0.01–100, equal by default), Share (normalised per image within the role), Crop, Status. It accepts dropped files; Delete removes the selected rows.
     * Beside the table, the per-image **crop editor** (`widgets/roi_editor.py`):
       * Mouse: draw, move, or resize by the corners.
       * Keyboard: arrows move (Ctrl ×10), Shift+arrows resize, Home/R resets.
       * Zero-area or near-zero crops are impossible (minimum 2 % per axis).
       * Collapsible "Exact values" percentage fields; Reset crop.
       * The ROI is normalised to the orientation-corrected image that the engine loader decodes on a worker (display decode ≤ 640 px; normalised coordinates are resolution independent).
  2. **Preview image (not analysed)**: the existing `PreviewPanel`, loading a separate application target image, with "Open preview image…" and "Use preview image as base". It shows the exact baked `Lut3D` that export writes.

**Side column** (scrolls; numbered by task)

1. **Look amounts:** tone, colour cast, saturation. These are engine *analysis* settings, so changing them invalidates the analysis. The v1 control "neutral protection" no longer exists.
2. **Analyse images (F5):** the button with its progress bar and disabled reason. Conflict text and the explicit acknowledgement checkbox sit right here.
3. **Diagnostics**, built from engine data only:
   * the baseline in use (base images or typical photo);
   * per-set: count, samples, tone range (lightness 1–99 %), clipping;
   * per-image: role, lightness 1/50/99 %, mean chroma, hue coverage (labelled "not accuracy"), clipped fraction, colour cast per lightness band (a, b), SHA-256 prefix;
   * translated warning codes; loader warnings shown verbatim;
   * the statement that no ΔE or accuracy score exists.
4. **LUT output** (existing): size 17/33/65 and overall strength. Strength is passed only to `generate_lut`, so it is baked once.
5. **Advanced:** seed, sample grid size (default 160).
6. **Status and export** (existing pipeline: same LUT, `overwrite=False` unless confirmed, metadata companion).

## State rules

* Every image / role / inclusion / weight / crop / amount / seed / grid change:
  * invalidates the `imagelook:analyze` channel, the analysis, the LUT channel and the preview channel;
  * resets any acknowledgement.
  * Export is disabled with a reason ("Analyse again"). Queued late completions are dropped by generation, and analysis results are also signature-checked.
  * A LUT fitted from a superseded analysis is ignored.
* Size and strength changes rebuild only the LUT from the current analysis.
* Conflicts are never averaged silently.
  * The LUT is not generated until the user ticks the acknowledgement (`fit_image_look(..., acknowledge_conflicts=True)` is passed only then) or changes the selection.
  * The effective acknowledgement is recorded in the LUT provenance.
* Analysis needs at least one included reference image; base images alone cannot define a look, and the disabled reason says so.
* Workers:
  * Image display decodes run **one at a time** (the shared loader may decode a full-resolution file before resizing). Analysis and LUT builds run on the pool with progress and cooperative cancel.
  * Cancel/Esc stops analysis, queued decodes, LUT build and preview. Export keeps the existing commit semantics.
  * Window close waits for workers (existing `MainWindow` logic).
* Inputs:
  * Only JPEG/PNG/TIFF images are offered: the contract defines no RAW recipe for analysis. At most 64 images per role (reference / base); duplicate paths are skipped.
  * Unsupported, unreadable or unusable files are reported with reasons; there are no phantom-disabled buttons.
  * If the engine module is unavailable, a translated reason is shown.
* All labels are in EN and JA (`gui/strings_imagelook.py`, merged into `strings.py`). Only native QStyle and palette are used: no QSS, no new dependencies. Light and Dark follow the existing theme switch.
* Narrow centre (< 560 logical px):
  * hint lines move into heading tooltips;
  * the Share and Crop columns hide (the crop stays visible in the editor);
  * the target path and domain line hide, so the viewer keeps its height.

> **Historical (0.2.0).** The audit fixes and the validation evidence below were produced with the 0.2.0 GUI and the v1 engine (a single reference list, a "neutral protection" control, `cps-designed-prior-v1`). They were not regenerated for 0.2.5, and the screenshots and exports under `output/` do not show roles, base images or the v2 engine. Quantities such as "reference" counts below refer to that version.

## Independent UI audit fixes (`output/image-look-ui-audit/REPORT.md`)

All fixes are in Image-Look-owned code only. The shared `lut_base.py` and `preview.py` are byte-identical to before.

* **F1 (stale After on cancel):**
  * `ImageLookWorkspace.cancel()` re-renders from the current LUT when it is `None`; if a render of the current LUT was cancelled, it shows Before only with a note.
  * `_build_cancelled()` (Jobs-panel cancel) does the same.
  * Proof: `audit-fixes/*_f1_cancel_shows_before_only.png` and `f1_manifest.json`.
* **F2:** the ROI editor ignores pointer motion below `QApplication.startDragDistance()`. A click or jitter neither emits a change nor invalidates the analysis.
* **F3:** model notes are cleared on every LUT-invalidating change, including un-acknowledging.
* **F4:** starting an analysis clears every reference's previous statistics.
* **F5:** the exact-crop spin limits are rounded inward to the 0.1 % step, so every offered value is accepted.
* **F6:** a removed or cancelled in-flight reference decode is drained, meaning its worker must actually return, before the next decode starts. Display decodes never overlap. A running analysis can still overlap a display decode; that is accepted and documented.
* Regression tests `test_f1`…`test_f6` and the updated editor test fail against the pre-fix source (`logs/audit_regressions_against_frozen_source.txt`) and pass after the fix.
* The pink thumbnail is Qt's selected-icon tint and was confirmed not to be a bug.
* The warm demonstration cube has near-zero colour bias because the engine reports colour coverage 0 % for this one harbor scene. That is conservative engine behaviour, so it is **not** proof of warm-style transfer accuracy.

## Validation evidence (`output/image-look-gui/`)

* `scale100|150|200/manifest.json` (native `windows11` style, effective DPR 1.0 / 1.5 / 2.0): 24 screens each, under `screens/`. The scenario set is:
  * EN/JA × Light/Dark:
    * empty;
    * single warm reference with the baseline photo as preview target;
    * multiple variants with weight, crop and exclusion;
    * conflict awaiting acknowledgement;
    * narrow 760×700.
  * EN-light and JA-dark only:
    * conflict acknowledged;
    * unusable-reference error.
  * Each run also writes exports in `exports/`: `.cube` + `.json` with engine provenance (reference sha256 / weight / ROI, settings, status `approximate_unverified`).
* Capture fixtures are the read-only `output/image-look-fixtures`: one CC0 harbor RAW scene with synthetic pixel-math variants, plus procedural charts. They are plumbing evidence only, with no cross-scene, OEM or accuracy claim.
* Automated tests (`tests/test_gui_image_look.py`) generate their own deterministic procedural PNGs at run time. No checked-in or downloaded images are used, and a missing engine module is a failure, not a skip.
* Logs are in `logs/`:
  * pytest runs;
  * screenshot run logs;
  * the source-collector inclusion check;
  * SHA-256 before/after lists proving pre-existing outputs and fixtures are unchanged;
  * the GUI source hash at freeze.
