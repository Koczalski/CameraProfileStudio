"""Derive the natural between-photo spread of the Image Look parameters (image_look._NATURAL_SPREAD).

For each photograph the look parameters are fitted against the built-in typical photo, as if that single
photograph were the reference.  Per parameter group the squared length of those parameters is summarised over the
photographs: the mean, the 90th percentile (the value stored) and the maximum.  Only numbers are stored in the code;
the photographs stay where they are.

    .venv\\Scripts\\python.exe scripts\\derive_natural_spread.py FOLDER [FOLDER ...] [--per-folder 14] [--seed 7]

The tone group is measured with the overall lightness shift removed (scene exposure is not a look).
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from camera_profile_studio import image_look as il  # noqa: E402

GROUPS = {'tone': slice(0, 15), 'cast': slice(15, 25), 'band': slice(25, 30), 'sector': slice(30, 42)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('folders', nargs='+', type=Path)
    parser.add_argument('--per-folder', type=int, default=14)
    parser.add_argument('--seed', type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    photos: list[Path] = []
    for folder in args.folders:
        found = [p for p in folder.rglob('*') if p.suffix.lower() in ('.jpg', '.jpeg', '.png')]
        photos += rng.sample(found, min(args.per_folder, len(found)))
    typical, settings = il._typical(), il.ImageLookSettings(shrinkage=0)
    rows = []
    for path in photos:
        try:
            stats = il.analyze_image_look((il.ImageLookReference(path=path),), settings=settings).references[0].stats
        except Exception as exc:  # noqa: BLE001 - one unreadable file must not stop the survey
            print(f'skipped {path.name}: {type(exc).__name__}', file=sys.stderr)
            continue
        x, y, cast, gain, sector = il._fit(stats, typical, settings, 'base_images')
        tone = y - x
        rows.append(np.concatenate([tone - tone.mean(), cast.ravel(), np.log(gain), np.log(sector)]))
    table = np.array(rows)
    print(f'{len(table)} photographs from {len(args.folders)} folder(s)')
    print(f'{"group":8s} {"mean":>8s} {"q90 (stored)":>13s} {"max":>8s}')
    for name, part in GROUPS.items():
        energy = np.sum(table[:, part] ** 2, axis=1)
        print(f'{name:8s} {energy.mean():8.4f} {np.percentile(energy, 90):13.4f} {energy.max():8.4f}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
