"""Reproducible LUT interpolation evidence against a nonidentity, real LCMS transform.

Run: python scripts/validate_lut_quality.py --output output/lut-quality
This measures interpolation fidelity, NOT camera JPEG reproduction accuracy.
"""
from __future__ import annotations

import argparse
import ctypes as C
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw

from camera_profile_studio.color import delta_e00, srgb_to_lab
from camera_profile_studio.lcms import probe_lcms
from camera_profile_studio.service import create_transform
from camera_profile_studio.lut import export_cube, generate_lut, read_cube, sample_lut
from camera_profile_studio.types import SpaceSpec, TransformRequest


class _xyY(C.Structure):
    _fields_ = [('x', C.c_double), ('y', C.c_double), ('Y', C.c_double)]


class _Primaries(C.Structure):
    _fields_ = [('red', _xyY), ('green', _xyY), ('blue', _xyY)]


def create_gamma_fixture(path: Path, gamma: float = 1.8) -> None:
    """Create a redistributable analytic D65/Rec.709 matrix/TRC ICC, not a camera profile."""
    import camera_profile_studio
    name = 'lcms2.dll' if os.name == 'nt' else 'liblcms2.dylib' if sys.platform == 'darwin' else 'liblcms2.so'
    bundled = Path(camera_profile_studio.__file__).parent / 'vendor' / 'lcms2' / name
    library = os.environ.get('CPS_LCMS2_DLL') or (str(bundled) if bundled.exists() else ctypes.util.find_library('lcms2'))
    if not library:
        raise RuntimeError('A real LCMS runtime is required for quality evidence')
    lib = C.CDLL(library)
    for function, result, args in (
        ('cmsBuildGamma', C.c_void_p, [C.c_void_p, C.c_double]),
        ('cmsFreeToneCurve', None, [C.c_void_p]),
        ('cmsCreateRGBProfile', C.c_void_p, [C.POINTER(_xyY), C.POINTER(_Primaries), C.POINTER(C.c_void_p)]),
        ('cmsSaveProfileToMem', C.c_int, [C.c_void_p, C.c_void_p, C.POINTER(C.c_uint32)]),
        ('cmsCloseProfile', C.c_int, [C.c_void_p]),
    ):
        getattr(lib, function).restype = result
        getattr(lib, function).argtypes = args
    curve = lib.cmsBuildGamma(None, gamma)
    if not curve:
        raise RuntimeError('LCMS could not allocate fixture tone curve')
    profile = None
    try:
        white = _xyY(.3127, .3290, 1)
        primaries = _Primaries(_xyY(.64, .33, 1), _xyY(.30, .60, 1), _xyY(.15, .06, 1))
        profile = lib.cmsCreateRGBProfile(C.byref(white), C.byref(primaries), (C.c_void_p*3)(curve, curve, curve))
        if not profile:
            raise RuntimeError('LCMS could not create fixture profile')
        length = C.c_uint32()
        if not lib.cmsSaveProfileToMem(profile, None, C.byref(length)):
            raise RuntimeError('LCMS could not measure fixture profile')
        buffer = C.create_string_buffer(length.value)
        if not lib.cmsSaveProfileToMem(profile, buffer, C.byref(length)):
            raise RuntimeError('LCMS could not serialize fixture profile')
        data = bytearray(buffer.raw[:length.value])
        data[24:36] = struct.pack('>6H', 2026, 1, 1, 0, 0, 0)
        path.write_bytes(data)
    finally:
        if profile:
            lib.cmsCloseProfile(profile)
        lib.cmsFreeToneCurve(curve)


def _stats(values: np.ndarray) -> dict[str, float | int]:
    return {'count': int(values.size), 'mean': float(values.mean()), 'median': float(np.median(values)),
            'p95': float(np.percentile(values, 95)), 'max': float(values.max())}


def _visual_panel(rgb: np.ndarray, title: str) -> Image.Image:
    pixels = np.uint8(np.rint(np.clip(rgb, 0, 1)*255))
    panel = Image.new('RGB', (pixels.shape[1], pixels.shape[0]+28), 'white')
    panel.paste(Image.fromarray(pixels), (0, 28))
    ImageDraw.Draw(panel).text((8, 7), title, fill='black')
    return panel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('output/lut-quality'))
    args = parser.parse_args(argv)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    profile_path = output/'analytic-rec709-gamma18.icc'
    create_gamma_fixture(profile_path)
    source = SpaceSpec('icc_rgb', profile_path)
    target = SpaceSpec('srgb')
    rng = np.random.default_rng(20261020)
    random = rng.random((20000, 3)).astype(np.float32)
    # Synthetic patches, not licensed ColorChecker data or measured camera references.
    patches = np.array([[.02,.02,.02], [.1,.1,.1], [.3,.3,.3], [.6,.6,.6], [.85,.85,.85], [1,1,1],
                        [.7,.1,.1], [.1,.7,.1], [.1,.1,.7], [.7,.7,.1], [.7,.1,.7], [.1,.7,.7],
                        [.8,.4,.2], [.6,.3,.15], [.3,.5,.7], [.2,.5,.25], [.5,.2,.6], [.9,.65,.15],
                        [.04,.1,.3], [.12,.04,.2], [.15,.3,.03], [.95,.7,.65], [.65,.8,.95], [.8,.95,.7]], np.float32)
    ramp = np.linspace(0, 1, 2049, dtype=np.float32)
    gradients = np.concatenate([np.column_stack((ramp, ramp, ramp))] +
                               [ramp[:, None]*np.eye(3, dtype=np.float32)[i] for i in range(3)])
    endpoints = np.array([[r,g,b] for r in (0,1) for g in (0,1) for b in (0,1)], np.float32)
    sets = {'random': random, 'synthetic_chart': patches, 'gradients': gradients, 'corners': endpoints}
    chart = np.repeat(np.repeat(patches.reshape(4, 6, 3), 72, axis=0), 96, axis=1)
    visual_ramp = np.linspace(0, 1, chart.shape[1], dtype=np.float32)
    bands = np.concatenate([np.repeat((visual_ramp[:,None]*color)[None], 24, axis=0)
                            for color in np.array([[1,1,1],[1,0,0],[0,1,0],[0,0,1]], np.float32)], axis=0)
    visual = np.concatenate((chart, bands), axis=0)
    report = {'schema_version': 1,
              'purpose': 'LUT interpolation accuracy against direct real LCMS float transform, NOT camera JPEG accuracy',
              'jpeg_accuracy': 'unverified; no real paired camera references supplied',
              'fixture': {'description': 'Analytic Rec.709 primaries D65 gamma=1.8 RGB ICC to encoded sRGB',
                          'profile_sha256': hashlib.sha256(profile_path.read_bytes()).hexdigest(),
                          'license': 'Generated analytic test fixture; no third-party camera profile or chart data'},
              'settings': {'intent': 'relative', 'black_point_compensation': False, 'gamut': 'clip',
                           'seed': 20261020, 'interpolation': 'tetrahedral', 'strength': 1.0},
              'software': {'python': platform.python_version(), 'numpy': np.__version__, 'lcms': probe_lcms()},
              'sample_counts': {k: len(v) for k,v in sets.items()}, 'sizes': {}}
    # Service records the complete request provenance; its apply delegates directly to LCMS float.
    transform = create_transform(TransformRequest(source, target, intent='relative', gamut='clip'))
    try:
        references = {name: transform.apply(samples) for name, samples in sets.items()}
        direct_visual = transform.apply(visual)
        panels = [_visual_panel(direct_visual, 'DIRECT LCMS: synthetic chart and gradients')]
        for size in (17, 33, 65):
            lut = generate_lut(transform, size=size, title=f'Analytic gamma1.8 to sRGB interpolation test {size}')
            exported = export_cube(lut, output/f'analytic-gamma18-{size}.cube', overwrite=True)
            restored = read_cube(exported.cube_path, input_space=source, output_space=target)
            metrics = {}
            for name, samples in sets.items():
                sampled = sample_lut(restored, samples)
                direct = references[name]
                metrics[name] = {'rgb_absolute_channel_error': _stats(np.abs(sampled-direct)),
                                 'rgb_max_channel_error_per_color': _stats(np.max(np.abs(sampled-direct), axis=1)),
                                 'delta_e00_d65': _stats(delta_e00(srgb_to_lab(sampled), srgb_to_lab(direct)))}
            all_sampled = sample_lut(restored, np.concatenate(list(sets.values())))
            all_direct = np.concatenate(list(references.values()))
            metrics['all'] = {'rgb_absolute_channel_error': _stats(np.abs(all_sampled-all_direct)),
                              'rgb_max_channel_error_per_color': _stats(np.max(np.abs(all_sampled-all_direct), axis=1)),
                              'delta_e00_d65': _stats(delta_e00(srgb_to_lab(all_sampled), srgb_to_lab(all_direct)))}
            report['sizes'][str(size)] = {'cube_sha256': exported.sha256, 'metrics': metrics}
            panels.append(_visual_panel(sample_lut(restored, visual), f'{size}^3 EXPORTED / RE-READ LUT'))
            stats = metrics['all']['delta_e00_d65']
            print(f'{size}: DeltaE00 mean={stats["mean"]:.8f}, median={stats["median"]:.8f}, p95={stats["p95"]:.8f}, max={stats["max"]:.8f}')
        image = Image.new('RGB', (panels[0].width*2, panels[0].height*2), 'white')
        for i, panel in enumerate(panels): image.paste(panel, ((i%2)*panel.width, (i//2)*panel.height))
        image.save(output/'direct-vs-lut-chart.png')
        report['visualization'] = 'direct-vs-lut-chart.png (8-bit visualization only; measurements use float32)'
        (output/'quality.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    finally:
        transform.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
