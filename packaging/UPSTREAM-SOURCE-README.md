# Corresponding upstream source preparation

This directory ships inside both application distributions. It contains exact
upstream Qt/PySide 6.9.1 source release archives, not just links. manifest.json
records their official URLs, byte lengths and SHA256 values. No binding source
offer or guarantee of legal compliance is made. Application source is in the
separate CameraProfileStudio-Source.zip, including workflows, tasks and notices.

## Binary-to-module coverage

- qtbase: Qt6Core, Gui, Widgets, Network, OpenGL, OpenGLWidgets, PrintSupport,
  Sql, Xml, Concurrent, DBus, Test; Windows/offscreen/minimal/direct2d platform
  plugins, Windows style, GIF/ICO/JPEG plugins, printing and TLS backend plugins.
- qtsvg: Qt6Svg, Qt6SvgWidgets, SVG image and icon plugins.
- qtimageformats: ICNS/TGA/TIFF/WBMP/WebP plugins and their bundled source.
- qtshadertools: Qt6ShaderTools, if pulled in by the final DLL dependency closure.
- qttranslations: Qt .qm translation resources, if present.
- pyside-setup: PySide6 and Shiboken6 Python bindings/generator.

The latter two conditional Qt modules are small and included defensively. Final
packaging runs packaging/verify_qt_sources.py over actual standalone DLLs and
plugins before creating either deliverable. Unknown Qt modules fail the gate;
they must not be silently declared covered by these archives. In particular,
qpdf.dll is excluded: PDF image loading is not an application requirement, and
QtPdf needs qtwebengine sources, which are not included. QML/Quick/WebEngine are
not mapped and will fail the gate if unexpectedly included. The 1.03 GiB
qt-everywhere archive has NOT been downloaded.

## Rebuild / replacement guidance

These archives retain each module's configure scripts, CMake files, LICENSES,
third-party source/notices and upstream documentation. Extract archives without
flattening their directory structures. Use an x64 VS developer command prompt.
The observed application build environment is Windows 11 x64, CPython 3.12.15,
VS 2022 Community MSVC 14.44.35207, Nuitka 2.7.12, PySide6/Shiboken6 6.9.1.
output/distribution/toolchain-environment.json records local SDK/tool details.
This is NOT a claim that the upstream wheels were built with this same compiler.

A starting shared-library Qt rebuild sequence (adapt paths and install prefix):

```bat
mkdir qtbase-build
cd qtbase-build
..\qtbase-everywhere-src-6.9.1\configure.bat -prefix C:\qt-rebuilt -release -shared -opensource -confirm-license -nomake tests -nomake examples
cmake --build . --parallel
cmake --install .
cd ..
mkdir qtsvg-build
cd qtsvg-build
C:\qt-rebuilt\bin\qt-configure-module.bat ..\qtsvg-everywhere-src-6.9.1
cmake --build . --parallel
cmake --install .
```

Use separate build directories for each additional module; run the installed
qt-configure-module.bat with the absolute source path from that build directory.
Repeat for qtimageformats/qtshadertools/qttranslations when required. Install the
CMake/Ninja and other tools requested by Qt 6.9.1's included build documentation.
Building PySide/Shiboken additionally requires the supported libclang development
package and Python development files. In the extracted pyside-setup tree, follow
its README and sources/pyside6/doc build instructions; select Qt with --qtpaths
pointing at the rebuilt Qt installation, and build matching shiboken6/PySide6
wheels for CPython 3.12 x64. Build commands/tools are guidance, not a tested
wheel-reproduction recipe; upstream wheel flags and patches require review.

Keep separate dynamic DLLs/plugins in their existing paths and preserve the Qt
6.9 ABI and x64 Python extension interface when replacing components. The app
does not prohibit reverse engineering to debug modifications. After replacement,
run CLI/GUI/conversion smoke tests. No rebuild or replacement test has yet run.

## rawpy / LibRaw rebuild inputs

rawpy 0.25.1 reports LibRaw 0.21.4. Its _rawpy.cp312-win_amd64.pyd imports the
separate raw_r.dll, not a statically embedded LibRaw. raw_r.dll itself statically
links codec dependencies. The rawpy v0.25.1 git tree pins LibRaw commit
9646d776c7c61976080a8f2be67928df0750493e and LibRaw-cmake commit
6e26c9e73677dc04f9eb236a97c6a4dc225ba7e8; both complete archives are included.
The copied upstream build reference files retain rawpy's MIT license. The
rawpy source archive is deliberately not included because it embeds large
third-party RAW test photographs whose redistribution was not reviewed.

Upstream Windows build reference uses vcpkg 2025.01.13, x64-windows-static,
installing zlib 1.3.1, libjpeg-turbo 3.1.0 with jpeg8, JasPer 4.2.4 and LCMS 2.16.
Those source archives and the complete pinned vcpkg build/patch tree are included.
JasPer's default port can acquire freeglut/OpenGL build dependencies; they are not
shown as imports of the inspected raw_r.dll. Their link inclusion must be checked
against upstream build records before declaring a complete native dependency set.
The vcpkg LCMS port also downloads an export patch (commit
f7b3c637c20508655f8b49935a4b556d52937b69); a copy is included under
build-reference/rawpy with SHA512 matching the port's expected digest.
Network-free recreation of the upstream build cache has not been
established. Inspect the exact scripts before running; do not execute them on a
host without authorizing their dependency installs.

To rebuild a compatible raw_r.dll: extract the LibRaw and LibRaw-cmake trees;
configure LibRaw-cmake with LIBRAW_PATH pointing at LibRaw, BUILD_SHARED_LIBS=ON,
CMAKE_BUILD_TYPE=Release, ENABLE_OPENMP=ON, ENABLE_X3FTOOLS=ON,
ENABLE_6BY9RPI=ON, ENABLE_EXAMPLES=OFF, ENABLE_RAWSPEED=OFF, and
CMAKE_PREFIX_PATH pointing at the static dependency installation. Use the
upstream NMake/MSVC build/install sequence recorded in setup.py. Do not enable
GPL2/GPL3 demosaic packs (both disabled in the shipped wheel). Preserve exported
LibRaw ABI and x64 architecture, and replace rawpy/raw_r.dll without embedding
it in the executable. LGPL-2.1 text and source notices are retained. This recipe
is source-backed guidance, not a completed rebuild/replacement verification.

The application's independent LCMS 2.17 runtime is not rawpy's internal LCMS 2.16.
Its worker-provided DLL/LICENSE/SOURCE.json remain unchanged in the package.
OpenCV's optional FFmpeg video plugin is omitted: isolated PNG/JPEG/TIFF and ORB
checks pass without it. The final DLL inventory rejects accidental inclusion.
Microsoft VC/OpenMP runtimes are separately licensed, not covered by these
LGPL source archives.

## Remaining release review

Actual compiled payload inventory is unavailable until source readiness/build.
Match every retained binary against its source/version/module and license,
including third-party code linked into upstream Qt wheels. Verify upstream wheel
patches/configuration and any external OpenSSL, graphics/runtime DLLs, rather
than assuming module-version matching alone is sufficient. Retain all upstream
notices and LGPL/GPL texts. Windows SDK/Direct3D and MSVC runtime redistribution
have separate license conditions. Source archives are shipped with local
artifacts, but public redistribution remains explicitly uncleared pending review.
