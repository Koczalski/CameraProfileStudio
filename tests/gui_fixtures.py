"""Synthetic GUI-validation fixtures (not camera evidence).

* ``preview_chart.png`` - an sRGB test image (gradients + patches) used only
  as preview input.
* ``look_pair_source.png`` / ``look_pair_target.png`` - the target is the
  source passed through the *real* engine look transform of the supplied
  Nikon D70s DCP (LookTable only).  It is a synthetic, pixel-aligned same-
  scene pair for exercising the Match workflow; it says nothing about camera
  JPEG accuracy.
* ``look_patches.csv`` - patch table sampled from that pair, grouped into
  four spatial blocks.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "input" / "ccd_profiles"
FIXTURES = ROOT / "output" / "gui-validation" / "fixtures"


def chart(width: int = 960, height: int = 640) -> np.ndarray:
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    img = np.zeros((height, width, 3), np.float32)
    img[..., 0] = x / (width - 1)
    img[..., 1] = y / (height - 1)
    img[..., 2] = 1.0 - x / (width - 1)
    # grey ramp band
    band = slice(int(height * 0.42), int(height * 0.58))
    img[band, :, :] = (x[band, :, None] / (width - 1))
    # patch grid
    rng = np.random.default_rng(7)
    cols, rows = 8, 3
    pw, ph = width // (cols + 2), int(height * 0.12)
    for r in range(rows):
        for c in range(cols):
            y0 = int(height * 0.64) + r * (ph + 4)
            x0 = (c + 1) * pw
            img[y0:y0 + ph, x0:x0 + pw - 6] = rng.uniform(0.08, 0.92, 3)
    return np.clip(img, 0, 1)


def save_png(array: np.ndarray, path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((np.clip(array, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB").save(path)


def build() -> dict[str, Path]:
    from camera_profile_studio import dcp, look
    from camera_profile_studio.types import DcpSettings

    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = {}
    src = chart()
    out["chart"] = FIXTURES / "preview_chart.png"
    save_png(src, out["chart"])
    # 8-bit round trip so the pair matches what load_image returns
    src8 = (np.clip(src, 0, 1) * 255 + 0.5).astype(np.uint8).astype(np.float32) / 255.0
    profile = dcp.read_dcp(PROFILES / "Nikon_D70s.dcp")
    tgt = look.apply_look(src8.astype(np.float32), profile, mode="look_only",
                          settings=DcpSettings(include_hue_sat_map=False))
    out["pair_source"] = FIXTURES / "look_pair_source.png"
    out["pair_target"] = FIXTURES / "look_pair_target.png"
    save_png(src8, out["pair_source"])
    save_png(np.asarray(tgt), out["pair_target"])
    tgt8 = (np.clip(np.asarray(tgt), 0, 1) * 255 + 0.5).astype(np.uint8).astype(np.float32) / 255.0
    rng = np.random.default_rng(11)
    h, w, _ = src8.shape
    out["csv"] = FIXTURES / "look_patches.csv"
    with open(out["csv"], "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["source_r", "source_g", "source_b", "target_r", "target_g", "target_b", "group"])
        for _ in range(240):
            yy, xx = int(rng.integers(0, h)), int(rng.integers(0, w))
            group = f"block{(yy * 2) // h}{(xx * 2) // w}"
            writer.writerow([f"{v:.6f}" for v in src8[yy, xx]] + [f"{v:.6f}" for v in tgt8[yy, xx]] + [group])
    # a long path for elision checks
    long_dir = FIXTURES / ("very_long_folder_name_for_path_elision_checks_" * 2).rstrip("_")
    long_dir.mkdir(parents=True, exist_ok=True)
    out["long_profile"] = long_dir / "Sony_DSC-F828_with_an_exceptionally_long_profile_file_name_copy.icc"
    out["long_profile"].write_bytes((PROFILES / "Sony_DSC-F828.icc").read_bytes())
    # an invalid profile to show error states
    out["invalid"] = FIXTURES / "broken_profile.icc"
    out["invalid"].write_bytes(b"not an icc profile" * 8)
    return out


if __name__ == "__main__":  # pragma: no cover
    for k, v in build().items():
        print(k, v)
