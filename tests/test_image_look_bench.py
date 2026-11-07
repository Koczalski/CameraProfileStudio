"""Image Look accuracy on synthetic scenes with known looks (see look_bench.py).

* A reference set that carries NO look must leave photos (almost) alone, however few photos there are
  and whether or not the user's own photos are given.
* A known look must not be made worse than doing nothing (within 0.1, far below anything visible).
* With the ideal base (the reference photos' own unlooked scenes) the same holds.  An unpaired set cannot tell
  that the base IS the unlooked reference, so how much of the look comes back is recorded, not required;
  the strong, photo-specific transfer is the job of the one-to-one mode.

The thresholds are engineering targets, not psychophysical facts: median CIEDE2000 <= 1 is hard to see and
the 95th percentile <= 3 is still a mild change.
"""
from __future__ import annotations

import numpy as np
import pytest

import look_bench as bench

HELD_OUT = [bench.scene(900 + i) for i in range(6)]
NOT_WORSE = 0.1     # CIEDE2000: a rounding-level tolerance for "not made worse"


def _scenes(first_seed: int, n: int) -> list[np.ndarray]:
    return [bench.scene(first_seed + i) for i in range(n)]


@pytest.mark.parametrize('mode', ['with base', 'typical photo'])
@pytest.mark.parametrize('n', [1, 2, 4, 8])
def test_a_reference_without_a_look_leaves_photos_alone(tmp_path, n, mode):
    errors = []
    for draw in range(3):
        references = _scenes(100 + draw * 20, n)
        bases = _scenes(300 + draw * 20, n) if mode == 'with base' else []
        model = bench.estimate(tmp_path, references, bases)
        errors += [bench.delta_e(h, bench.apply(model, h)) for h in HELD_OUT]
    errors = np.concatenate(errors)
    assert np.median(errors) <= 1.0, f'median {np.median(errors):.2f}'
    assert np.percentile(errors, 95) <= 3.0, f'p95 {np.percentile(errors, 95):.2f}'


def test_the_same_photos_as_reference_and_base_give_no_look(tmp_path):
    scenes = _scenes(100, 4)
    model = bench.estimate(tmp_path, scenes, scenes)
    assert max(float(bench.delta_e(h, bench.apply(model, h)).max()) for h in HELD_OUT) < .05


@pytest.mark.parametrize('mode', ['with base', 'typical photo'])
@pytest.mark.parametrize('name', bench.LOOKS)
def test_a_known_look_is_not_made_worse(tmp_path, name, mode):
    references = [bench.look(name, s) for s in _scenes(100, 4)]
    bases = _scenes(300, 4) if mode == 'with base' else []
    model = bench.estimate(tmp_path, references, bases)
    nothing = np.mean([bench.delta_e(h, bench.look(name, h)).mean() for h in HELD_OUT])
    estimated = np.mean([bench.delta_e(bench.apply(model, h), bench.look(name, h)).mean() for h in HELD_OUT])
    assert estimated <= nothing + NOT_WORSE, f'{estimated:.2f} vs doing nothing {nothing:.2f}'


@pytest.mark.parametrize('name', bench.LOOKS)
def test_the_ideal_base_does_not_make_a_known_look_worse(tmp_path, name, record_property):
    scenes = _scenes(100, 4)
    model = bench.estimate(tmp_path, [bench.look(name, s) for s in scenes], scenes)
    nothing = np.mean([bench.delta_e(h, bench.look(name, h)).mean() for h in HELD_OUT])
    estimated = np.mean([bench.delta_e(bench.apply(model, h), bench.look(name, h)).mean() for h in HELD_OUT])
    record_property('recovered_percent', round(100 * (1 - estimated / nothing)))
    assert estimated <= nothing + NOT_WORSE, f'{estimated:.2f} vs doing nothing {nothing:.2f}'
