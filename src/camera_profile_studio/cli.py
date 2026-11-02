"""Command line interface for the same headless jobs used by the desktop UI."""
from __future__ import annotations

import argparse
from dataclasses import fields, is_dataclass, replace
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

import numpy as np

from .errors import (CancelledError, DependencyError, ExportError, ImageError, StudioError,
                     ValidationError)
from .service import _module, convert_profile, inspect_profile
from .types import DcpSettings, MatchSettings, SamplePairs, SpaceSpec, TransformRequest


def _jsonable(value):
    if is_dataclass(value):
        return {field.name: dict(getattr(value, field.name)) if field.name in ('metadata', 'provenance')
                else _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _print_json(value, *, stream=None) -> None:
    print(json.dumps(_jsonable(value), ensure_ascii=True, allow_nan=False, sort_keys=True), file=stream or sys.stdout)


def _reject_constant(value):
    raise ValueError(f'Nonfinite JSON constant {value} is not allowed.')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def _read_json(path: Path) -> dict:
    try:
        with path.open('r', encoding='utf-8-sig') as stream:
            text = stream.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            raise ValueError('Settings exceed the 1 MiB limit.')
        result = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        if not isinstance(result, dict):
            raise ValueError('Settings must be a JSON object.')
        schema = result.pop('schema_version', 1)
        if type(schema) is not int or schema != 1:
            raise ValueError('Only schema_version 1 is supported.')
        return result
    except OSError as exc:
        raise StudioError(f'Cannot read settings: {path}', code='settings_io') from exc
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(f'Invalid settings JSON: {exc}', code='invalid_json') from exc
    except RecursionError as exc:
        raise ValidationError('Invalid settings JSON: nesting is too deep.', code='invalid_json') from exc


def _record(cls, payload):
    if not isinstance(payload, dict):
        raise ValidationError(f'{cls.__name__} must be an object.', code='invalid_request')
    unknown = set(payload) - {field.name for field in fields(cls)}
    if unknown:
        raise ValidationError(f'Unknown {cls.__name__} fields: {", ".join(sorted(unknown))}', code='unknown_fields')
    try:
        return cls(**payload)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f'Invalid {cls.__name__}: {exc}', code='invalid_request') from exc


def _image_look_settings(engine, payload):
    """Build ImageLookSettings; unknown keys (for example the removed neutral_protection) are image_look_invalid."""
    if not isinstance(payload, dict):
        raise ValidationError('Image-look settings must be an object.', code='image_look_invalid')
    unknown = set(payload) - {field.name for field in fields(engine.ImageLookSettings)}
    if unknown:
        hint = ' (neutral_protection was removed in image-look-v2)' if 'neutral_protection' in unknown else ''
        raise ValidationError(f'Unknown image-look settings: {", ".join(sorted(unknown))}{hint}',
                              code='image_look_invalid')
    try:
        return engine.ImageLookSettings(**payload)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f'Invalid image-look settings: {exc}', code='image_look_invalid') from exc


def _request(payload: dict) -> TransformRequest:
    payload = payload.copy()
    for name in ('input_space', 'output_space'):
        if name not in payload:
            raise ValidationError(f'Missing {name}.', code='invalid_request')
        payload[name] = _record(SpaceSpec, payload[name])
    if payload.get('dcp') is not None:
        payload['dcp'] = _record(DcpSettings, payload['dcp'])
    return _record(TransformRequest, payload)


def _samples(path: Path) -> SamplePairs:
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist()) > 256 * 1024 * 1024:
                raise ValueError('Sample archive exceeds the 256 MiB uncompressed limit.')
        with np.load(path, allow_pickle=False) as data:
            required = {'source_rgb', 'target_rgb', 'groups', 'evidence'}
            if set(data.files) != required:
                raise ValueError('NPZ must contain exactly source_rgb, target_rgb, groups and evidence.')
            groups, evidence = data['groups'], data['evidence']
            if groups.ndim != 1 or groups.dtype.kind != 'U' or evidence.ndim != 0 or evidence.dtype.kind != 'U':
                raise ValueError('Groups must be a Unicode vector; evidence must be a Unicode scalar.')
            return SamplePairs(data['source_rgb'], data['target_rgb'], tuple(groups.tolist()), evidence.item())
    except OSError as exc:
        raise StudioError(f'Cannot read sample archive: {path}', code='samples_io') from exc
    except (ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise ValidationError(f'Invalid sample archive: {exc}', code='invalid_samples') from exc


def _check_lut_outputs(args) -> tuple[str, set]:
    """Shared output-path checks of the LUT-writing commands; returns (export format, claimed paths)."""
    output_paths = {args.output.resolve(), args.output.with_suffix('.json').resolve()}
    if args.output.is_dir():
        raise ExportError('The output path is a directory; give a file name.', code='invalid_export_path')
    if not args.overwrite and (args.output.exists() or args.output.with_suffix('.json').exists()):
        raise ExportError('Output LUT or metadata already exists; use --overwrite explicitly.', code='output_exists')
    export_format = args.format.replace('-', '_')
    expected_suffix = '.cube' if export_format == 'cube' else '.png'
    if args.output.suffix.lower() != expected_suffix:
        raise ExportError(f'Output filename must end in {expected_suffix} for --format {args.format}.',
                          code='invalid_export_path')
    return export_format, output_paths


def _lut_summary(loaded) -> dict:
    return {'path': str(loaded.source_path), 'size': loaded.size, 'title': loaded.title,
            'source_format': loaded.source_format, 'warnings': list(loaded.warnings)}


def _write_json_report(path: Path, payload: dict, *, overwrite: bool) -> None:
    if path.is_dir():
        raise ExportError('The report path is a directory; give a file name.', code='invalid_export_path')
    if path.exists() and not overwrite:
        raise ExportError('Report file already exists; use --overwrite explicitly.', code='output_exists')
    text = json.dumps(_jsonable(payload), ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2) + '\n'
    temporary = None
    try:
        handle, name = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.json.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ExportError(f'Cannot write report: {exc}', code=_module('lut')._export_code(exc, path),
                          details=(('path', str(path)),)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _run_diagnose(args) -> int:
    health = _module('lut_health')
    loaded = health.load_lut_file(args.lut)
    report = health.diagnose_lut(loaded)
    header = {'lut': _lut_summary(loaded)}
    if args.json is not None:
        if args.json.resolve() == args.lut.resolve():
            raise ValidationError('The report must not overwrite the LUT file.', code='source_output_collision')
        _write_json_report(args.json, {**header, **health.report_to_dict(report, include_arrays=True)},
                           overwrite=args.overwrite)
        header['report_file'] = str(args.json)
    _print_json({**header, **health.report_to_dict(report)})
    return 0


def _run_repair(args) -> int:
    health = _module('lut_health')
    lutmod = _module('lut')
    export_format, output_paths = _check_lut_outputs(args)
    if args.lut.resolve() in output_paths:
        raise ValidationError('Output artifacts must not overwrite the input LUT.', code='source_output_collision')
    settings = health.RepairSettings(smooth=args.smooth, fix_jumps=args.fix_jumps, round_clip=args.round_clip,
                                     lock_gray=args.lock_gray, iterations=args.iterations)
    loaded = health.load_lut_file(args.lut)
    result = health.repair_lut(loaded, settings)
    record = dict(result.after.provenance)['repair']
    lut = health.to_lut3d(result.lut, args.size)
    lut = replace(lut, provenance=lut.provenance + (('repair', record),))
    exported = health.diagnose_lut(lut)
    extra = {'health': health.health_summary(exported), 'repair': json.loads(record)}
    export = lutmod.export_lut(lut, args.output, format=export_format, overwrite=args.overwrite, extra_metadata=extra)
    _print_json({'schema_version': 1, 'export': export, 'lut': _lut_summary(loaded), 'repair': json.loads(record),
                 'before': health.health_summary(result.before), 'after': health.health_summary(result.after),
                 'exported': health.health_summary(exported)})
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValidationError(message, code='cli_usage')


def _parser():
    parser = _Parser(prog='camera-profile-studio')
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect', help='Inspect ICC/ICM/DCP metadata and actual capabilities.')
    inspect.add_argument('profile', type=Path)
    inspect.add_argument('--json', action='store_true')
    convert = commands.add_parser('convert', help='Generate a validated LUT from explicit color-domain settings.')
    convert.add_argument('--request', type=Path, required=True)
    match = commands.add_parser('match', help='Fit paired sRGB samples with grouped holdout evaluation.')
    match.add_argument('--samples', type=Path, required=True)
    match.add_argument('--settings', type=Path)
    image_look = commands.add_parser('image-look', help='Estimate one approximate global sRGB look from finished reference photos '
                                   '(optionally relative to your own base photos, role "base").')
    image_look.add_argument('--request', type=Path, required=True)
    for command in (convert, match, image_look):
        command.add_argument('--output', type=Path, required=True)
        command.add_argument('--size', type=int, choices=(17, 33, 65), default=33)
        command.add_argument('--strength', type=float, default=1.0)
        command.add_argument('--overwrite', action='store_true')
        command.add_argument('--format', choices=('cube', 'hald-png', 'strip-png', 'strip-png-flipped'), default='cube',
                             help='LUT file format: .cube (default), Hald CLUT PNG (16-bit) or strip PNG (8-bit).')
    diagnose = commands.add_parser('diagnose', help='Check a 3D LUT file (.cube, .3dl, Hald/strip PNG or TIFF) for '
                                   'tone reversals, tints, clipping, jumps, hue folds, banding risk and flat areas.')
    diagnose.add_argument('--lut', type=Path, required=True)
    diagnose.add_argument('--json', type=Path, metavar='OUT',
                          help='Also write the full report (node locations, grey axis, hue samples) to OUT.')
    diagnose.add_argument('--overwrite', action='store_true', help='Replace an existing --json file.')
    repair = commands.add_parser('repair', help='Smooth / repair a LUT on request and export the result (never automatic).')
    repair.add_argument('--lut', type=Path, required=True)
    repair.add_argument('--output', type=Path, required=True)
    repair.add_argument('--smooth', type=float, default=0.0, help='Global smoothing strength 0..1 (default 0).')
    repair.add_argument('--fix-jumps', action='store_true', help='Smooth locally around jumps and hue folds.')
    repair.add_argument('--round-clip', action='store_true', help='Round the border of clipped plateaus.')
    repair.add_argument('--lock-gray', action='store_true', help='Make grey inputs neutral at their own lightness.')
    repair.add_argument('--iterations', type=int, default=8, help='Smoothing iterations 1..64 (default 8).')
    repair.add_argument('--size', type=int, choices=(17, 33, 65), default=None,
                        help='Export grid size (default: the nearest of 17/33/65).')
    repair.add_argument('--overwrite', action='store_true')
    repair.add_argument('--format', choices=('cube', 'hald-png', 'strip-png', 'strip-png-flipped'), default='cube',
                        help='LUT file format: .cube (default), Hald CLUT PNG (16-bit) or strip PNG (8-bit).')
    commands.add_parser('gui', help='Launch the optional Qt desktop application.')
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == 'inspect':
            info = inspect_profile(args.profile)
            if args.json:
                _print_json({'schema_version': 1, **_jsonable(info)})
            else:
                print(f'{info.description} [{info.format.upper()} {info.version}]')
                print(f'{info.color_space} / {info.pcs}; class {info.device_class}; SHA256 {info.sha256}')
                for cap in info.capabilities:
                    print(f'{cap.feature}: {cap.status}' + (f' ({"; ".join(cap.reasons)})' if cap.reasons else ''))
                for warning in info.warnings:
                    print(f'Warning: {warning}')
            return 0
        if args.command == 'gui':
            return int(_module('gui.app').main())
        if args.command == 'diagnose':
            return _run_diagnose(args)
        if args.command == 'repair':
            return _run_repair(args)
        input_paths = [args.request] if args.command in ('convert', 'image-look') else [args.samples, args.settings]
        output_paths = {args.output.resolve(), args.output.with_suffix('.json').resolve()}
        if any(path is not None and path.resolve() in output_paths for path in input_paths):
            raise ValidationError('Output artifacts must not overwrite input settings or samples.', code='source_output_collision')
        export_format, _ = _check_lut_outputs(args)
        report = None
        image_look_report = None
        if args.command == 'convert':
            lut = convert_profile(_request(_read_json(args.request)), size=args.size, strength=args.strength)
        elif args.command == 'image-look':
            engine = _module('image_look')
            payload = _read_json(args.request)
            if set(payload) - {'references', 'settings'}:
                raise ValidationError('Unknown image-look request fields.', code='unknown_fields')
            references = payload.get('references')
            if not isinstance(references, list) or not 1 <= len(references) <= 128:
                raise ValidationError('Select between 1 and 64 reference images (plus at most 64 base images).',
                                      code='image_look_invalid')
            records = []
            for value in references:
                # 'role' is optional ('reference' by default); the engine rejects any other value.
                reference = _record(engine.ImageLookReference, value)
                path = reference.path if reference.path.is_absolute() else args.request.resolve().parent / reference.path
                if path.resolve() in output_paths:
                    raise ValidationError('Output artifacts must not overwrite references.', code='source_output_collision')
                records.append(engine.ImageLookReference(path, reference.weight, reference.roi, reference.role))
            if not 1 <= sum(1 for record in records if record.role == 'reference') <= 64:
                raise ValidationError('Select between 1 and 64 reference images (role "reference").',
                                      code='image_look_invalid')
            if sum(1 for record in records if record.role == 'base') > 64:
                raise ValidationError('Select at most 64 base images (role "base").', code='image_look_invalid')
            settings = _image_look_settings(engine, payload.get('settings', {}))
            analysis = engine.analyze_image_look(tuple(records), settings=settings)
            model = engine.fit_image_look(analysis)
            transform = engine.create_image_look_transform(model)
            try:
                lut = _module('lut').generate_lut(transform, size=args.size, strength=args.strength,
                                                title='Image Look Creator - approximate unverified')
            finally:
                transform.close()
            image_look_report = {'status': 'approximate_unverified', 'warnings': model.warnings,
                                 'conflicts': analysis.conflicts, 'baseline': model.baseline,
                                 'base_count': analysis.base_count}
        else:
            settings = _record(MatchSettings, _read_json(args.settings)) if args.settings else MatchSettings()
            matching = _module('matching')
            model = matching.fit_match(_samples(args.samples), settings=settings)
            transform = matching.create_match_transform(model)
            try:
                lut = _module('lut').generate_lut(transform, size=args.size, strength=args.strength)
            finally:
                transform.close()
            report = model.report
        result = _module('lut').export_lut(lut, args.output, format=export_format, overwrite=args.overwrite)
        output = {'schema_version': 1, 'export': result}
        if report is not None:
            output['holdout_report'] = report
        if image_look_report is not None:
            output['image_look_report'] = image_look_report
        _print_json(output)
        return 0
    except (KeyboardInterrupt, CancelledError):
        _print_json({'code': 'cancelled', 'message': 'Operation cancelled.', 'details': {}}, stream=sys.stderr)
        return 130
    except StudioError as exc:
        _print_json({'code': exc.code, 'message': exc.message, 'details': dict(exc.details)}, stream=sys.stderr)
        return 3 if isinstance(exc, (DependencyError, ExportError, ImageError)) or exc.code.endswith('_io') else 2
    except OSError as exc:
        _print_json({'code': 'io_error', 'message': str(exc), 'details': {}}, stream=sys.stderr)
        return 3
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == '__main__':
    raise SystemExit(main())
