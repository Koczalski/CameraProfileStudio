"""Lazy, headless orchestration with strict color-domain checks and provenance."""
from __future__ import annotations

from dataclasses import asdict, replace
import importlib
import json
from pathlib import Path

from . import __version__
from .errors import (DependencyError, InvalidProfileError, UnsupportedError, ValidationError, checkpoint)
from .types import (Capability, DcpSettings, JobContext, Lut3D, ProfileInfo, RGB, RGBTransform,
                    SpaceSpec, TransformRequest)


def _module(name: str):
    try:
        return importlib.import_module(f'.{name}', __package__)
    except ImportError as exc:
        raise DependencyError(f'The {name} engine or its dependency is unavailable.',
                              code='missing_module', details=(('module', name), ('cause', str(exc)))) from exc


def _format(path: Path) -> str:
    try:
        with Path(path).open('rb') as stream:
            header = stream.read(40)
    except OSError as exc:
        raise InvalidProfileError(f'Cannot read profile: {path}', code='profile_io') from exc
    if header[36:40] == b'acsp' or Path(path).suffix.lower() in ('.icc', '.icm'):
        return 'icc'
    if header[:2] in (b'II', b'MM') or Path(path).suffix.lower() == '.dcp':
        return 'dcp'
    raise UnsupportedError('File is not a recognized ICC/ICM/DCP profile.', code='unsupported_profile_format')


def inspect_profile(path: Path, *, context: JobContext | None = None) -> ProfileInfo:
    checkpoint(context, stage='profile.inspect', fraction=0)
    if _format(path) == 'dcp':
        info = _module('dcp').read_dcp(path, context=context).info
    else:
        info = _module('icc').inspect_icc(path, context=context)
        try:
            version = _module('lcms').probe_lcms()
        except DependencyError as exc:
            info = replace(info, capabilities=tuple(
                Capability(cap.feature, 'unsupported', cap.reasons + (exc.message,))
                if cap.feature.startswith('technical') else cap for cap in info.capabilities),
                warnings=info.warnings + ('ICC parsing succeeded, but the native transform runtime is unavailable.',))
        else:
            info = replace(info, warnings=info.warnings + (f'LittleCMS runtime {version}; intent support is checked when creating the requested transform.',))
    checkpoint(context, stage='profile.inspect', fraction=1)
    return info


class _ProvenanceTransform:
    def __init__(self, transform: RGBTransform, provenance: dict[str, str]) -> None:
        self._transform = transform
        self.input_space, self.output_space = transform.input_space, transform.output_space
        self.provenance = tuple({**dict(getattr(transform, 'provenance', ())), **provenance}.items())

    def apply(self, rgb: RGB, *, context: JobContext | None = None) -> RGB:
        return self._transform.apply(rgb, context=context)

    def close(self) -> None:
        self._transform.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _same_path(first: Path | None, second: Path | None) -> bool:
    return first is not None and second is not None and first.resolve() == second.resolve()


def create_transform(request: TransformRequest, *, context: JobContext | None = None) -> RGBTransform:
    checkpoint(context, stage='service.create', fraction=0)
    if not isinstance(request, TransformRequest):
        raise ValidationError('Expected TransformRequest.', code='invalid_request')
    profile_path = request.profile_path
    if request.input_space.kind == 'camera_linear':
        if profile_path is None:
            profile_path = request.input_space.profile_path
        elif not _same_path(profile_path, request.input_space.profile_path):
            raise ValidationError('Camera input profile and selected DCP differ.', code='profile_mismatch')
    if request.output_space.kind == 'camera_linear':
        raise UnsupportedError('Inverse DCP/camera-linear output is not supported.', code='unsupported_direction')
    profile_format = _format(profile_path) if profile_path is not None else None
    provenance = {
        'application_version': __version__,
        'request': json.dumps(asdict(request), default=str, sort_keys=True, allow_nan=False),
        'jpeg_accuracy': 'unverified',
        'input_domain': json.dumps(asdict(request.input_space), default=str, sort_keys=True),
        'output_domain': json.dumps(asdict(request.output_space), default=str, sort_keys=True),
        'gamut': request.gamut,
    }
    if request.mode == 'look' or profile_format == 'dcp' or request.input_space.kind == 'camera_linear':
        if profile_path is None or profile_format != 'dcp':
            raise UnsupportedError('Look extraction requires a DCP with actual look elements.', code='unsupported_look')
        if request.intent != 'relative' or request.black_point_compensation:
            raise UnsupportedError('ICC intent/BPC options do not apply to the DCP renderer.', code='unsupported_settings')
        settings = request.dcp or DcpSettings(gamut=request.gamut)
        if settings.gamut != request.gamut:
            raise ValidationError('Request and DCP gamut policies must agree.', code='conflicting_settings')
        if request.output_space != SpaceSpec('srgb'):
            raise UnsupportedError('DCP rendering currently outputs encoded sRGB only.', code='unsupported_space')
        if request.mode == 'look':
            if request.input_space != SpaceSpec('srgb'):
                raise UnsupportedError('Photo look extraction requires encoded sRGB input.', code='unsupported_space')
        elif request.input_space.kind != 'camera_linear' or not _same_path(request.input_space.profile_path, profile_path):
            raise UnsupportedError('Technical DCP rendering requires explicit matching camera-linear input.', code='unsupported_space')
        elif request.look_mode != 'combined':
            raise ValidationError('look_mode applies only to look extraction.', code='unused_settings')
        profile = _module('dcp').read_dcp(profile_path, context=context)
        if request.mode == 'look':
            transform = _module('look').create_look_transform(profile, mode=request.look_mode, settings=settings, context=context)
            provenance['experimental'] = 'true'
            provenance['interpretation'] = 'Profile-derived reference-space photo look; not inverse sensor recovery or OEM JPEG reproduction.'
        else:
            transform = _module('dcp').create_dcp_transform(profile, settings=settings, context=context)
            provenance['experimental'] = 'false'
            provenance['interpretation'] = 'Standalone DCP technical rendering; identity individual camera calibration assumed; not an OEM JPEG pipeline.'
        provenance['source_profile_sha256'] = profile.info.sha256
        provenance['profile_warnings'] = json.dumps(profile.info.warnings)
        provenance['dcp_settings'] = json.dumps(asdict(settings), sort_keys=True, allow_nan=False)
    else:
        if request.dcp is not None or request.look_mode != 'combined':
            raise ValidationError('DCP/look settings are not applicable to ICC conversion.', code='unused_settings')
        if profile_path is not None and not any(_same_path(profile_path, space.profile_path) for space in (request.input_space, request.output_space)):
            raise ValidationError('Selected ICC must be explicitly assigned as input or output space.', code='unassigned_profile')
        transform = _module('lcms').create_icc_transform(request.input_space, request.output_space,
            intent=request.intent, black_point_compensation=request.black_point_compensation,
            gamut=request.gamut, context=context)
        provenance['lcms_version'] = transform.runtime_version
        for direction, digest in transform.profile_hashes.items():
            provenance[f'{direction}_profile_sha256'] = digest
        provenance['experimental'] = 'false'
        provenance['interpretation'] = 'ICC technical conversion; device RGB encoding follows the actual profile, not assumed camera-linear.'
    try:
        wrapped = _ProvenanceTransform(transform, provenance)
        checkpoint(context, stage='service.create', fraction=1)
        return wrapped
    except BaseException:
        transform.close()
        raise


def convert_profile(request: TransformRequest, *, size: int = 33, strength: float = 1.0,
                    context: JobContext | None = None) -> Lut3D:
    transform = create_transform(request, context=context)
    try:
        return _module('lut').generate_lut(transform, size=size, strength=strength, context=context)
    finally:
        transform.close()
