"""Theme-aware colours for the LUT Check charts and finding rows.

Backgrounds and text come from the active palette (so Light/Dark and the
system theme apply); only the data colours below are fixed.  Each data colour
is chosen to reach at least 3:1 contrast (WCAG 2.2 non-text contrast) against
the palette ``Base`` colour in both themes; ``tests/test_gui_diagnose.py``
checks this.  Colour is never the only carrier of meaning: curves have a
legend and a value read-out, severities have icons and words, flagged points
have a ring shape.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

DATA_LIGHT = {
    "r": "#c42b1c", "g": "#107c10", "b": "#0f6cbd",
    "flag": "#b3410a", "ok": "#107c10", "info": "#0f6cbd", "warning": "#8f5200", "error": "#c42b1c",
}
DATA_DARK = {
    "r": "#ff7b6e", "g": "#6ccb5f", "b": "#62b4ff",
    "flag": "#ffb454", "ok": "#6ccb5f", "info": "#62b4ff", "warning": "#ffc83d", "error": "#ff7b6e",
}


def is_dark(palette: QPalette) -> bool:
    return palette.color(QPalette.ColorRole.Base).lightness() < 128


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """``a`` blended towards ``b`` by ``t`` (0..1), opaque."""
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def chart_colors(palette: QPalette) -> dict[str, QColor]:
    """Colour tokens for a chart painted with ``palette``."""
    base = palette.color(QPalette.ColorRole.Base)
    text = palette.color(QPalette.ColorRole.Text)
    data = DATA_DARK if is_dark(palette) else DATA_LIGHT
    colors = {k: QColor(v) for k, v in data.items()}
    colors.update({
        "bg": base,
        "text": text,
        "muted": _mix(text, base, 0.30),        # secondary text and identity line (>= 4.5:1 on the background)
        "grid": _mix(base, text, 0.16),          # gridlines (decorative)
        "axis": _mix(base, text, 0.55),
        "frame": _mix(base, text, 0.30),
        "focus": palette.color(QPalette.ColorRole.Highlight),
    })
    return colors


def severity_color(palette: QPalette, severity: str) -> QColor:
    data = DATA_DARK if is_dark(palette) else DATA_LIGHT
    return QColor(data.get(severity, data["info"]))


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color: QColor) -> float:
    return 0.2126 * _linear(color.redF()) + 0.7152 * _linear(color.greenF()) + 0.0722 * _linear(color.blueF())


def contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG 2.x contrast ratio of two opaque colours."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)
