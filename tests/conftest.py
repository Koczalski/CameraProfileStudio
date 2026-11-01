"""Skip tests that need the third-party sample camera profiles when absent.

The profiles in ``input/ccd_profiles`` are not distributed with the source
repository (their redistribution terms are unverified).  See
``input/ccd_profiles/README.md`` for where to obtain them.  Tests that open
them by name, or that use the ``fx`` GUI fixture set built from them, are
skipped instead of failing when any of them is missing.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

PROFILE_DIR = Path(__file__).resolve().parents[1] / "input" / "ccd_profiles"
REQUIRED_PROFILES = (
    "Kodak_DCS760.icc", "Kodak_ProBack645.icc", "Nikon_D200.dcp", "Nikon_D200.icc", "Nikon_D70.icc",
    "Nikon_D70s.dcp", "Olympus_E1.dcp", "Sony_DSC-F828.icc", "Sony_DSC-V3.icc",
)
_MARKERS = ("PROFILES", "ccd_profiles")
_PROFILE_FIXTURES = {"fx"}


def missing_profiles() -> list[str]:
    return [name for name in REQUIRED_PROFILES if not (PROFILE_DIR / name).is_file()]


def _needs_profiles(item) -> bool:
    if _PROFILE_FIXTURES & set(getattr(item, "fixturenames", ())):
        return True
    function = getattr(item, "function", None)
    if function is None:
        return False
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return False
    return any(marker in source for marker in _MARKERS)


def pytest_collection_modifyitems(config, items):
    missing = missing_profiles()
    if not missing:
        return
    reason = ("sample camera profiles not installed in input/ccd_profiles "
              f"(missing: {', '.join(missing)}); see input/ccd_profiles/README.md")
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if _needs_profiles(item):
            item.add_marker(skip)
