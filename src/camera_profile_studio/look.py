"""Profile-derived sRGB look approximation, never camera-matrix inversion."""
from __future__ import annotations

import numpy as np

from .dcp import (_SRGB_TO_WORK, _WORK_TO_SRGB, _bounded, _components, _decode,
                  _encode, _input, _run_components, _settings, _unsupported,
                  _validate_tags, _weight)
from .errors import InvalidProfileError, UnsupportedError, ValidationError, checkpoint
from .types import Capability, DcpProfile, DcpSettings, JobContext, LookMode, RGB, RGBTransform, SpaceSpec

_MODES = {
    'colors_only': (True, True, False),
    'tone_only': (False, False, True),
    'look_only': (False, True, False),
    'combined': (True, True, True),
}
_COMPONENT_TAGS = (50938, 50982, 50940)
_COMPONENT_NAMES = ('HueSatMap', 'LookTable', 'ToneCurve')


def _selection(profile: DcpProfile, mode: LookMode, settings: DcpSettings) -> tuple[bool, bool, bool]:
    if mode not in _MODES:
        raise ValidationError(f'Unknown look mode: {mode}', code='invalid_look_mode')
    tags = dict(profile.tags)
    switches = (settings.include_hue_sat_map, settings.include_look_table, settings.include_tone_curve)
    active = tuple(selected and switch and tag in tags for selected, switch, tag
                   in zip(_MODES[mode], switches, _COMPONENT_TAGS))
    if not any(active):
        raise UnsupportedError(f'No enabled, present profile components for {mode}; no look will be invented', code='missing_look_components')
    return active


def look_capabilities(profile: DcpProfile) -> tuple[Capability, ...]:
    tags = dict(profile.tags)
    caps = []
    for mode, selected in _MODES.items():
        present = tuple(want and tag in tags for want, tag in zip(selected, _COMPONENT_TAGS))
        reasons = ['Experimental sRGB-to-sRGB profile-derived approximation; camera JPEG accuracy unverified.',
                   'Uses reference linear ProPhoto conversion, not an inverse camera input matrix.']
        if present[0]:
            reasons.append('HueSatMap is camera-input calibration, not evidence of camera JPEG look design.')
        if present[2]:
            reasons.append('Hue-preserving tone uses monotone PCHIP rather than the Adobe spline.')
        for want, exists, name in zip(selected, present, _COMPONENT_NAMES):
            if want and not exists:
                reasons.append(f'{name} absent; omitted, not synthesized.')
        try:
            if not any(present):
                raise UnsupportedError('No applicable components; matrix-only profiles have no JPEG-look information.', code='missing_look_components')
            _unsupported(tags, present, technical=False)
            if present[0] and 50939 in tags:
                _weight(tags, 5000., dual=True)
                reasons.append('Dual HueSatMap requires explicit illuminant_kelvin.')
            reasons.append('Host default black/shadow and exposure rendering are not inferred.')
            status = 'partial'
        except (UnsupportedError, InvalidProfileError) as exc:
            status = 'unsupported'
            reasons.append(str(exc))
        caps.append(Capability(f'look.{mode}', status, tuple(reasons)))
    available = [c for c in caps if c.status != 'unsupported']
    general = Capability('look', 'partial' if available else 'unsupported',
                         ('Profile-derived approximation only; no verified OEM JPEG reproduction.',
                          'Check per-mode capability and enabled components; absent components are never invented.')
                         if available else ('No supported look components for the default modes.',))
    return (general, *caps)


class _LookTransform:
    def __init__(self, profile: DcpProfile, mode: LookMode, settings: DcpSettings):
        _settings(settings, technical=False)
        tags = dict(profile.tags)
        _validate_tags(tags)
        active = _selection(profile, mode, settings)
        _unsupported(tags, active, technical=False)
        self.input_space = SpaceSpec('srgb')
        self.output_space = SpaceSpec('srgb')
        self._tags = tags
        self._components = _components(tags, settings, active)
        self._gamut = settings.gamut
        self._closed = False

    def apply(self, rgb: RGB, *, context: JobContext | None = None) -> RGB:
        checkpoint(context, stage='look.apply', fraction=0.)
        if self._closed:
            raise ValidationError('Transform is closed', code='closed_transform')
        a = _input(rgb, self._gamut)
        flat = a.reshape(-1, 3)
        out = np.empty_like(flat, dtype=np.float32)
        for start in range(0, len(flat), 32768):
            checkpoint(context, stage='look.apply', fraction=start / max(len(flat), 1))
            work = _bounded(_decode(flat[start:start + 32768]) @ _SRGB_TO_WORK.T,
                            self._gamut, 'look linear ProPhoto')
            work = _run_components(work, self._tags, self._components, self._gamut)
            linear = _bounded(work @ _WORK_TO_SRGB.T, self._gamut, 'look linear sRGB output')
            out[start:start + 32768] = _encode(linear)
        checkpoint(context, stage='look.apply', fraction=1.)
        return out.reshape(a.shape)

    def close(self) -> None:
        self._closed = True


def create_look_transform(profile: DcpProfile, *, mode: LookMode = 'combined',
                          settings: DcpSettings = DcpSettings(),
                          context: JobContext | None = None) -> RGBTransform:
    checkpoint(context, stage='look.prepare', fraction=0.)
    transform = _LookTransform(profile, mode, settings)
    checkpoint(context, stage='look.prepare', fraction=1.)
    return transform


def apply_look(rgb: RGB, profile: DcpProfile, *, mode: LookMode = 'combined',
               settings: DcpSettings = DcpSettings(), context: JobContext | None = None) -> RGB:
    transform = create_look_transform(profile, mode=mode, settings=settings, context=context)
    try:
        return transform.apply(rgb, context=context)
    finally:
        transform.close()
