"""Acquire one pinned CC0 RAW and validate the shared decoder, never OEM JPEG accuracy.

Run with .venv/Scripts/python.exe scripts/validate_raw_input.py --download once.
Later runs are offline by default. No packages, profiles or executable code are fetched.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import traceback
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "input/raw_validation"
OUTPUT = ROOT / "output/raw-validation"
RAW_PATH = INPUT / "raw_pixls_us_1117_Nikon_D70s.NEF"
PROVENANCE = INPUT / "raw_pixls_us_1117_provenance.json"
LISTING_URL = "https://raw.pixls.us/json/getrepository.php"
RAW_URL = ("https://raw.pixls.us/getfile.php/1117/nice/"
           "Nikon - D70s - 12bit 12bit compressed (Lossy (type 1)) (3:2).NEF")
EXPECTED_SHA256 = "6c46f9854033e7623ebbb5de8c951df6762ca44f05d45035ccc5cb7e9f4e5692"
LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
MAX_DIMENSION = 1200


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")


def fetch(url: str, maximum: int) -> tuple[bytes, dict]:
    request_url = quote(url, safe=":/?=&%")
    with urlopen(request_url, timeout=60) as response:
        final_url = response.url
        if urlsplit(final_url).hostname != "raw.pixls.us" or urlsplit(final_url).scheme != "https":
            raise ValueError("Unexpected download destination: " + final_url)
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Response exceeds modest sample size limit")
        return data, {"url": url, "request_url": request_url, "final_url": final_url,
                      "retrieved_at_utc": now(), "http_status": response.status,
                      "content_type": response.headers.get("Content-Type"),
                      "bytes": len(data), "sha256": digest(data)}


def acquire() -> None:
    listing_bytes, listing_retrieval = fetch(LISTING_URL, 4 * 1024 * 1024)
    listing = json.loads(listing_bytes)
    rows = [row for row in listing["data"] if "getfile.php/1117/nice/" in str(row)]
    if len(rows) != 1:
        raise ValueError("Expected exactly one public listing entry for id 1117")
    row = rows[0]
    if (row[:2] != ["Nikon", "D70s"] or LICENSE_URL not in row[5]
            or "Creative Commons 0 - Public Domain" not in row[5]
            or RAW_URL not in row[7] or EXPECTED_SHA256 not in row[7]):
        raise ValueError("Public listing does not match pinned identity/hash/CC0 license")
    INPUT.mkdir(parents=True, exist_ok=True)
    if RAW_PATH.exists():
        data = RAW_PATH.read_bytes()
        retrieval = {"url": RAW_URL, "reused_local_file": True, "verified_at_utc": now()}
    else:
        data, retrieval = fetch(RAW_URL, 8 * 1024 * 1024)
    if digest(data) != EXPECTED_SHA256:
        raise ValueError("RAW SHA256 mismatch; refusing adoption")
    if not RAW_PATH.exists():
        RAW_PATH.write_bytes(data)
    save_json(PROVENANCE, {
        "schema_version": 1, "repository": "raw.pixls.us", "entry_id": 1117,
        "camera_make": "Nikon", "camera_model": "D70s", "source_url": RAW_URL,
        "listing_url": LISTING_URL, "listing_date": row[6],
        "license": "CC0-1.0", "license_url": LICENSE_URL,
        "license_evidence": "Live public repository listing entry; not inferred from file extension",
        "listing_entry": row, "listing_retrieval": listing_retrieval,
        "raw_retrieval": retrieval, "file": RAW_PATH.relative_to(ROOT).as_posix(),
        "bytes": len(data), "sha256": digest(data), "expected_sha256": EXPECTED_SHA256,
        "purpose": "Real RAW decoder acceptance only; no OEM JPEG/color accuracy claim",
    })


def validate(report: dict) -> None:
    import numpy as np
    import cv2
    import rawpy
    from PIL import Image, PngImagePlugin
    from camera_profile_studio.images import load_image
    from camera_profile_studio.types import RawRecipe

    report["runtime"] = {"python": sys.version, "numpy": np.__version__,
                         "rawpy": rawpy.__version__, "libraw": list(rawpy.libraw_version),
                         "opencv": cv2.__version__}
    recipe = RawRecipe()
    report["recipe"] = asdict(recipe)
    try:
        image = load_image(RAW_PATH, max_dimension=MAX_DIMENSION, raw_recipe=recipe)
    except Exception as exc:
        report["checks"]["loader_max_dimension_1200_succeeds"] = False
        report["loader_error"] = {"type": type(exc).__name__, "message": str(exc),
                                  "code": getattr(exc, "code", ""), "traceback": traceback.format_exc()}
        if getattr(exc, "code", "") == "raw_decoder_missing":
            raise
        report["status"] = "FAILED"
        report["blockers"].append("Shared loader at max_dimension=1200 failed: " + str(exc))
        # A native-size diagnostic must never be mistaken for passing the requested size.
        image = load_image(RAW_PATH, max_dimension=None, raw_recipe=recipe)
        report["native_size_diagnostic_only"] = True
    else:
        report["checks"]["loader_max_dimension_1200_succeeds"] = True
    rgb = image.rgb
    metadata = dict(image.metadata)
    report["loader"] = {"shape": list(rgb.shape), "dtype": str(rgb.dtype),
                        "minimum": float(np.min(rgb)), "maximum": float(np.max(rgb)),
                        "metadata": metadata, "warnings": list(image.warnings),
                        "source_path": str(image.source_path)}
    checks = report["checks"]
    checks.update({
        "rgb_shape": rgb.ndim == 3 and rgb.shape[2] == 3 and min(rgb.shape[:2]) > 0,
        "maximum_dimension_1200": max(rgb.shape[:2]) == MAX_DIMENSION,
        "float32": rgb.dtype == np.float32,
        "all_finite": bool(np.isfinite(rgb).all()),
        "unit_range": bool(np.all((rgb >= 0) & (rgb <= 1))),
        "contiguous_readonly": bool(rgb.flags.c_contiguous and not rgb.flags.writeable),
        "source_path_recorded": image.source_path == RAW_PATH,
        "metadata_unique_keys": len(metadata) == len(image.metadata),
        "recipe_recorded": json.loads(metadata.get("raw_recipe", "null")) == asdict(recipe),
        "source_16_bit_recorded": metadata.get("source_bit_depth") == "16",
        "encoded_srgb_recorded": metadata.get("color_space") == "srgb",
        "versions_recorded": metadata.get("rawpy_version") == rawpy.__version__ and
                             metadata.get("libraw_version") == str(rawpy.libraw_version),
        "camera_metadata_recorded": all(key in metadata for key in
            ("camera_whitebalance", "raw_black_level_per_channel", "raw_white_level", "raw_sizes")),
        "orientation_recorded": bool(metadata.get("orientation")),
        "resize_recorded": json.loads(metadata.get("resize", "{}" )).get("max_dimension") == MAX_DIMENSION,
    })

    # Build the documented recipe independently: do not reuse loader options or pixels.
    options = dict(demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                   output_color=rawpy.ColorSpace.sRGB, output_bps=16, gamma=(2.4, 12.92),
                   no_auto_bright=True, no_auto_scale=False, use_auto_wb=False,
                   use_camera_wb=True, user_wb=None, exp_shift=1.0,
                   exp_preserve_highlights=0.0, bright=1.0, half_size=False,
                   four_color_rgb=False, highlight_mode=rawpy.HighlightMode.Clip,
                   median_filter_passes=0, fbdd_noise_reduction=rawpy.FBDDNoiseReductionMode.Off)
    expected_options = json.loads(json.dumps(options, default=str))
    actual_options = json.loads(metadata.get("raw_effective_options", "{}"))
    checks["effective_recipe_options_recorded"] = all(
        actual_options.get(key) == value for key, value in expected_options.items())
    report["independent_expected_options"] = expected_options
    with rawpy.imread(str(RAW_PATH)) as raw:
        wb = list(raw.camera_whitebalance)
        decoded = raw.postprocess(**options)
    checks["camera_wb_recorded_matches_raw"] = json.loads(metadata.get("camera_whitebalance", "null")) == wb
    checks["rawpy_direct_uint16_rgb"] = decoded.dtype == np.uint16 and decoded.ndim == 3 and decoded.shape[2] == 3
    expected = decoded.astype(np.float32) / np.float32(65535)
    scale = MAX_DIMENSION / max(expected.shape[:2])
    target = (max(1, round(expected.shape[1] * scale)), max(1, round(expected.shape[0] * scale)))
    resized = cv2.resize(expected, target, interpolation=cv2.INTER_AREA)
    report["direct_resize_diagnostic"] = {
        "shape": list(resized.shape), "dtype": str(resized.dtype),
        "minimum": float(np.min(resized)), "maximum": float(np.max(resized)),
        "channel_values_above_one": int(np.count_nonzero(resized > 1)),
        "channel_values_below_zero": int(np.count_nonzero(resized < 0)),
        "maximum_yxc": [int(v) for v in np.unravel_index(resized.argmax(), resized.shape)],
    }
    if report.get("native_size_diagnostic_only"):
        direct_path = OUTPUT / "nikon_d70s_direct_rawpy_decoder_NOT_OEM.png"
        png_metadata = PngImagePlugin.PngInfo()
        png_metadata.add_text("Description", "Direct rawpy decoder diagnostic, NOT shared-loader acceptance or OEM camera JPEG")
        png_metadata.add_text("SourceSHA256", EXPECTED_SHA256)
        png_metadata.add_text("SourceLicense", "CC0-1.0 " + LICENSE_URL)
        Image.fromarray(np.rint(np.clip(resized, 0, 1) * 255).astype(np.uint8)).save(direct_path, pnginfo=png_metadata)
        report["direct_diagnostic_png"] = {
            "path": direct_path.relative_to(ROOT).as_posix(), "sha256": digest(direct_path.read_bytes()),
            "encoding": "8-bit sRGB; direct rawpy resize clipped only for display",
            "purpose": "Decoder diagnostic, NOT a successful shared-loader result or OEM photo",
        }
    else:
        expected = resized
    checks["direct_shape_matches"] = expected.shape == rgb.shape
    if expected.shape != rgb.shape:
        raise ValueError("Shared loader geometry differs from direct RAW recipe")
    error = np.abs(expected.astype(np.float64) - rgb.astype(np.float64))
    tolerance = 2e-7
    checks["direct_recipe_pixels_match"] = bool(np.max(error) <= tolerance)
    positions = [(y, x) for y in np.linspace(0, rgb.shape[0] - 1, 5, dtype=int)
                 for x in np.linspace(0, rgb.shape[1] - 1, 7, dtype=int)]
    report["direct_comparison"] = {
        "method": "Separate rawpy decode with explicit contractual defaults; uint16/65535, then OpenCV INTER_AREA resize unless native-size diagnostic",
        "comparison_scope": "native-size diagnostic only" if report.get("native_size_diagnostic_only") else "requested 1200px shared-loader output",
        "limitation": "Independent invocation/options, not an independent RAW engine; shares LibRaw and resize library",
        "native_shape": list(decoded.shape), "native_dtype": str(decoded.dtype),
        "compared_channel_values": int(rgb.size), "absolute_tolerance": tolerance,
        "maximum_absolute_error": float(np.max(error)), "mean_absolute_error": float(np.mean(error)),
        "sample_pixels": [{"yx": [int(y), int(x)], "loader_rgb": rgb[y, x].tolist(),
                           "direct_rgb": expected[y, x].tolist()} for y, x in positions],
    }
    if not all(checks.values()):
        report["status"] = "FAILED"
        report["blockers"].append("Acceptance checks failed: " + ", ".join(k for k, v in checks.items() if not v))
        return
    png_path = OUTPUT / "nikon_d70s_decoder_result_NOT_OEM.png"
    png_metadata = PngImagePlugin.PngInfo()
    png_metadata.add_text("Description", "LibRaw decoder result; NOT OEM camera JPEG or verified camera color reproduction")
    png_metadata.add_text("SourceSHA256", EXPECTED_SHA256)
    png_metadata.add_text("SourceLicense", "CC0-1.0 " + LICENSE_URL)
    Image.fromarray(np.rint(rgb * 255).astype(np.uint8)).save(png_path, pnginfo=png_metadata)
    report["display_png"] = {"path": png_path.relative_to(ROOT).as_posix(),
                             "sha256": digest(png_path.read_bytes()),
                             "encoding": "8-bit encoded sRGB display derivative; acceptance checks use float32, not PNG",
                             "purpose": "Decoder result, NOT OEM photo/JPEG reference"}
    report["status"] = "COMPLETE"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Fetch live CC0 listing and the single pinned RAW if absent")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "created_at_utc": now(), "status": "PARTIAL",
              "purpose": "Real RAW shared image loader acceptance, not camera JPEG accuracy",
              "oem_jpeg_accuracy": "unverified; no paired OEM JPEG or reference patches",
              "max_dimension": MAX_DIMENSION, "checks": {}, "blockers": [],
              "implementation_sha256": {p.relative_to(ROOT).as_posix(): digest(p.read_bytes())
                  for p in (Path(__file__).resolve(), ROOT / "src/camera_profile_studio/images.py",
                            ROOT / "src/camera_profile_studio/types.py") if p.exists()}}
    sys.path.insert(0, str(ROOT / "src"))
    sys.dont_write_bytecode = True
    try:
        if args.download:
            acquire()
        if not RAW_PATH.exists() or not PROVENANCE.exists():
            report["blockers"].append("Pinned sample/provenance absent: run with --download")
        else:
            provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
            report["provenance"] = provenance
            checks = report["checks"]
            checks["raw_sha256"] = digest(RAW_PATH.read_bytes()) == EXPECTED_SHA256
            checks["cc0_listing_recorded"] = (provenance.get("license") == "CC0-1.0" and
                provenance.get("license_url") == LICENSE_URL and
                LICENSE_URL in provenance.get("listing_entry", [None] * 6)[5])
            if not all(checks.values()):
                raise ValueError("Pinned sample hash or license provenance check failed")
            validate(report)
    except (ImportError, NotImplementedError) as exc:
        report["blockers"].append(f"Loader/runtime unavailable; sample ready: {type(exc).__name__}: {exc}")
    except Exception as exc:
        report["status"] = "PARTIAL" if getattr(exc, "code", "") == "raw_decoder_missing" else "FAILED"
        report["blockers"].append(f"{type(exc).__name__}: {exc}")
        report["exception"] = traceback.format_exc()
    report["finished_at_utc"] = now()
    save_json(OUTPUT / "validation.json", report)
    lines = ["RAW input validation: " + report["status"], "UTC: " + report["finished_at_utc"],
             "Scope: real RAW decoding only; OEM JPEG accuracy remains unverified."]
    lines += [("PASS " if passed else "FAIL ") + name for name, passed in report["checks"].items()]
    lines += ["BLOCKER " + blocker for blocker in report["blockers"]]
    log = "\n".join(lines) + "\n"
    (OUTPUT / "validation.log").write_text(log, encoding="utf-8")
    print(log, end="")
    return 0 if report["status"] == "COMPLETE" else 2 if report["status"] == "PARTIAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
