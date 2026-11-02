![Windows Build](https://github.com/Koczalski/CameraProfileStudio/actions/workflows/windows-distribution.yml/badge.svg)
# Camera Profile Studio

English | [日本語](README.ja.md)

**[Download (Releases)](../../releases)**

A Windows app that turns camera profiles (ICC / DCP) and photos into 3D LUTs. A personal hobby project.

## Key Features

### 1. Technical Conversion
* **Profile to LUT**: Bakes the colour processing of an ICC or DCP profile into a 3D LUT as is.
* **Input and output**: Camera RGB, sRGB, linear sRGB or any ICC profile, with rendering intent and black point compensation.

### 2. Camera Look Extraction
* **DCP looks**: Turns a camera's HueSatMap, LookTable and tone curve into an approximate look LUT.

### 3. Image Look Creator
* **From reference photos**: Analyses tone, colour cast and saturation of 1 to 64 photos and combines them into one look.
* **Base images**: Add your own photos that the look will be applied to. The look is then estimated as the difference to them, which gets closer to the references.

### 4. Camera Match
* **Match two cameras**: Estimates a LUT that matches the colours of two photos of the same scene.
* **Measured**: Shows the colour difference (ΔE00) before and after.

### 5. Batch
* **Many at once**: Converts many profiles in one run. Items that cannot be processed are listed with the reason before the run starts.

### 6. Three Export Formats
* **.cube / Hald CLUT PNG (16-bit) / strip PNG (8-bit)**: For photo and video software and game engines.
* **Interpolation**: Preview and export use tetrahedral interpolation, so greys stay neutral.

### 7. Preview
* **Before and after**: Loads JPEG, PNG, TIFF and RAW and compares them in a split view. Embedded ICC profiles and EXIF orientation are applied.

### 8. English / Japanese, Light / Dark
* **Display**: Switch between English and Japanese, and between light and dark themes.

### 9. Updates
* **Update check**: Help > Check for updates… (and automatically at most once a day at start-up; can be turned off in Settings) looks at the public GitHub Releases. An installed copy can download, verify (SHA-256) and install the new version; a portable copy opens the download page. Only the public release list is requested, and nothing about you or your settings is sent.

## Technology Stack

* **Language**: Python 3.12
* **GUI**: PySide6 (Qt 6)
* **Colour**: NumPy / SciPy / colour-science / LittleCMS 2 / OpenCV / LibRaw (rawpy)
* **Build**: Nuitka / Inno Setup
* **OS**: Windows 10 / 11 (64-bit)

## Development

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[gui,raw,test]
python -m camera_profile_studio.gui.app
```

* LittleCMS (lcms2.dll) is required. Build it with `scripts\build-lcms.ps1` or point `CPS_LCMS2_DLL` to it.
* The sample camera profiles used by the tests are not included ([input/ccd_profiles/README.md](input/ccd_profiles/README.md), Japanese).
* Building the exe: `packaging\windows-launcher\Build-0.2.6.cmd`
* Detailed documentation: [docs/README.en.md](docs/README.en.md)

## Contribution & Feedback

This is a hobby project, so I may not be able to respond to Issues or PRs. I do read them.

## License

[MIT License](LICENSE). Bundled libraries (Qt / PySide6, LibRaw and others) keep their own licenses ([licenses/](licenses/)).
The software comes with no warranty. Colours are approximations, not exact reproductions of a camera or film.

## Contact

Discord: https://discord.com/users/618663163914878986

<sub>Developed with assistance from Anthropic's Claude.</sub>
