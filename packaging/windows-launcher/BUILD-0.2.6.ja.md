# Camera Profile Studio 0.2.6 のビルド（Windows）

`Build-0.2.6.cmd` をダブルクリックすると、同梱ソース（`CameraProfileStudio-0.2.6-Source`）の
公式手順（`docs/BUILD_0.2.6.md`）を、新しいビルドフォルダーで最初から最後まで実行します。

## 流れ

1. **事前チェック**（PC の設定は変更しません）— 足りないものがあれば、対処のコマンドを表示して止まります。
2. 開始してよいか確認（Enter で開始）
3. ソースを `b\<日時>` へ複製 → `-ValidateOnly` → `.venv` 作成と固定版パッケージのインストール
4. `-BuildApproved`：全テスト → LittleCMS → EXE（Nuitka）→ インストーラー（Inno Setup）→ 配布物の検査
5. 成果物を `release-0.2.6` へコピーし、SHA-256 を照合。ポータブル版も展開します。

0.2.6 の変更点（LUT 診断画面の追加）と、それ以前の変更点は `docs\BUILD_0.2.6.md` にあります。

所要時間の目安は、初回 15～40 分、キャッシュが効く 2 回目以降はさらに短縮されます（以前は約 30 分。PC とネットワークによります）。元のソースフォルダー、既存の `launcher.dist`、0.2.0 の配布物は変更しません。

## 必要なもの

| 項目 | 入っていないとき |
|---|---|
| Windows 10/11 x64 | — |
| Python 3.12 x64（0.2.0 は 3.12.15 で作成） | `winget install -e --id Python.Python.3.12` |
| Visual Studio 2022 の C++ x64 ビルドツール ＋ Windows SDK | `winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"` |
| インターネット接続（PyPI・GitHub・download.qt.io） | プロキシ・ファイアウォールを確認 |
| NTFS のドライブに 20 GB 程度の空き | `-BuildRoot` で別の場所を指定 |
| （任意）インストール版の Camera Profile Studio | あってもかまいません。インストール版には一切触れず、検査の「インストーラー試験」だけを省略して警告として記録します |
| （任意）uv | 入っていれば依存パッケージのインストールに使います（なければ pip）。`winget install -e --id astral-sh.uv` |

## できあがるもの

```
release-0.2.6\
  CameraProfileStudio-0.2.6-Setup.exe
  CameraProfileStudio-0.2.6-Portable.zip
  CameraProfileStudio-0.2.6-Source.zip
  DELIVERY_SHA256SUMS.txt / source-tests.log / smoke-results.json など
  portable\launcher.dist\CameraProfileStudio.exe   ← そのまま起動できる EXE
```

## オプション（コマンドプロンプトから）

```
Build-0.2.6.cmd -CheckOnly                 事前チェックだけ行う
Build-0.2.6.cmd -BuildRoot C:\cpsb         ビルドフォルダーの場所を変える（E: が NTFS でない場合など）
Build-0.2.6.cmd -Python C:\...\python.exe  使う Python を指定する
Build-0.2.6.cmd -Yes                       開始前の確認を省く
Build-0.2.6.cmd -NoCache                   キャッシュを使わず、すべて新規に取得・コンパイルする（クリーンビルド）
Build-0.2.6.cmd -CacheRoot D:\cpsc         キャッシュの場所を変える
```

## 速くするしくみ（キャッシュと並列化）

- 初回の実行から、EXE のコンパイル（Nuitka）を論理プロセッサー数で並列に行います（以前は 4 並列。空きメモリが少ない PC では自動で減らします）。
- 2 回目以降は、`%LOCALAPPDATA%\CameraProfileStudio-build-cache` に残したキャッシュを使います: pip／uv の取得済みパッケージ、Nuitka の C オブジェクト（変わっていないモジュールは再コンパイルしません）、ダウンロード済みの固定版ツールと上流ソース、LittleCMS の DLL（ソースの SHA-256・コンパイラー版・ビルド設定が同じときだけ。機能検査と PE 検査は毎回行います）。
- キャッシュは SHA-256 や内容から作ったキーで照合してから使うので、成果物は変わりません。EXE は毎回リンクし直し、すべての検査も省きません。フォルダーは不要なら丸ごと削除できます。`-NoCache` でキャッシュを使わないクリーンビルドもできます。
- テストは GUI 以外を並列（pytest-xdist）、GUI を 1 プロセスで順番に実行します。ポータブル版 ZIP は Inno Setup と並行して作り、配布物の検査のうち互いに独立な項目も並行して行います。
- 更新確認はテストと検査の間は無効です（`CPS_DISABLE_UPDATE_CHECK=1`）。ネットワークに出ず、更新バーも出ません。
- インストーラーの圧縮は速さ優先の `lzma2/fast` で、ブロック圧縮のスレッド数（`LZMANumBlockThreads`）はビルドが論理プロセッサー数（上限 256、空きメモリで制限）から決めて渡します。Nuitka の並列数とテストの並列数も、実行時の論理プロセッサー数と空きメモリから決めます。
- 開始時に「推定所要時間 約 N 分」を、各手順の開始時に「経過 m:ss / 残り 約 N 分」を表示します。直近に成功したビルド（`build-logs\*\summary.json`）の手順ごとの所要時間から計算するため、成功したビルドがまだない場合は経過時間だけを表示します（途中の定期更新はありません）。
- 残る最大の時間は Nuitka の最初の段階（Python コードの最適化。1 本のスレッドで約 7 分。`scipy.integrate._lebedev` だけで約 160 秒）で、並列化やキャッシュでは短くなりません。

## 失敗したとき

- 画面に「失敗した手順」とログの場所が出ます。ログは `build-logs\<日時>\`（`summary.json`、各手順のログ、`project\` に主要ログの写し）にあります。
- ビルドフォルダーは調査用に残ります。再実行すると新しいフォルダーで最初からやり直します。
- Claude に「ビルドが失敗した」と伝えれば、`build-logs` を読んで原因を調べられます。

## 注意

- コード署名はしていません。他の PC へ配ると SmartScreen の確認が出ることがあります。
- 配布物の検査は「ホスト型スモーク」です。ネイティブのデスクトップ画面での受け入れ確認（画像ルック作成ウィンドウの目視確認など）は含みません（`docs/BUILD_0.2.6.md` の記載どおり）。

## リポジトリから実行する場合

`packaging\windows-launcher\Build-0.2.6.cmd`（または同じフォルダーの `Build.bat`）をリポジトリ内から実行すると、隣に `CameraProfileStudio-0.2.6-Source` がない場合はリポジトリのルートをソースとして使います。`.git`・`output`・`.venv`・ビルドフォルダー（`b`）・`build-logs`・`release-0.2.6*` は複製しません。ビルドフォルダー・ログ・成果物はこのフォルダーの `b\`・`build-logs\`・`release-0.2.6\` にでき、いずれも `.gitignore` で除外しています。パスが長いという警告が出る場合は `Build-0.2.6.cmd -BuildRoot C:\cpsb` のように短い場所を指定してください。
