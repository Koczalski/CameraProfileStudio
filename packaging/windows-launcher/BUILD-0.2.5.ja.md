# Camera Profile Studio 0.2.5 のビルド（Windows）

`Build-0.2.5.cmd` をダブルクリックすると、同梱ソース（`CameraProfileStudio-0.2.5-Source`）の
公式手順（`docs/BUILD_0.2.5.md`）を、新しいビルドフォルダーで最初から最後まで実行します。

## 流れ

1. **事前チェック**（PC の設定は変更しません）— 足りないものがあれば、対処のコマンドを表示して止まります。
2. 開始してよいか確認（Enter で開始）
3. ソースを `b\<日時>` へ複製 → `-ValidateOnly` → `.venv` 作成と固定版パッケージのインストール
4. `-BuildApproved`：全テスト → LittleCMS → EXE（Nuitka）→ インストーラー（Inno Setup）→ 配布物の検査
5. 成果物を `release-0.2.5` へコピーし、SHA-256 を照合。ポータブル版も展開します。

0.2.5 の変更点（画像ルック作成の新しいエンジン、LUT の四面体補間）と、0.2.5・0.2.3・0.2.2 の変更点は `CameraProfileStudio-0.2.5-Source\docs\BUILD_0.2.5.md` にあります。

所要時間の目安は 30～90 分です。元のソースフォルダー、既存の `launcher.dist`、0.2.0 の配布物は変更しません。

## 必要なもの

| 項目 | 入っていないとき |
|---|---|
| Windows 10/11 x64 | — |
| Python 3.12 x64（0.2.0 は 3.12.15 で作成） | `winget install -e --id Python.Python.3.12` |
| Visual Studio 2022 の C++ x64 ビルドツール ＋ Windows SDK | `winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"` |
| インターネット接続（PyPI・GitHub・download.qt.io） | プロキシ・ファイアウォールを確認 |
| NTFS のドライブに 20 GB 程度の空き | `-BuildRoot` で別の場所を指定 |
| **インストール版の Camera Profile Studio がないこと** | 設定 ＞ アプリ からアンインストール（検査でインストーラーを試すため。ポータブル版は関係ありません） |

## できあがるもの

```
release-0.2.5\
  CameraProfileStudio-0.2.5-Setup.exe
  CameraProfileStudio-0.2.5-Portable.zip
  CameraProfileStudio-0.2.5-Source.zip
  DELIVERY_SHA256SUMS.txt / source-tests.log / smoke-results.json など
  portable\launcher.dist\CameraProfileStudio.exe   ← そのまま起動できる EXE
```

## オプション（コマンドプロンプトから）

```
Build-0.2.5.cmd -CheckOnly                 事前チェックだけ行う
Build-0.2.5.cmd -BuildRoot C:\cpsb         ビルドフォルダーの場所を変える（E: が NTFS でない場合など）
Build-0.2.5.cmd -Python C:\...\python.exe  使う Python を指定する
Build-0.2.5.cmd -Yes                       開始前の確認を省く
```

## 失敗したとき

- 画面に「失敗した手順」とログの場所が出ます。ログは `build-logs\<日時>\`（`summary.json`、各手順のログ、`project\` に主要ログの写し）にあります。
- ビルドフォルダーは調査用に残ります。再実行すると新しいフォルダーで最初からやり直します。
- Claude に「ビルドが失敗した」と伝えれば、`build-logs` を読んで原因を調べられます。

## 注意

- コード署名はしていません。他の PC へ配ると SmartScreen の確認が出ることがあります。
- 配布物の検査は「ホスト型スモーク」です。ネイティブのデスクトップ画面での受け入れ確認（画像ルック作成ウィンドウの目視確認など）は含みません（`docs/BUILD_0.2.5.md` の記載どおり）。

## リポジトリから実行する場合

`packaging\windows-launcher\Build-0.2.5.cmd`（または同じフォルダーの `Build.bat`）をリポジトリ内から実行すると、隣に `CameraProfileStudio-0.2.5-Source` がない場合はリポジトリのルートをソースとして使います。`.git`・`output`・`.venv`・ビルドフォルダー（`b`）・`build-logs`・`release-0.2.5*` は複製しません。ビルドフォルダー・ログ・成果物はこのフォルダーの `b\`・`build-logs\`・`release-0.2.5\` にでき、いずれも `.gitignore` で除外しています。パスが長いという警告が出る場合は `Build-0.2.5.cmd -BuildRoot C:\cpsb` のように短い場所を指定してください。
