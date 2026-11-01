"""Validate the pinned LittleCMS2 x64 DLL via ctypes (float32 RGB identity).

Usage: <venv python> validate_lcms2.py <path-to-lcms2.dll>
Exit code 0 only if every check passes.
"""
import array
import ctypes
import hashlib
import json
import os
import platform
import random
import struct
import sys
from ctypes import c_int, c_uint32, c_void_p

TYPE_RGB_FLT = 0x44001C          # FLOAT_SH(1)|COLORSPACE_SH(PT_RGB)|CHANNELS_SH(3)|BYTES_SH(4)
TYPE_XYZ_FLT = 0x49001C          # FLOAT_SH(1)|COLORSPACE_SH(PT_XYZ=9)|CHANNELS_SH(3)|BYTES_SH(4)
INTENT_PERCEPTUAL = 0
INTENT_RELATIVE_COLORIMETRIC = 1
cmsFLAGS_NOOPTIMIZE = 0x0100
EXPECTED_VERSION = 2170          # LCMS_VERSION in include/lcms2.h for lcms2.17
MAX_IDENTITY_ERR = 1e-4          # float32 identity tolerance (8-bit step would be 3.92e-3)
N_PIXELS = 4096
SEED = 20261020


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    dll_path = os.path.abspath(sys.argv[1])
    result = {
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "pointer_bits": struct.calcsize("P") * 8,
        "machine": platform.machine(),
        "dll_path": dll_path,
        "dll_sha256": sha256(dll_path),
        "checks": {},
    }
    assert struct.calcsize("P") == 8, "Python must be 64-bit to load the x64 DLL"

    lcms = ctypes.CDLL(dll_path)  # x64 has a single calling convention; no stdcall issue
    lcms.cmsGetEncodedCMMversion.argtypes = []
    lcms.cmsGetEncodedCMMversion.restype = c_int
    lcms.cmsCreate_sRGBProfile.argtypes = []
    lcms.cmsCreate_sRGBProfile.restype = c_void_p
    lcms.cmsCreateTransform.argtypes = [c_void_p, c_uint32, c_void_p, c_uint32, c_uint32, c_uint32]
    lcms.cmsCreateTransform.restype = c_void_p
    lcms.cmsDoTransform.argtypes = [c_void_p, c_void_p, c_void_p, c_uint32]
    lcms.cmsDoTransform.restype = None
    lcms.cmsDeleteTransform.argtypes = [c_void_p]
    lcms.cmsDeleteTransform.restype = None
    lcms.cmsCreateXYZProfile.argtypes = []
    lcms.cmsCreateXYZProfile.restype = c_void_p
    lcms.cmsCloseProfile.argtypes = [c_void_p]
    lcms.cmsCloseProfile.restype = c_int

    version = lcms.cmsGetEncodedCMMversion()
    result["encoded_cmm_version"] = version
    result["checks"]["version_is_2170"] = version == EXPECTED_VERSION

    # Fractional float32 RGB samples in [0,1]; not multiples of 1/255 in general.
    rng = random.Random(SEED)
    vals = [rng.random() for _ in range(N_PIXELS * 3)]
    # Explicit awkward fractions: 1/3, 1/255 half-steps, tiny and near-1 values.
    vals[0:9] = [1.0 / 3.0, 0.5 / 255.0, 0.999999, 0.0, 1e-7, 0.25, 0.7071068, 0.123456789, 0.5]
    src = array.array("f", vals)            # float32 storage, exact float32 inputs
    in_buf = (ctypes.c_float * len(src)).from_buffer(src)
    out = (ctypes.c_float * len(src))()

    srgb_in = lcms.cmsCreate_sRGBProfile()
    srgb_out = lcms.cmsCreate_sRGBProfile()
    result["checks"]["srgb_profiles_created"] = bool(srgb_in) and bool(srgb_out)

    results = []
    for intent_name, intent in (("perceptual", INTENT_PERCEPTUAL),
                                ("relative_colorimetric", INTENT_RELATIVE_COLORIMETRIC)):
        for flag_name, flags in (("optimized", 0), ("no_optimize", cmsFLAGS_NOOPTIMIZE)):
            xf = lcms.cmsCreateTransform(srgb_in, TYPE_RGB_FLT, srgb_out, TYPE_RGB_FLT, intent, flags)
            if not xf:
                results.append({"intent": intent_name, "flags": flag_name, "transform": False})
                continue
            lcms.cmsDoTransform(xf, in_buf, out, N_PIXELS)
            lcms.cmsDeleteTransform(xf)
            got = list(out)
            maxabs = max(abs(g - s) for g, s in zip(got, src))
            # Quantization evidence: how many outputs are NOT on an 8-bit grid (k/255).
            off_grid = sum(1 for g in got if abs(g * 255.0 - round(g * 255.0)) > 1e-3)
            exact_8bit = sum(1 for g, s in zip(got, src) if g == round(s * 255.0) / 255.0)
            results.append({
                "intent": intent_name,
                "flags": flag_name,
                "transform": True,
                "maxabs_error": maxabs,
                "outputs_off_8bit_grid": off_grid,
                "outputs_total": len(got),
                "sample_in": [round(v, 9) for v in src[:9]],
                "sample_out": [round(v, 9) for v in got[:9]],
                "pass": maxabs < MAX_IDENTITY_ERR and off_grid > 0.99 * len(got),
            })
    # Non-identity control: sRGB white must map to the D50 PCS white (~0.9642, 1.0, 0.8249)
    # relative colorimetric; proves cmsDoTransform does real colour computation.
    xyz_prof = lcms.cmsCreateXYZProfile()
    white_in = (ctypes.c_float * 3)(1.0, 1.0, 1.0)
    white_out = (ctypes.c_float * 3)()
    xf = lcms.cmsCreateTransform(srgb_in, TYPE_RGB_FLT, xyz_prof, TYPE_XYZ_FLT, INTENT_RELATIVE_COLORIMETRIC, 0)
    xyz = [round(v, 6) for v in white_out]
    d50 = (0.9642, 1.0, 0.8249)
    wp_ok = False
    if xf:
        lcms.cmsDoTransform(xf, white_in, white_out, 1)
        lcms.cmsDeleteTransform(xf)
        xyz = [round(v, 6) for v in white_out]
        wp_ok = all(abs(a - b) < 2e-3 for a, b in zip(xyz, d50))
    result["non_identity_control"] = {"srgb_white_to_xyz_d50": xyz, "expected_approx": list(d50), "pass": wp_ok}
    result["checks"]["non_identity_white_to_d50"] = wp_ok
    lcms.cmsCloseProfile(xyz_prof)
    lcms.cmsCloseProfile(srgb_in)
    lcms.cmsCloseProfile(srgb_out)

    result["identity"] = results
    result["identity_tolerance"] = MAX_IDENTITY_ERR
    result["checks"]["identity_all_pass"] = all(r.get("pass") for r in results) and len(results) == 4
    result["pass"] = all(result["checks"].values())
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
