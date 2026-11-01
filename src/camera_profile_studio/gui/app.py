"""Application bootstrap: DPI policy, native style, palette/theme, language.

Style choice follows the Qt QStyle documentation: Qt ships the ``windows11``
style plugin (Fluent) on Windows; ``QStyleFactory.keys()`` lists what is
available at runtime.  We prefer ``windows11`` and fall back to
``windowsvista`` and finally the built-in ``Fusion``.  No application-wide
style sheet is installed.
"""

from __future__ import annotations

import os
import sys
from typing import Sequence

from PySide6.QtCore import QCoreApplication, QSettings, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QStyleFactory

from .i18n import T

ORGANIZATION = "CameraProfileStudio"
APPLICATION = "Camera Profile Studio"
STYLE_PREFERENCE = ("windows11", "windowsvista", "Fusion")
THEMES = ("system", "light", "dark")


def preferred_style(available: Sequence[str] | None = None) -> str:
    keys = [k.lower() for k in (available if available is not None else QStyleFactory.keys())]
    for name in STYLE_PREFERENCE:
        if name.lower() in keys:
            return name
    return "Fusion"


def configure_high_dpi() -> None:
    """Must run before QApplication exists.

    Qt 6 is always high-DPI aware (per-monitor v2 on Windows).  PassThrough
    keeps fractional factors such as 150 % instead of rounding to 2x.
    """
    if QCoreApplication.instance() is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def apply_theme(theme: str) -> None:
    """Light / Dark / follow system via QStyleHints.setColorScheme (Qt >= 6.8).

    The active QStyle derives its palette from the colour scheme, so no
    custom palette or QSS is applied.
    """
    app = QGuiApplication.instance()
    if app is None:
        return
    hints = app.styleHints()
    scheme = {
        "light": Qt.ColorScheme.Light,
        "dark": Qt.ColorScheme.Dark,
    }.get(theme, Qt.ColorScheme.Unknown)
    if hasattr(hints, "setColorScheme"):
        hints.setColorScheme(scheme)


def current_color_scheme() -> str:
    app = QGuiApplication.instance()
    if app is None:
        return "unknown"
    scheme = app.styleHints().colorScheme()
    return {Qt.ColorScheme.Light: "light", Qt.ColorScheme.Dark: "dark"}.get(scheme, "unknown")


def settings() -> QSettings:
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, ORGANIZATION, "CameraProfileStudio")


def default_language() -> str:
    from PySide6.QtCore import QLocale

    return "ja" if QLocale.system().language() == QLocale.Language.Japanese else "en"


def create_application(argv: Sequence[str] | None = None, *, style: str | None = None) -> QApplication:
    configure_high_dpi()
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setOrganizationName(ORGANIZATION)
    app.setApplicationName(APPLICATION)
    app.setApplicationDisplayName(APPLICATION)
    requested = style or os.environ.get("CPS_STYLE")
    name = requested if requested and requested.lower() in [k.lower() for k in QStyleFactory.keys()] else preferred_style()
    app.setStyle(name)
    from . import icons

    if icons.available():
        app.setWindowIcon(icons.icon("color"))
    store = settings()
    T.set_language(str(store.value("ui/language", default_language())))
    apply_theme(str(store.value("ui/theme", "system")))
    return app


def main(argv: Sequence[str] | None = None) -> int:
    app = create_application(argv)
    from .main_window import MainWindow

    window = MainWindow()
    window.show()
    paths = [a for a in (argv if argv is not None else sys.argv)[1:] if not a.startswith("-")]
    if paths:
        window.open_profiles(paths)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
