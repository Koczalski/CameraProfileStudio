"""Floating-point sRGB transfer functions and D65 color-difference metrics."""
from __future__ import annotations

import numpy as np
from .errors import ValidationError
from .types import RGB, Matrix


def _rgb(rgb: RGB) -> np.ndarray:
    try:
        result = np.asarray(rgb, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValidationError('RGB input must be numeric.', code='invalid_rgb') from exc
    if result.ndim < 1 or result.shape[-1] != 3 or not np.all(np.isfinite(result)):
        raise ValidationError('RGB input must be finite with final dimension 3.', code='invalid_rgb')
    return result


def srgb_decode(rgb: RGB) -> RGB:
    value = _rgb(rgb)
    output = value / 12.92
    nonlinear = value > 0.04045
    output[nonlinear] = ((value[nonlinear] + 0.055) / 1.055) ** 2.4
    return output.astype(np.float32)


def srgb_encode(rgb: RGB) -> RGB:
    value = _rgb(rgb)
    output = value * 12.92
    nonlinear = value > 0.0031308
    output[nonlinear] = 1.055 * value[nonlinear] ** (1 / 2.4) - 0.055
    return output.astype(np.float32)


def srgb_to_lab(rgb: RGB) -> Matrix:
    value = _rgb(rgb)
    if np.any(value < 0) or np.any(value > 1):
        raise ValidationError('Lab evaluation requires normalized sRGB.', code='rgb_out_of_range')
    # IEC sRGB/D65 matrix; normalize XYZ by the same matrix's reference white.
    matrix = np.array([[0.4124564, 0.3575761, 0.1804375],
                       [0.2126729, 0.7151522, 0.0721750],
                       [0.0193339, 0.1191920, 0.9503041]])
    xyz = srgb_decode(value).astype(np.float64) @ matrix.T
    relative = xyz / matrix.sum(axis=1)
    delta = 6 / 29
    f = np.where(relative > delta ** 3, np.cbrt(relative), relative / (3 * delta ** 2) + 4 / 29)
    return np.stack((116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])), axis=-1)


def delta_e00(first_lab: Matrix, second_lab: Matrix) -> Matrix:
    first, second = _rgb(first_lab), _rgb(second_lab)
    if first.shape != second.shape:
        raise ValidationError('Lab arrays must have identical shapes.', code='shape_mismatch')
    from colour.difference import delta_E_CIE2000
    return np.asarray(delta_E_CIE2000(first, second), dtype=np.float64)
