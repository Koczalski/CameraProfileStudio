"""LittleCMS 2 C API wrapper using native float32 RGB buffers exclusively."""
from __future__ import annotations

import ctypes as C
import ctypes.util
from contextlib import ExitStack
from importlib import resources
import os
from pathlib import Path
import threading

import numpy as np

from .errors import DependencyError, InvalidProfileError, UnsupportedError, ValidationError, checkpoint
from .icc import _parse_icc, _read_profile
from .types import Gamut, Intent, JobContext, RGB, RGBTransform, SpaceSpec

TYPE_RGB_FLT = 0x0044001C
_INTENTS = {'perceptual': 0, 'relative': 1, 'saturation': 2, 'absolute': 3}
_ERROR_CALLBACK = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.c_char_p)
_RESOURCE_LIFETIME = ExitStack()
_LOAD_LOCK = threading.RLock()
_LIBRARIES: dict[str, C.CDLL] = {}


class _xyY(C.Structure):
    _fields_ = [('x', C.c_double), ('y', C.c_double), ('Y', C.c_double)]


class _Primaries(C.Structure):
    _fields_ = [('Red', _xyY), ('Green', _xyY), ('Blue', _xyY)]


def _bind(lib: C.CDLL) -> None:
    signatures = {
        'cmsGetEncodedCMMversion': (C.c_uint32, []),
        'cmsCreateContext': (C.c_void_p, [C.c_void_p, C.c_void_p]),
        'cmsDeleteContext': (None, [C.c_void_p]),
        'cmsSetLogErrorHandlerTHR': (None, [C.c_void_p, _ERROR_CALLBACK]),
        'cmsOpenProfileFromMemTHR': (C.c_void_p, [C.c_void_p, C.c_void_p, C.c_uint32]),
        'cmsCreate_sRGBProfileTHR': (C.c_void_p, [C.c_void_p]),
        'cmsBuildGamma': (C.c_void_p, [C.c_void_p, C.c_double]),
        'cmsFreeToneCurve': (None, [C.c_void_p]),
        'cmsCreateRGBProfileTHR': (C.c_void_p, [C.c_void_p, C.POINTER(_xyY), C.POINTER(_Primaries), C.POINTER(C.c_void_p)]),
        'cmsCloseProfile': (C.c_int, [C.c_void_p]),
        'cmsIsIntentSupported': (C.c_int, [C.c_void_p, C.c_uint32, C.c_uint32]),
        'cmsCreateTransformTHR': (C.c_void_p, [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32]),
        'cmsDoTransform': (None, [C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint32]),
        'cmsDeleteTransform': (None, [C.c_void_p]),
        'cmsSaveProfileToMem': (C.c_int, [C.c_void_p, C.c_void_p, C.POINTER(C.c_uint32)]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.restype = result
        function.argtypes = args


def _load_library(library_path: Path | None = None) -> C.CDLL:
    with _LOAD_LOCK:
        selected = str(library_path) if library_path is not None else os.environ.get('CPS_LCMS2_DLL')
        if not selected:
            dll_name = 'lcms2.dll' if os.name == 'nt' else 'liblcms2.dylib' if os.sys.platform == 'darwin' else 'liblcms2.so'
            bundled = resources.files('camera_profile_studio').joinpath('vendor', 'lcms2', dll_name)
            if bundled.is_file():
                selected = str(_RESOURCE_LIFETIME.enter_context(resources.as_file(bundled)))
            else:
                selected = ctypes.util.find_library('lcms2')
        if not selected:
            raise DependencyError('LittleCMS 2 runtime is not installed or bundled.', code='missing_lcms')
        if selected in _LIBRARIES:
            return _LIBRARIES[selected]
        try:
            lib = C.CDLL(selected)
            _bind(lib)
            version = lib.cmsGetEncodedCMMversion()
            if not 2000 <= version < 3000:
                raise ValueError(f'Unsupported LittleCMS version {version}')
        except (OSError, AttributeError, ValueError) as exc:
            raise DependencyError(f'Cannot load a compatible LittleCMS 2 runtime: {selected}', code='missing_lcms', details=(('library', selected),)) from exc
        _LIBRARIES[selected] = lib
        return lib


def probe_lcms(*, library_path: Path | None = None) -> str:
    version = _load_library(library_path).cmsGetEncodedCMMversion()
    return f'{version // 1000}.{(version % 1000) // 10}.{version % 10}'


class _ICCTransform:
    def __init__(self, input_space: SpaceSpec, output_space: SpaceSpec, *, intent: Intent,
                 black_point_compensation: bool, gamut: Gamut, context: JobContext | None) -> None:
        self.input_space, self.output_space = input_space, output_space
        self._lib = _load_library()
        self._lock = threading.RLock()
        self._context = None
        self._transform = None
        self._profiles: list[int] = []
        self._buffers: list[object] = []
        self._messages: list[str] = []
        self._gamut = gamut
        self._closed = False
        self.profile_hashes: dict[str, str] = {}
        self.runtime_version = probe_lcms()
        self._callback = _ERROR_CALLBACK(self._on_error)
        try:
            self._context = self._lib.cmsCreateContext(None, None)
            if not self._context:
                raise DependencyError('LittleCMS context allocation failed.', code='lcms_context_failed')
            self._lib.cmsSetLogErrorHandlerTHR(self._context, self._callback)
            source = self._profile(input_space, 'input', _INTENTS[intent])
            target = self._profile(output_space, 'output', _INTENTS[intent])
            checkpoint(context, stage='lcms.create', fraction=0.5)
            # Disable optimizers/cache: retain floating pipelines, including sub-8-bit differences.
            flags = 0x0040 | 0x0100 | (0x2000 if black_point_compensation else 0)
            self._transform = self._lib.cmsCreateTransformTHR(
                self._context, source, TYPE_RGB_FLT, target, TYPE_RGB_FLT, _INTENTS[intent], flags)
            if not self._transform or self._messages:
                raise UnsupportedError(self._failure('LittleCMS could not construct this transform.'), code='unsupported_direction')
            checkpoint(context, stage='lcms.create', fraction=1)
        except BaseException:
            self.close()
            raise

    def _on_error(self, context: int, code: int, text: bytes | None) -> None:
        self._messages.append(f'{code}: {(text or b"Unknown LCMS error").decode("utf-8", errors="replace")}')

    def _failure(self, message: str) -> str:
        return message + (' ' + '; '.join(self._messages) if self._messages else '')

    def _profile(self, space: SpaceSpec, direction: str, intent: int) -> int:
        if space.kind == 'srgb':
            profile = self._lib.cmsCreate_sRGBProfileTHR(self._context)
        elif space.kind == 'linear_srgb':
            white = _xyY(0.3127, 0.3290, 1)
            primaries = _Primaries(_xyY(0.64, 0.33, 1), _xyY(0.30, 0.60, 1), _xyY(0.15, 0.06, 1))
            curve = self._lib.cmsBuildGamma(self._context, 1.0)
            if not curve:
                raise InvalidProfileError(self._failure('Unable to create linear TRC.'), code='lcms_profile_failed')
            try:
                curves = (C.c_void_p * 3)(curve, curve, curve)
                profile = self._lib.cmsCreateRGBProfileTHR(self._context, C.byref(white), C.byref(primaries), curves)
            finally:
                self._lib.cmsFreeToneCurve(curve)
        else:
            data = _read_profile(space.profile_path)
            info = _parse_icc(data, space.profile_path)
            status = {cap.feature: cap for cap in info.capabilities}
            intent_name = next(name for name, value in _INTENTS.items() if value == intent)
            for feature in ('technical', f'technical.{direction}', f'technical.{direction}.{intent_name}'):
                if status[feature].status != 'supported':
                    raise UnsupportedError('; '.join(status[feature].reasons) or 'Unsupported ICC direction.', code='unsupported_direction')
            self.profile_hashes[direction] = info.sha256
            buffer = C.create_string_buffer(data)
            self._buffers.append(buffer)
            profile = self._lib.cmsOpenProfileFromMemTHR(self._context, buffer, len(data))
        if not profile:
            raise InvalidProfileError(self._failure('LittleCMS could not open the profile.'), code='lcms_profile_failed')
        self._profiles.append(profile)
        if not self._lib.cmsIsIntentSupported(profile, intent, 0 if direction == 'input' else 1):
            raise UnsupportedError(f'Profile does not support {direction} intent {intent}.', code='unsupported_intent')
        return profile

    def apply(self, rgb: RGB, *, context: JobContext | None = None) -> RGB:
        checkpoint(context, stage='lcms.transform', fraction=0)
        try:
            data = np.asarray(rgb)
            if data.dtype.kind not in 'iuf' or data.ndim < 1 or data.shape[-1] != 3:
                raise ValueError('Expected numeric RGB with final dimension 3.')
            if not np.all(np.isfinite(data)) or np.any(data < 0) or np.any(data > 1):
                raise ValueError('ICC device values must be finite and within [0, 1].')
            source = np.ascontiguousarray(data, dtype=np.float32)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(str(exc), code='invalid_rgb') from exc
        output = np.empty_like(source)
        flat_source, flat_output = source.reshape(-1, 3), output.reshape(-1, 3)
        with self._lock:
            if self._closed:
                raise ValidationError('Transform has been closed.', code='closed_transform')
            self._messages.clear()
            total = len(flat_source)
            for offset in range(0, total, 65536):
                checkpoint(context, stage='lcms.transform', fraction=offset / total)
                end = min(offset + 65536, total)
                self._lib.cmsDoTransform(self._transform, flat_source[offset:end].ctypes.data,
                                         flat_output[offset:end].ctypes.data, end - offset)
                if self._messages:
                    raise InvalidProfileError(self._failure('LittleCMS transform failed.'), code='lcms_transform_failed')
            if not np.all(np.isfinite(output)):
                raise InvalidProfileError('LittleCMS produced nonfinite values.', code='lcms_nonfinite')
            if self._gamut == 'error' and (np.any(output < -1e-6) or np.any(output > 1 + 1e-6)):
                raise ValidationError('Output lies outside the selected RGB gamut.', code='out_of_gamut')
            np.clip(output, 0, 1, out=output)
        checkpoint(context, stage='lcms.transform', fraction=1)
        return output

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._transform:
                self._lib.cmsDeleteTransform(self._transform)
                self._transform = None
            for profile in reversed(self._profiles):
                self._lib.cmsCloseProfile(profile)
            self._profiles.clear()
            if self._context:
                self._lib.cmsDeleteContext(self._context)
                self._context = None
            self._buffers.clear()

    def __enter__(self) -> _ICCTransform:
        if self._closed:
            raise ValidationError('Transform has been closed.', code='closed_transform')
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def create_icc_transform(input_space: SpaceSpec, output_space: SpaceSpec, *,
                         intent: Intent = 'relative', black_point_compensation: bool = False,
                         gamut: Gamut = 'clip', context: JobContext | None = None) -> RGBTransform:
    checkpoint(context, stage='lcms.create', fraction=0)
    if not isinstance(input_space, SpaceSpec) or not isinstance(output_space, SpaceSpec):
        raise ValidationError('ICC spaces must be SpaceSpec records.', code='invalid_space')
    if input_space.kind == 'camera_linear' or output_space.kind == 'camera_linear':
        raise UnsupportedError('DCP camera-linear RGB is not an ICC device encoding.', code='unsupported_space')
    if intent not in _INTENTS or gamut not in ('clip', 'error') or not isinstance(black_point_compensation, bool):
        raise ValidationError('Invalid ICC rendering options.', code='invalid_settings')
    return _ICCTransform(input_space, output_space, intent=intent,
                         black_point_compensation=black_point_compensation, gamut=gamut, context=context)
