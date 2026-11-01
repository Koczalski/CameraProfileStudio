"""Live-switchable Japanese/English UI text.

Every user-visible string in the GUI is looked up through :data:`T` with a
stable key.  Widgets register *bindings* (object, setter, key, kwargs); when the
language changes all bindings are re-applied, so switching language never
needs a restart.  Dynamic content (log entries, job states, errors) stores the
key and arguments rather than the rendered text and re-renders on
:pyattr:`Translator.changed`.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import QObject, Signal

from .strings import STRINGS

SUPPORTED_LANGUAGES = ("en", "ja")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - defensive
        return "{" + key + "}"


class Translator(QObject):
    """Holds the current language and re-applies registered bindings."""

    changed = Signal(str)

    def __init__(self, language: str = "en") -> None:
        super().__init__()
        self._language = language if language in SUPPORTED_LANGUAGES else "en"
        self._bindings: list[tuple[QObject, str, str, dict[str, Any]]] = []
        self._watchers: list[tuple[QObject, Callable[[], None]]] = []

    # ------------------------------------------------------------------ lookup
    @property
    def language(self) -> str:
        return self._language

    def text(self, key: str, **kwargs: Any) -> str:
        table = STRINGS.get(self._language, {})
        template = table.get(key)
        if template is None:
            template = STRINGS["en"].get(key, key)
        if kwargs:
            try:
                return template.format_map(_SafeDict(kwargs))
            except (ValueError, IndexError):  # pragma: no cover - malformed template
                return template
        return template

    __call__ = text

    # --------------------------------------------------------------- language
    def set_language(self, language: str) -> None:
        if language not in SUPPORTED_LANGUAGES or language == self._language:
            return
        self._language = language
        self._reapply()
        self.changed.emit(language)

    # --------------------------------------------------------------- bindings
    def bind(self, obj: QObject, setter: str, key: str, **kwargs: Any) -> None:
        """Call ``getattr(obj, setter)(text(key))`` now and on every switch.

        Re-binding the same (obj, setter) replaces the previous key so callers
        can update dynamic labels through the same mechanism.
        """
        self._bindings = [b for b in self._bindings if not (b[0] is obj and b[1] == setter)]
        self._bindings.append((obj, setter, key, kwargs))
        self._apply_one(obj, setter, key, kwargs)

    def watch(self, obj: QObject, callback: Callable[[], None]) -> None:
        """Call ``callback()`` after each language switch while ``obj`` lives.

        Unlike connecting a lambda to :pyattr:`changed`, this never calls into
        a deleted widget.
        """
        self._watchers.append((obj, callback))

    def unbind(self, obj: QObject) -> None:
        self._bindings = [b for b in self._bindings if b[0] is not obj]

    def _apply_one(self, obj: QObject, setter: str, key: str, kwargs: dict[str, Any]) -> bool:
        value = self.text(key, **kwargs)
        if setter in ("setAccessibleName", "setWindowTitle"):
            value = strip_mnemonic(value)
        if not shiboken6.isValid(obj):
            return False
        try:
            getattr(obj, setter)(value)
            return True
        except RuntimeError:  # underlying C++ object already deleted
            return False

    def _reapply(self) -> None:
        alive = []
        for binding in self._bindings:
            if self._apply_one(*binding):
                alive.append(binding)
        self._bindings = alive
        watchers = []
        for obj, callback in self._watchers:
            if not shiboken6.isValid(obj):
                continue
            try:
                callback()
            except RuntimeError:
                continue
            watchers.append((obj, callback))
        self._watchers = watchers

    def missing_keys(self) -> dict[str, set[str]]:
        """Keys present in one language but not the other (for tests)."""
        en, ja = set(STRINGS["en"]), set(STRINGS["ja"])
        return {"ja": en - ja, "en": ja - en}


_MNEMONIC_JA = re.compile(r"\(&.\)")


def strip_mnemonic(text: str) -> str:
    """Remove Qt mnemonic markers (``&Open`` / ``髢九￥(&O)``) for a11y names."""
    text = _MNEMONIC_JA.sub("", text)
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


T = Translator()


def tr(key: str, **kwargs: Any) -> str:
    return T.text(key, **kwargs)


def bind(obj: QObject, setter: str, key: str, **kwargs: Any) -> None:
    T.bind(obj, setter, key, **kwargs)


def bind_text(widget: QObject, key: str, *, accessible: bool = True, tooltip: str | None = None, **kwargs: Any) -> None:
    """Common helper: bind visible text and (optionally) the accessible name."""
    T.bind(widget, "setText", key, **kwargs)
    if accessible and hasattr(widget, "setAccessibleName"):
        T.bind(widget, "setAccessibleName", key, **kwargs)
    if tooltip:
        T.bind(widget, "setToolTip", tooltip)


def on_language_changed(callback: Callable[[str], None]) -> None:
    T.changed.connect(callback)

