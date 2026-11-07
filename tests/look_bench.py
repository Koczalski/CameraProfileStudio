"""Benchmark helpers for Image Look: synthetic scenes, known looks, errors (no photos needed).

A *known look* is applied to scenes to make "reference photos".  The engine estimates a look from them
(against the user's own scenes, or against the built-in typical photo) and is applied to held-out scenes.
Error = CIEDE2000 between engine(H) and known_look(H); "do nothing" = CIEDE2000 between H and known_look(H).
With a reference set that carries NO look the engine must leave photos (almost) alone.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

from camera_profile_studio.color import delta_e00, srgb_to_lab
from camera_profile_studio.image_look import (
    ImageLookReference, ImageLookSettings, _TYPICAL, _sector_interp, analyze_image_look, apply_image_look,
    fit_image_look, oklab_to_srgb, srgb_to_oklab,
)

LOOKS = ('teal-orange', 'faded-warm-film', 'contrasty-vivid', 'cool-bleach')


def scene(seed: int, size: int = 192) -> np.ndarray:
    """A synthetic photo: smooth lightness field, 1-3 dominant hues, patchy chroma, some near-neutral areas.

    Scenes differ in brightness, colourfulness and dominant colours the way real photos do, so a
    statistic that merely follows scene content shows up as a (false) look.
    """
    rng = np.random.default_rng(seed)

    def field(sigma: float) -> np.ndarray:
        f = gaussian_filter(rng.standard_normal((size, size)), sigma)
        return (f - f.mean()) / f.std()

    mean, spread = rng.uniform(.14, .86), rng.uniform(.08, .30)
    lightness = np.clip(mean + spread * field(14) + .04 * field(2), .02, .98)
    count = int(rng.integers(2, 6))
    centres = rng.uniform(0, 2 * np.pi, count)
    pick = np.clip((np.tanh(field(22)) * .5 + .5) * count, 0, count - 1e-6).astype(int)
    hue = np.angle(np.exp(1j * (centres[pick] + .45 * field(8))))
    # Saturation level is a property of the photo; which hues can be that saturated is a property of the
    # colour gamut, so real photos share one chroma-by-hue shape (taken from the built-in typical photo).
    shape = np.asarray(_TYPICAL['sector_chroma'], dtype=np.float64)
    chroma = (rng.uniform(.005, .19) * (.82 + .18 * np.abs(field(10)))
              * _sector_interp(hue, shape / shape.mean()))
    chroma = np.where(field(18) > .9, chroma * .08, chroma)             # neutral materials
    lab = np.stack([lightness, chroma * np.cos(hue), chroma * np.sin(hue)], axis=-1)
    return np.clip(oklab_to_srgb(lab), 0, 1)


def look(name: str, rgb: np.ndarray) -> np.ndarray:
    """A known global look defined in Oklab: tone curve, lightness-dependent cast, chroma scale."""
    lab = srgb_to_oklab(rgb)
    l, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    if name == 'teal-orange':
        l2 = l + .35 * l * (1 - l) * (2 * l - 1)
        shadow, high = np.clip(1 - l / .5, 0, 1), np.clip((l - .5) / .5, 0, 1)
        a2, b2, scale = a + shadow * -.035 + high * .025, b + shadow * -.025 + high * .045, 1.25
    elif name == 'faded-warm-film':
        l2, a2, b2, scale = .07 + .88 * l, a, b + .02, .8
    elif name == 'contrasty-vivid':
        l2, a2, b2, scale = l + .7 * l * (1 - l) * (2 * l - 1), a, b, 1.45
    elif name == 'cool-bleach':
        l2, a2, b2, scale = l + .4 * l * (1 - l) * (2 * l - 1), a, b - .025, .6
    else:
        raise ValueError(name)
    return np.clip(oklab_to_srgb(np.stack([l2, a2 * scale, b2 * scale], axis=-1)), 0, 1)


def delta_e(first: np.ndarray, second: np.ndarray, step: int = 2) -> np.ndarray:
    """Per-pixel CIEDE2000 between two sRGB images (every ``step``-th pixel)."""
    x, y = first[::step, ::step].reshape(-1, 3), second[::step, ::step].reshape(-1, 3)
    return np.asarray(delta_e00(srgb_to_lab(x), srgb_to_lab(y)), dtype=np.float64)


def estimate(directory: Path, references: list[np.ndarray], bases: list[np.ndarray],
             settings: ImageLookSettings = ImageLookSettings()):
    """Fit a look from reference scenes (and base scenes) written as PNG files into ``directory``."""
    items = []
    for role, group in (('reference', references), ('base', bases)):
        for i, image in enumerate(group):
            path = directory / f'{role}{i}.png'
            Image.fromarray(np.round(np.clip(image, 0, 1) * 255).astype(np.uint8)).save(path)
            items.append(ImageLookReference(path=path, role=role))
    analysis = analyze_image_look(tuple(items), settings=settings)
    return fit_image_look(analysis, acknowledge_conflicts=True)


def apply(model, image: np.ndarray) -> np.ndarray:
    return np.asarray(apply_image_look(image, model), dtype=np.float64)
