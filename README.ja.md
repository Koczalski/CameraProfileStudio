![Windows Build](https://github.com/Koczalski/CameraProfileStudio/actions/workflows/windows-distribution.yml/badge.svg)
# Camera Profile Studio

[English](README.md) | 日本語

**[ダウンロード（Releases）](../../releases)**

カメラプロファイル（ICC / DCP）や写真から 3D LUT を作る Windows 用ソフトです。個人の趣味で作っています。

## 主な機能

### 1. 技術変換
* **プロファイル → LUT**：ICC・DCP プロファイルの色処理を、そのまま 3D LUT にします。
* **入出力の指定**：カメラ RGB・sRGB・リニア sRGB・任意の ICC、レンダリングインテント、黒点補正。

### 2. カメラルック抽出
* **DCP のルック**：HueSatMap・LookTable・トーンカーブから、そのカメラらしい色の傾向を LUT にします（近似）。

### 3. 画像ルック作成
* **お手本の写真から**：1〜64 枚の写真のトーン・色かぶり・彩度を解析して、1 つのルックにまとめます。
* **適用先の画像**：ルックをかける自分の写真を登録すると、その写真との差分として推定するため、お手本に近づきます。

### 4. カメラマッチ
* **2 台の色合わせ**：同じ場面を撮った 2 枚の写真から、色をそろえる LUT を推定します。
* **数値で確認**：色の差（ΔE00）の改善量を表示します。

### 5. 一括処理
* **まとめて変換**：多数のプロファイルを一度に LUT にします。処理できないものは、始める前に理由付きで表示します。

### 6. 3 つの書き出し形式
* **.cube / Hald CLUT PNG（16 bit）/ ストリップ PNG（8 bit）**：写真・動画ソフトからゲームエンジンまで使えます。
* **補間**：プレビューと書き出しは四面体補間で、グレーに色が付きません。

### 7. プレビュー
* **適用前・適用後の比較**：JPEG・PNG・TIFF・RAW を読み込み、分割表示で比べられます。埋め込み ICC と EXIF の向きにも対応します。

### 8. 日本語・英語、ライト・ダーク
* **表示**：日本語と英語、ライトテーマとダークテーマを切り替えられます。

### 9. 更新
* **更新の確認**：ヘルプ > 更新を確認…（起動時にも 1 日に 1 回まで自動で確認します。設定で無効にできます）で、公開されている GitHub Releases を調べます。インストール版は新しいバージョンのダウンロード・SHA-256 の確認・インストールまで行い、ポータブル版はダウンロードページを開きます。取得するのは公開されているリリース一覧だけで、個人情報や設定は送信しません。

## 技術スタック

* **言語**：Python 3.12
* **GUI**：PySide6（Qt 6）
* **色処理**：NumPy / SciPy / colour-science / LittleCMS 2 / OpenCV / LibRaw（rawpy）
* **ビルド**：Nuitka / Inno Setup
* **対応 OS**：Windows 10 / 11（64 bit）

## 開発

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[gui,raw,test]
python -m camera_profile_studio.gui.app
```

* LittleCMS（lcms2.dll）が必要です。`scripts\build-lcms.ps1` でビルドするか、環境変数 `CPS_LCMS2_DLL` で指定します。
* テスト用のカメラプロファイルは同梱していません（[input/ccd_profiles/README.md](input/ccd_profiles/README.md)）。
* exe のビルド：`packaging\windows-launcher\Build-0.2.6.cmd`
* 詳しい資料：[docs/DETAILS.ja.md](docs/DETAILS.ja.md)

## コントリビューション・フィードバック

個人の趣味の開発なので、Issue・PR への対応はあまりできません。目は通します。

## ライセンス

[MIT License](LICENSE) です。同梱しているライブラリ（Qt / PySide6、LibRaw など）は、それぞれのライセンスに従います（[licenses/](licenses/)）。
本ソフトウェアは無保証です。色は近似で、カメラやフィルムの正確な再現ではありません。

## 連絡先

Discord: https://discord.com/users/618663163914878986

<sub>開発には Anthropic の Claude を補助として使用しています。</sub>
