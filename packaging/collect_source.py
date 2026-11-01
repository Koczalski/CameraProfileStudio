"""Create a filtered source archive without build products or interpreter caches."""
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TREES = ('src', 'assets', 'packaging', 'scripts', 'docs', 'tests',
         'licenses', '.github/workflows')
FILES = ('pyproject.toml', 'requirements-lock.txt', 'README.md', 'README.ja.md',
         'README_JA.md', 'README_EN.md', 'LICENSE', 'LICENSE.txt')
EXCLUDED_DIRS = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
                 '.git', '.venv', 'node_modules', 'build', 'dist', 'input', 'inputs', 'output', 'logs',
                 'delivery', 'releases', '.tools', 'fixtures', 'tasks'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.pyd', '.dll', '.exe', '.obj', '.o',
                     '.lib', '.a', '.pdb', '.ilk', '.zip', '.7z', '.log',
                     '.nef', '.nrw', '.cr2', '.cr3', '.arw', '.dng', '.raf', '.orf', '.rw2'}


def include(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    excluded_dirs = EXCLUDED_DIRS
    # License collectors preserve upstream paths, including Nuitka's build/inline_copy notices.
    if (relative.parts[0] == 'licenses'
            and path.name.lower().startswith(('license', 'copying', 'copyright', 'notice'))
            and path.suffix.lower() in ('', '.txt', '.rst', '.md')):
        excluded_dirs = EXCLUDED_DIRS - {'build'}
    return (not excluded_dirs.intersection(relative.parts)
            and not any(part.endswith('.egg-info') for part in relative.parts)
            and path.suffix.lower() not in EXCLUDED_SUFFIXES
            and not path.is_symlink())


def collect(destination: Path) -> None:
    paths = set()
    for tree in TREES:
        folder = ROOT / tree
        if folder.exists():
            paths.update(p for p in folder.rglob('*') if p.is_file() and include(p))
    paths.update(ROOT / name for name in FILES if (ROOT / name).is_file())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            archive.write(path, path.relative_to(ROOT).as_posix())
    print(f'Source archive: {destination}, {len(paths)} files; native binaries excluded')


if __name__ == '__main__':
    collect(Path(sys.argv[1]))
