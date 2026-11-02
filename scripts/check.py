"""check.py — このプロジェクトの「完了」の唯一の定義。

使い方:
    python scripts/check.py          # チェックを順に実行し、最後に GATE: PASS / GATE: FAIL を出す
    python scripts/check.py --list   # 実行されるコマンドだけ表示する

チェックの決め方:
    1. scripts/check_commands.txt があれば、その中のコマンドを上から順に実行する
       （1行1コマンド。空行と # で始まる行は無視）
    2. なければプロジェクトを見て自動で決める
       - Python: ruff（設定があれば）/ mypy（設定があれば）/ pytest（テストがあれば）
       - Node:   package.json の typecheck / lint / test スクリプト

終了コード:
    0 = GATE: PASS       すべて成功
    1 = GATE: FAIL       どれかが失敗（最初の失敗で止まる）
    3 = GATE: NO-CHECKS  実行できるチェックが見つからない

標準ライブラリだけで動く。Windows / macOS / Linux 共通。
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_FILE = ROOT / "scripts" / "check_commands.txt"
PER_COMMAND_TIMEOUT = int(os.environ.get("CHECK_TIMEOUT_SEC", "600"))
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".claude", ".harness"}

for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        stream.reconfigure(encoding="utf-8", errors="replace")


def py() -> str:
    """今このスクリプトを動かしている Python を使う（python / python3 / py の違いを吸収）。"""
    exe = sys.executable or "python"
    return f'"{exe}"' if " " in exe else exe


def read_commands_file() -> list[str]:
    lines = COMMANDS_FILE.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def has_files(pattern: str, limit_dirs: int = 2000) -> bool:
    for seen, (_dirpath, dirnames, filenames) in enumerate(os.walk(ROOT), start=1):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if seen > limit_dirs:
            return False
        if any(Path(f).match(pattern) for f in filenames):
            return True
    return False


def module_available(name: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-c", f"import {name}"],
        cwd=ROOT, capture_output=True, check=False, text=True,
    )
    return result.returncode == 0


def detect_python() -> list[str]:
    pyproject = ROOT / "pyproject.toml"
    pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.exists() else ""
    is_python = bool(pyproject_text) or any(
        (ROOT / n).exists() for n in ("setup.py", "setup.cfg", "requirements.txt")
    ) or has_files("*.py")
    if not is_python:
        return []

    cmds: list[str] = []
    ruff_configured = "[tool.ruff" in pyproject_text or (ROOT / "ruff.toml").exists() or (ROOT / ".ruff.toml").exists()
    if ruff_configured:
        if shutil.which("ruff"):
            cmds.append("ruff check .")
        elif module_available("ruff"):
            cmds.append(f"{py()} -m ruff check .")

    mypy_configured = "[tool.mypy" in pyproject_text or (ROOT / "mypy.ini").exists()
    if mypy_configured and module_available("mypy"):
        cmds.append(f"{py()} -m mypy .")

    has_tests = (ROOT / "tests").is_dir() or has_files("test_*.py") or has_files("*_test.py")
    if has_tests:
        if module_available("pytest"):
            cmds.append(f"{py()} -m pytest -q")
        else:
            cmds.append(f"{py()} -m unittest discover -q")
    return cmds


def detect_node() -> list[str]:
    pkg = ROOT / "package.json"
    if not pkg.exists():
        return []
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
    except (json.JSONDecodeError, OSError):
        return []
    runner = "npm"
    if (ROOT / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
        runner = "pnpm"
    elif (ROOT / "yarn.lock").exists() and shutil.which("yarn"):
        runner = "yarn"
    cmds = []
    for name in ("typecheck", "lint", "test"):
        body = scripts.get(name, "")
        if body and "no test specified" not in body:
            cmds.append(f"{runner} run {name}")
    return cmds


def detect() -> tuple[list[str], str]:
    if COMMANDS_FILE.exists():
        return read_commands_file(), f"{COMMANDS_FILE.relative_to(ROOT)}"
    cmds = detect_node() + detect_python()
    return cmds, "自動検出（scripts/check_commands.txt で上書きできます）"


def run(cmd: str) -> tuple[int, str, float]:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=ROOT, capture_output=True, check=False,
            text=True, encoding="utf-8", errors="replace",
            timeout=PER_COMMAND_TIMEOUT,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = f"{exc.stdout or ''}{exc.stderr or ''}\n[timeout] {PER_COMMAND_TIMEOUT} 秒を超えました"
        code = 124
    return code, out, time.monotonic() - start


def main(argv: list[str]) -> int:
    cmds, source = detect()

    if "--list" in argv:
        print(f"# 取得元: {source}")
        for c in cmds:
            print(c)
        return 0 if cmds else 3

    print(f"# check.py — 取得元: {source}")
    if not cmds:
        print("実行できるチェックが見つかりません。scripts/check_commands.txt にテストやビルドのコマンドを書いてください。")
        print("GATE: NO-CHECKS")
        return 3

    results = []
    for cmd in cmds:
        print(f"\n$ {cmd}")
        code, out, secs = run(cmd)
        out = out.rstrip()
        if out:
            print(out)
        status = "ok" if code == 0 else f"exit {code}"
        print(f"[{status}] {secs:.1f}s")
        results.append((cmd, code))
        if code != 0:
            print("\n---- summary ----")
            for c, rc in results:
                print(f"{'PASS' if rc == 0 else 'FAIL'}  {c}")
            for c in cmds[len(results):]:
                print(f"SKIP  {c}")
            print(f"GATE: FAIL ({cmd})")
            return 1

    print("\n---- summary ----")
    for c, _ in results:
        print(f"PASS  {c}")
    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
