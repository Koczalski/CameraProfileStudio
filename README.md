# CameraProfileStudio

CameraProfileStudio は、LUT 作成支援に重視を置いたオープンソースの Windows ソフトウェアです（Python 3.12 / PySide6）。
自己満足の開発である為、Issue・PR の対応が難しいです。

- 日本語の詳細: [README.ja.md](README.ja.md)
- English: [docs/README.en.md](docs/README.en.md)
- 使い方（画像ルック作成）: [docs/IMAGE_LOOK_GUIDE.ja.md](docs/IMAGE_LOOK_GUIDE.ja.md)
- ビルド手順: [docs/BUILD_0.2.5.md](docs/BUILD_0.2.5.md)、[packaging/windows-launcher/BUILD-0.2.5.ja.md](packaging/windows-launcher/BUILD-0.2.5.ja.md)

## 現在開発中

ソースバージョン 0.2.5。Release 未定。

## 主な機能

### １．技術変換（プロファイル → LUT）

ICC・ICM・DCP カメラプロファイルに含まれる色処理を、そのまま 3D LUT にします。

* **入出力の指定**: カメラ RGB / sRGB / リニア sRGB / 任意 ICC、レンダリングインテント、黒点補正。
* **格子サイズ**: 17³ / 33³ / 65³。四面体補間でプレビュー・適用（無彩色が色付きしない）。

### ２．カメラルック抽出

DCP の HueSatMap・LookTable・トーンカーブから、そのカメラの色の傾向を sRGB → sRGB の LUT にします（近似）。

### ３．画像ルック作成

お手本にしたい写真（複数可・重み付き）の色の統計（トーン・色かぶり・彩度）を Oklab で解析し、ルックを 1 つの LUT にします。ルックをかける自分の写真を「適用先の画像」として登録すると、その写真との差分として推定されるため精度が上がります。画素対応は使いません。

### ４．カメラマッチ

同じ場面を 2 台で撮った写真から、色をそろえる LUT を推定します（位置合わせ、CCM＋トーン＋残差格子、ΔE00 の改善量を表示）。

### ５．一括処理

多数のプロファイルをまとめて変換。処理できない項目は実行前に理由付きで表示します。

### ６．書き出し形式

`.cube`、Hald CLUT PNG（16 bit）、ストリップ PNG（8 bit）。書き出しごとに検証と来歴（JSON）を保存します。

### ７．画像の読み込み

JPEG（MPF 付きカメラ JPEG を含む）・PNG・TIFF・RAW（LibRaw）。埋め込み ICC は sRGB に正規化し、EXIF 回転を反映します。

### ８．CLI

`camera_profile_studio.cli`: convert / match / image-look を JSON リクエストで実行できます。

## 技術スタック

* **Python 3.12 / NumPy / SciPy / OpenCV**: 色変換・LUT 生成・位置合わせ。
* **LittleCMS 2（lcms2）**: ICC 変換。
* **PySide6（Qt 6）**: GUI（日本語 / 英語）。
* **Nuitka + Inno Setup**: Windows 用インストーラーとポータブル版のビルド（`scripts/`, `packaging/`）。
* テスト: pytest（`tests/`）。

## 開発環境のセットアップ

Windows 10 / 11、Python 3.12。

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[gui,raw,test]
```

- LittleCMS 2（lcms2.dll）が必要です。`scripts\build-lcms.ps1` でビルドするか、環境変数 `CPS_LCMS2_DLL` に DLL の場所を指定してください。
- サンプルのカメラプロファイルはリポジトリに含めていません。入手方法は [input/ccd_profiles/README.md](input/ccd_profiles/README.md) を参照してください。ない場合、それを使うテストは自動でスキップされます。
- テスト: `pytest -q`　GUI の起動: `python -m camera_profile_studio.gui.app`
- exe のビルド: `packaging\windows-launcher\Build-0.2.5.cmd`

## コントリビューション・フィードバック

基本的には目を通します。
あらゆる種類のコントリビューションを歓迎します。
質問や提案がある場合は、Issue を作成してください。

## 免責事項

本ソフトウェアは無保証で提供します。使用の有無を問わず、本ソフトウェアの存在に起因・関連するあらゆる事象について、作者は一切の責任を負いません。
LUT はプロファイルや写真の統計からの近似であり、カメラ・メーカー・フィルムの色の正確な再現を保証するものではありません。

## ライセンス

### 本体

Camera Profile Studio のソースコードは **MIT License** です（Copyright (c) 2026 Koczalski、[LICENSE](LICENSE)）。
第三者のコンポーネントはそれぞれのライセンスのままで、MIT License によって変わることはありません。

### 使用している主なライブラリ

| コンポーネント | 用途 | ライセンス |
|---|---|---|
| Qt 6.9.1 / PySide6 / Shiboken6 | GUI | LGPL-3.0-only（または GPL-2.0 / GPL-3.0） |
| LittleCMS 2.17 | ICC 変換（lcms2.dll） | MIT |
| rawpy 0.25.1 | RAW 読み込み | MIT |
| LibRaw 0.21（rawpy に同梱） | RAW 現像 | LGPL-2.1 |
| NumPy / SciPy | 数値計算 | BSD-3-Clause |
| colour-science | 色空間・ΔE 計算 | BSD-3-Clause |
| Pillow | 画像入出力 | MIT-CMU（HPND） |
| OpenCV（opencv-python-headless） | 位置合わせ・リサイズ | Apache-2.0 |
| tifffile | TIFF 入出力 | BSD-3-Clause |
| Microsoft Fluent UI System Icons | GUI アイコン（`assets/icons/fluent/`） | MIT |
| Python 3.12 | 実行環境（配布版に同梱） | PSF License |
| Nuitka 2.7.12 | exe ビルド（ビルド時のみ） | Apache-2.0 |
| Inno Setup | インストーラー作成（ビルド時のみ） | Inno Setup License |
| pytest / pytest-qt | テスト（開発時のみ） | MIT |

- 各ライセンス文書の原文とバージョンの一覧: [licenses/](licenses/)（`manifest.json`、`third-party/`、`Qt/`、`LittleCMS/`）
- 配布版（exe）: Qt・PySide6・LibRaw は差し替え可能な別 DLL のまま配布し、LGPL に従って、ライセンス文書と対応するソースアーカイブ（`third-party-source/`）をポータブル版・インストーラーの両方に同梱します。同梱する Qt の DLL がすべてソースと対応付けられることはビルド時に検査し、使っていない Qt6Pdf は除外しています。根拠と公開時の確認項目: [licenses/DISTRIBUTION-NOTICES.md](licenses/DISTRIBUTION-NOTICES.md)
- LibRaw・Qt などの上流ソースの所在: [licenses/upstream-source/](licenses/upstream-source/)

### カメラプロファイル

サンプルのカメラプロファイル（.dcp / .icc）は**このリポジトリに含めていません**。再配布条件を確認できていないためです。入手方法は [input/ccd_profiles/README.md](input/ccd_profiles/README.md) を参照してください（RawTherapee・Photivo 由来のものは GPL-3.0）。
ユーザーが読み込むプロファイルは、ユーザー自身が使用権を持つものを使ってください。作成した LUT を配布する場合は、元のプロファイルの条件を確認してください。

## 連絡先

Discord: https://discord.com/users/618663163914878986

<sub>この README とソフトウェアの開発には、Anthropic の Claude を補助として使用しています。</sub>
