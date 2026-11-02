# Camera Profile Studio
カメラプロファイル（ICC / DCP）や写真から 3D LUT を作る Windows デスクトップアプリ。Python 3.12 + PySide6。個人の趣味プロジェクト。

# コマンド
- セットアップ: `py -3.12 -m venv .venv` → `.venv\Scripts\activate` → `pip install -e .[gui,raw,test]`
- 起動（GUI）: `python -m camera_profile_studio.gui.app`
- テスト全体: `.venv\Scripts\python.exe -m pytest -q`
- テスト1ファイル: `.venv\Scripts\python.exe -m pytest tests/test_lut_engine.py -q`
- 完了判定: `.venv\Scripts\python.exe scripts\check.py`（最後に `GATE: PASS` が出れば完了。中身は scripts/check_commands.txt）

# 決まっていること
- 依存のバージョンは pyproject.toml と requirements-lock.txt で固定している。勝手に上げない・足さない
- LittleCMS（lcms2.dll）はリポジトリに含めない。`scripts\build-lcms.ps1` でビルドするか `CPS_LCMS2_DLL` で指定する
- input/ccd_profiles のサンプルプロファイルは配布できないため置いていない。無いときは tests/conftest.py が関係するテストを skip する

# テストのマーカー
- `gui`（PySide6 が必要）、`lcms`（本物の lcms2.dll が必要）、`reference`（独立した参照データが必要）、`windows`（Windows 統合）
- 完了判定では既定でこれらを除外している

# 進め方
- 影響が複数ファイルに広がる変更は、計画を見せてから実装する
- バグ修正は、再現する失敗テストを先に書く
- 完了の報告には、実行したコマンドと GATE 行を含める
- 仕様の詳細は docs/ にある（REQUIREMENTS.md、TECHNICAL_SPEC.md、UI_DESIGN.md など）。必要なものだけ読む
- 中断・引き継ぎのときは STATE.md を更新する（/handoff）

# 2回以上直したこと
