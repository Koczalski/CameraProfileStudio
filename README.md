# CameraProfileStudio

カメラプロファイル（ICC / DCP）や写真から 3D LUT を作る Windows 用ソフトです。
個人の趣味で作っているため、Issue・PR への対応はあまりできません。

## できること

- プロファイルから LUT を作成（技術変換・カメラルック抽出）
- お手本の写真からルックを作成（画像ルック作成）
- 2 台のカメラの色合わせ（カメラマッチ）
- 一括変換
- 書き出し: .cube / Hald CLUT PNG / ストリップ PNG

ダウンロードは [Releases](../../releases) から。Windows 10 / 11（64 bit）。

## 開発

Python 3.12 / PySide6。

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[gui,raw,test]
python -m camera_profile_studio.gui.app
```

- LittleCMS（lcms2.dll）が必要です（`scripts\build-lcms.ps1`、または環境変数 `CPS_LCMS2_DLL`）。
- テスト用のカメラプロファイルは同梱していません（[input/ccd_profiles/README.md](input/ccd_profiles/README.md)）。
- exe のビルド: `packaging\windows-launcher\Build-0.2.5.cmd`
- 詳しい説明: [README.ja.md](README.ja.md) / [English](docs/README.en.md)

## ライセンス

MIT License（[LICENSE](LICENSE)）。同梱ライブラリ（Qt / PySide6、LibRaw など）はそれぞれのライセンスです（[licenses/](licenses/)）。

## 免責事項

無保証です。色は近似で、カメラやフィルムの正確な再現ではありません。

## 連絡先

Discord: https://discord.com/users/618663163914878986

<sub>開発には Anthropic の Claude を補助として使用しています。</sub>
