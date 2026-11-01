# Camera Profile Studio — UI design and rationale

Scope: `src/camera_profile_studio/gui/**` (PySide6 6.9.1 / Qt Widgets). The GUI calls the
engine only through `gui/backend.py`, which follows `docs/API_CONTRACT.md` v1.

## 1. Primary sources consulted (spec 04 §3)

| # | Source | What it decided here |
|---|---|---|
| 1 | Microsoft, *Design Windows apps* — https://learn.microsoft.com/en-us/windows/apps/design/ | Use standard platform controls, theme, typography, and support accessibility from the start. |
| 2 | Qt for Python, *QStyle* — https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QStyle.html | Windows ships the `windows11` (Fluent) style plugin as well as `windowsvista`. `windows` and `fusion` are built in. `QStyleFactory.keys()` lists the styles available at runtime. Change the platform style with a QProxyStyle, not with style sheets. |
| 3 | Microsoft, *Navigation basics* — https://learn.microsoft.com/en-us/windows/apps/design/basics/navigation-basics | Six peer pages with no parent/child relation (fewer than 8) → flat structure. Use left navigation when there are more than 5 top-level items. Label destinations clearly. No hierarchy deeper than 2 levels. |
| 4 | Fluent 2, *Layout* — https://fluent2.microsoft.design/layout | 4 px base spacing ramp (4/8/12/16/24). Proximity groups related items. Responsive behaviour adjusts at breakpoints. Align text left and centre icons. |
| 5 | Microsoft, *fluentui-system-icons* — https://github.com/microsoft/fluentui-system-icons | MIT-licensed Regular 24 px SVGs. Vendored unmodified (commit `83b26348c811f072429427ff48362238656524fe`) with LICENSE/NOTICE in `assets/icons/fluent/` and in a runtime copy at `gui/icons/`. |
| 6 | Microsoft, *Accessibility overview* — https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessibility-overview | Every element needs an accessible name. Support keyboard/tab navigation, contrast, system text scaling and high contrast. |

## 2. Information architecture and layout

* **Main window:** a left `QListView` backed by a `QStandardItemModel` (Model/View) lists the six workspaces. A `QStackedWidget` holds the pages. A bottom `QTabWidget` shows **Jobs** (a `QTableView` over `JobsModel`) and **Log** (a `QListView` over `LogModel`). The two areas are separated by user-resizable `QSplitter`s, and their sizes persist in `QSettings`. Shortcuts: Ctrl+1…6 switch workspaces, Ctrl+J toggles the panel, Ctrl+Shift+L switches the language, and Ctrl+O opens a file.
* **Each workspace** has a top header with the page title, a subtitle and the primary commands. Below it a `QSplitter` holds the centre work area/preview and a right settings column inside a vertical `QScrollArea`. The right column is ordered by task. In the Converter that order is 1 Profile → 2 Conversion/colour spaces → LUT size/strength → Advanced → Status/Export. Advanced settings sit in a disclosure section that is collapsed by default (spec 03 "複雑な設定はAdvanced").
* **Profiles** live in one shared `ProfilesModel` used by the Inspector table and by the Converter and Look combo boxes. The Converter's output ICC picker (ICC→ICC) is a separate combo box. Opening an output profile uses `open_profiles(make_current=False)`, so it never changes the input selection.
* **Responsive behaviour:**
  * Below 1000 logical px wide, the left navigation becomes icon-only (NavigationView "LeftCompact"); names stay available as tooltips and accessible text.
  * When the preview toolbar is narrower than its full width, the image button becomes icon-only.
  * Side panels scroll instead of clipping.
  * Long check/radio labels use `WrapChoice`: a standard button plus a word-wrapped buddy label, so Japanese text never clips.
  * Paths are elided in the middle. The full path is in the tooltip and accessible description, and the text can be selected.
  * Minimum usable window: about 690×680 logical px.

## 3. Native styling decisions

* Style: `windows11`, falling back to `windowsvista` and then `Fusion` (`app.preferred_style`, tested). No application-wide QSS and no custom palette. The only "design system" is the Fluent 2 spacing ramp constants in `widgets/common.py`.
* Theme: System/Light/Dark via `QStyleHints.setColorScheme` (Qt ≥ 6.8). Icons are drawn by a `QIconEngine` at paint time from the active palette's `WindowText` (and the disabled colour), so live theme switches recolour them. Secondary text uses `QPalette.PlaceholderText` as the foreground role.
* Fonts: the system font with a larger point size and DemiBold weight for titles. No font files are bundled. This is the spec exception to generic style guidance.
* DPI: Qt 6 per-monitor DPI awareness with `HighDpiScaleFactorRoundingPolicy.PassThrough`, so 150 % is not rounded to 200 %.
* No Mica, Acrylic or animation. They were not needed (spec 04 §5).

## 4. Behaviour rules that came from the specs and the contract

* **Background work:** all work runs on the `JobManager` thread pool. This covers inspect, image decode/resize (in the engine), LUT build, preview render, export, registration, fitting, batch items and diagnostics.
  * Channels plus *generations* drop stale results; for example, a slow preview cannot overwrite a newer one.
  * `supersede=False` jobs are never filtered.
  * Jobs with `side_effects=True` (export, batch) always report their real outcome. A late cancel after the engine has committed files is reported as success with the paths. A real error such as a partial publication is reported as a failure, never hidden as "cancelled".
  * Closing the window cancels all jobs. If a worker (for example a native call) is still running after the timeout, the window stays open and retries until the pool is idle.
* **Distinct states:** Jobs show Queued/Running/Succeeded/Failed/Cancelled/Replaced. Failures show a translated summary, the raw engine detail and the traceback in "details".
* **Disabled commands always state why.** A `DisabledReason` label sits under the command, and the same text appears in the tooltip. Examples:
  * Strength is fixed at 100 % because the input and output `SpaceSpec`s differ, including the profile path.
  * A camera-linear or ICC-input LUT cannot be previewed on an sRGB photo.
  * A DCP requires a camera neutral, and a dual-illuminant profile requires Kelvin; neither is guessed.
  * An ICC without a B2A/invertible path cannot be the output (the direction item is disabled, with the engine reason).
  * Look modes without the needed elements are unavailable, with the engine reason.
  * An unrelated reference photo is unsupported for Match.
  * Match needs a same-scene confirmation plus either registration followed by a visual check, or declared pixel alignment.
* **Preview equals export:** both use the same engine `Lut3D` object, with strength baked by the engine. Preview uses `images.render_preview` on that LUT. A test compares the exported cube read back against the in-memory LUT and checks that their preview renders match.
* **Overwrites:**
  * Single export: the native save dialog confirms the `.cube`. An existing `.json` companion is confirmed separately. Without confirmation, `overwrite=False` is passed to the engine.
  * Batch: existing outputs are detected first, and the user chooses Replace / Skip existing / Cancel.
* **Language:** JA/EN switch live through key-based bindings. The log, job and status texts are re-rendered from keys. Tests check key parity and placeholders.

## 5. Validation evidence (output/gui-validation)

* `tests/gui_screenshots.py --scale {100,150,200}` runs native Windows (`platform=windows`, `style=windows11`). The host is at 225 %, so `QT_SCALE_FACTOR` is compensated to give an effective DPR of 1.0/1.5/2.0 (recorded in `manifest_scale*.json`). The script drives real actions and saves 24 screenshots per scale (6 workspaces × EN/JA × Light/Dark) plus 5 narrow (760×700) screenshots. The actions are:
  * inspect 6 files (one invalid);
  * ICC→sRGB, ICC→ICC with export, and sRGB→ICC with preview;
  * the D200 inverse refusal;
  * DCP technical, without and then with operator-entered WB;
  * Look look-only at 60 % with export, and combined needing Kelvin;
  * Match CSV fit, then pair confirm/register/fit/export;
  * batch: run, re-run choosing skip (files unchanged), re-run choosing replace;
  * diagnostics.
* `tests/test_gui_core.py` covers i18n, style fallback, job semantics including late-cancel publication, and viewer keyboard handling. `tests/test_gui_workflows.py` covers real-engine end-to-end flows offscreen.

## 6. Known limitations

* Engine reasons, warnings and capability texts are shown verbatim in English, even in the Japanese UI.
* The screenshots emulate 100/150/200 % with `QT_SCALE_FACTOR` on a 225 % host. Windows display scaling itself was not changed (no host changes allowed).
* The screenshots are widget grabs of the client area, not desktop captures including the title bar.
* Drag & drop is exercised by sending `QDropEvent`s in tests, not by a physical mouse drag.
* Keyboard tests cover navigation, shortcuts, the viewer and the Advanced toggle. A full screen-reader (Narrator) pass was not performed.
* High-contrast themes were not separately captured.
* Match fixtures are synthetic: an engine-generated D70s LookTable pair. They demonstrate the workflow, not camera accuracy.
* RAW (rawpy 0.25.1 is installed) was not exercised through the GUI with a real RAW file, because no RAW sample was supplied.
* Packaging needs `camera_profile_studio.gui` package data for `icons/*.svg` + `LICENSE` + `NOTICE.txt` (owned by packaging/pyproject).
