# Camera Profile Studio

[English](README.en.md)

> **ソースバージョン 0.2.7** は、0.2.6 に、LUT 比較画面（複数の LUT と複数の写真を1つの表で見比べ、コンタクトシートと比較画像の JPEG に書き出す）を加えたものです。0.2.6 は、0.2.5 に LUT 診断画面とアプリ内の更新確認を加えたものです。0.2.5 は、0.2.4 に、画像ルック作成の新しいエンジン（Oklab での統計の比較。ルックをかける自分の写真を「適用先の画像」として登録できる。無彩色の保護の設定は廃止）と、LUT の四面体補間を加えたものです（[docs/BUILD_0.2.7.md](BUILD_0.2.7.md)、[docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md)）。0.2.4 は、0.2.3 に例外処理の強化（理由と対処の分かるエラー文、exFAT/FAT32 への書き出し、設定ファイル破損時の起動など）を加えたものです。0.2.3 は、0.2.2 に「プレビュー画像入りのカメラ JPEG（MPF）が読み込めない」不具合の修正を加えたものです。0.2.2 は、0.2.1 に PNG 形式の LUT 書き出し（.cube に加えて Hald CLUT PNG・ストリップ PNG）と、一括処理のエラー防止（現在の設定では処理できない項目を、実行前に理由付きで表示）を加えたものです。0.2.1 では GUI の不具合修正と、バージョン情報・ライセンス表示を加えました（[docs/BUILD_0.2.7.md](BUILD_0.2.7.md)）。0.2.0 では **Image Look Creator（画像ルック作成）** を追加しました（近似・未検証。[docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md) を参照）。0.2.0 の検証範囲は、配布物に添付された 0.2.0 のリリースレポートを参照してください。以下の過去の証拠は **0.1.0** についてのもので、Image Look Creator は対象外です。
>
> **バージョン 0.1.0：** エンジン・CLI・GUIは実装済みで、**ソースからの実行で**テストしました。GUIの操作手順はWindows 11で確認しています。**インストーラーとポータブルZIPはビルド済みで、ビルドマシン上で範囲を限ったスモークテストに合格しました（クリーンなVMではありません）。**
> 機能が実装されていることと、実機の色を正確に再現できることは別です。**撮って出しJPEGの再現精度は、全機種で未検証です。**
> 証拠に基づく現状は [docs/FINAL_REPORT.md](FINAL_REPORT.md) を参照してください。

Camera Profile Studio は、ICC・ICM・DCPカメラプロファイルを読み込み、プロファイルに実際に含まれる色処理を3D LUT（`.cube`）にします。プロファイルを使う3つの機能に加えて、0.2.0 では、補正前の画像を使わず仕上がった写真だけから推定する Image Look Creator を追加しました。

| 機能 | 出力 | 対象外 |
|---|---|---|
| **Technical Convert** | プロファイルが定義する色変換（例: プロファイルのデバイスRGB → sRGB）のLUT。LittleCMS 2の32-bit floatで計算 | 完成したJPEGにそのまま適用するルックではない |
| **Camera Look Extractor** | DCP内のルック要素（HueSatMap、LookTable、ToneCurve）から作るsRGB → sRGBのLUT | カメラ本体のJPEG処理の再現ではない。該当タグがないプロファイルからはルックを作らない |
| **Camera Match Studio** | 対応する色サンプル（色票パッチ、または同一シーンと確認した画像ペア）から推定するsRGB → sRGBのLUT。検証用データのΔE00を表示 | 対応関係のない「参考写真」からは推定できない |
| **Image Look Creator**（0.2.0） | 仕上がった参考写真1〜64枚（JPEG/PNG/TIFF）と、ルックをかける自分の写真（適用先の画像。任意）の統計の差から推定する、近似的なsRGB → sRGBのLUT 1つ。補正前の画像は不要 | 元画像・カメラ・OEM・フィルムのルックの復元ではない。精度やΔEのスコアはない。全体のトーン・色かぶり・彩度のみ |

色変換エンジンはGUIなしでも動きます（Python APIとCLI）。そのため自動テストできます。

## LUTを書き出す前に：5つのルール

1. **デバイスRGB用のLUTはJPEGに適用しない。** カメラICC/DCPから作る技術変換LUTの入力は*カメラのデバイスRGB*です。ICCではプロファイル固有の入力座標、DCPでは黒レベル減算・デモザイク・ホワイトバランス済みのカメラリニアRGBを指します。通常のJPEGやPNGは現像済みのsRGBなので、適用すると色が正しくなりません。このようなLUTはプレビューできず、エクスポートしたメタデータには入力色空間が記録されます。
2. **ここでのsRGBは、Rec.709映像ガンマではなくsRGB伝達関数を指す。** `srgb` はRec.709/sRGB原色、D65白色、sRGB伝達関数です。`linear_srgb` は同じ原色・白色で、伝達関数がリニアのものです。Rec.709（BT.709/BT.1886）の映像タイムラインで使う場合は、トーンカーブがLUTの前提と異なります。
3. **格子サイズのプリセットは17・33・65。** 格子が大きいほど直接変換に近づき、ファイルも大きくなります（17³ = 4,913行、33³ = 35,937行、65³ = 274,625行）。解析的なテスト用プロファイルで、書き出したLUTとLittleCMSの直接変換を28,228色で比較した実測例は次のとおりです。

   | 格子 | ΔE00平均 | ΔE00最大 |
   |---|---:|---:|
   | 17 | 0.050976 | 2.045431 |
   | 33 | 0.012305 | 0.867180 |
   | 65 | 0.003430 | 0.260683 |

   これは補間誤差であり、実機の精度ではありません。プロファイルによっては（特にリニア入力の暗部で）異なる結果になります。出典：`output/lut-quality/quality.json`。
4. **強度はファイルに一度だけ焼き込む。** `出力 = (1 − s) × 入力 + s × 完全な変換`（0 ≤ s ≤ 1）。1以外の強度は入力と出力の色空間が同じ場合に限り使えます。実際にはsRGB → sRGBのルックとマッチが該当します。プレビューは焼き込み済みLUTをサンプリングするため、プレビューとエクスポートの結果は同じです。ファイルに強度100%版が残るわけではありません。
5. **SDRのみ。** 値は0〜1の範囲に限ります。HDR、Log/V-Log、浮動小数点のHDR画像には対応しません。シェーパー付きの.cubeや値域が0〜1以外の.cubeは読み込み時に拒否します。

## 基本的な使い方（GUI、ソースから実行）

左のナビゲーションに6つのワークスペース（**Profile Inspector、Technical Converter、Camera Look Extractor、Camera Match Studio、Batch Processing、Settings**）があり、下部のパネルにJobsとLogが表示されます。ソースバージョン 0.2.0 では7つ目のワークスペース **Image Look Creator**（[後述](#image-look-creator020近似)）が加わりました。この節で述べる検証の対象には含まれません。

以下の手順は、Windows 11上でネイティブの `windows11` スタイルを使い、100/150/200%のスケーリングをエミュレートして、ソースから実行して確認しました（`output/gui-validation/`、[docs/UI_DESIGN.md](UI_DESIGN.md)）。GUIは3つのスケールすべてで、ソースからの実行として受入済みです。パッケージ版の確認はずっと狭い範囲です。ネイティブのウィンドウを1つ目視確認し、JobsタブとLogタブを選択しただけです（[Windows向け成果物のビルド](#windows向け成果物のビルド)を参照）。

1. **プロファイルを開く。** `.icc`・`.icm`・`.dcp` をウィンドウにドラッグ＆ドロップするか、ファイル選択ダイアログで選びます。Inspectorに形式、バージョン、デバイスクラス、色空間、PCS、タグ、SHA-256が表示されます。
2. **プロファイルで何ができるかを確認する。** プロファイルごとに次の4項目を表示します。

   | 項目 | 状態 |
   |---|---|
   | Technical Conversion | 対応 / 非対応 |
   | Camera Look Extraction | 対応 / 部分対応 / 不可（モードごとにも表示） |
   | Camera Match 参照データ | あり / なし |
   | 実機JPEG再現精度 | 検証済み / 未検証 |

   実行できない組み合わせは操作できなくなり、理由が表示されます。変換に失敗した場合に、ダミーや無変換のLUTを代わりに出力することはありません。
3. **入力ドメインとモードを選ぶ。** Converterは、プロファイル → 変換・色空間 → LUTサイズ・強度 → Advanced（既定では閉じている） → 状態・エクスポート、の順に並んでいます。
   - 技術変換の方向：プロファイルRGB → sRGBまたはリニアsRGB、sRGB → プロファイルRGB、ICC → ICC（出力プロファイルは別の選択欄で選ぶ）。
   - プロファイルが対応しない方向は選べず、エンジンの理由が表示されます。たとえば `Nikon_D200.icc` は逆変換の経路がないため、出力側に指定できません。
   - ルックのモード：Colors Only / Tone Curve Only / Look Only / Combined。
   - DCPの技術変換では、カメラのニュートラル値（ホワイトバランス）を入力する必要があります。2光源のプロファイルではケルビン単位の色温度も必要です。入力するまでエクスポートは無効のままで、ホワイトバランスを推測することはありません。
4. **プレビューする。** Before/Afterプレビューは、入力がsRGBのLUTだけに対応します。読み込める画像はJPEG・PNG・TIFFです。埋め込みRGB ICCは変換し、ICCのない画像は警告付きでsRGBとして扱います。オプションの `raw` コンポーネントを入れるとカメラRAWも読み込めます。
5. **`.cube` を書き出す。** サイズと強度を選びます。`名前.cube` と付属の `名前.json` を書き出します。.cubeの上書きは保存ダイアログで確認し、既存の付属JSONは別に確認します。確認しない限り、何も上書きしません。

**Camera Match** には実際の対応関係が必要です。次の2つの入力で動作を確認しました。

- パッチCSV：列は `source_r, source_g, source_b, target_r, target_g, target_b, group` で、値は0〜1のエンコード済みsRGBです。
- 同一シーンであるとユーザーが確認した2枚の画像：位置合わせを行い、インライア率と有効領域の割合を表示します。プレビューで確認して承認するまで、推定は実行できません。

推定の前にサンプルを学習用と検証用のグループに分け、ΔE00は検証用データについてのみ、警告付きで報告します。マッチの結果が無補正より改善しないこともあり、その場合はレポートにそう表示されます。同じシーンや色票から取った検証用データでは、他のシーンでの精度は確かめられません。

**RAWファイル** は1つの固定レシピで現像します（LibRaw/rawpyのAHDデモザイク、16-bit、sRGB出力、カメラWBまたはユーザー指定WB、自動明るさ補正なし）。マッチングの入力を一定にするためのもので、メーカーの撮って出しJPEGの処理ではありません。

**Batch Processing** は独立したジョブを順に処理し、ジョブごとに結果を表示します。成功と失敗が混在するのは通常のことです（たとえば不正なプロファイルだけが失敗し、他は成功します）。出力先に既存のファイルがある場合は、置き換え・既存をスキップ・キャンセルから選びます。

キャンセルは協調型で、実行中のネイティブ処理は現在の処理単位を終えてから止まります。ファイルの書き込みが確定した後にキャンセルした場合は、実際の結果どおり成功として、ファイルのパスとともに報告します。本当に部分的な書き出しで失敗した場合は、キャンセルしていても失敗として報告します。

**言語とテーマ**：

- 画面の表示は日本語と英語を再起動せずに切り替えられます（Ctrl+Shift+L）。エンジンが返す理由や技術的な診断情報は英語のまま表示されることがあります。
- テーマはシステム・ライト・ダークから選べます。Qtの `windows11` スタイルが使える環境ではそれを使い、使えない場合は `windowsvista`、`Fusion` の順にフォールバックします。
- 狭いウィンドウでは、設定欄を横にスクロールするか、スプリッターで幅を調整する必要がある場合があります。
- 検証用のプローブで、実際のNikon D70sのRAWファイルをGUIのプレビューでデコードできることを確認しました。これはデコードの確認であり、実機の精度の確認ではありません。

## Image Look Creator（0.2.0、近似）

Image Look Creator は、仕上がった参考写真1〜64枚から（0.2.5 で新しいエンジンになりました。ルックをかける自分の写真を「適用先の画像」として加えることもできます）、全体にかかるsRGB → sRGBのルックを **1つ** 推定します。Camera Match Studio とは別の機能で、サンプルの組を作らず、補正前の画像も不要です（参照画像1枚でも作れます）。正解データがないため、ΔEは表示しません。

- **推定するもの：** なめらかで単調なトーンカーブ、明るさの帯ごとの小さな色かぶり、彩度の変化（明るさの帯ごと・色相区分ごと）。Oklab での色の統計から求めます。ルックは、**参考画像**（まねたい仕上がりの写真）の統計から**適用先の画像**（ルックをかける自分の写真。任意だが推奨）の統計を引いたものです。適用先の画像を登録しない場合は、組み込みの「一般的な写真」の統計と比べますが、精度は下がります（警告 `typical_photo_baseline`）。画素どうしの対応は使いません。推定値の幅は意図的に狭くしているので、強いルックは弱めに出ます。
- **参考画像・適用先の画像：** JPEG・PNG・TIFF（8/16ビットRGB。埋め込みICCはsRGBに変換）。RAWファイルは対応形式ではありません（GUIで選べるのはJPEG・PNG・TIFFのみ）。画像ごとに正の重み（既定は均等。画素数ではなく画像単位で正規化）と、任意の正規化トリミング `[x0, y0, x1, y1]` を指定できます。場面の種類が多様なほど、場面に引きずられにくくなります。
- **調整項目：** トーン・色かぶり・彩度の適用量は互いに独立した0〜1の値で、解析設定の一部です。変更したら再解析が必要です。参考画像どうしが食い違う場合（トーン・色・彩度）は、選択を変えるか食い違いを明示的に了承するまでLUTを作れません。
- **出力：** プレビューは、解析に使わない別の対象画像で行います。全体の強度はLUT生成時に1回だけ焼き込み、プレビューも書き出すLUTを使います。格子は17・33・65。付属JSONには、画像のファイル名と役割（絶対パスは記録しない）、SHA-256、重み、トリミング、シード、設定、警告と、状態 `approximate_unverified` を記録します。
- **検証：** この機能の検証範囲は、配布物に添付された 0.2.0 のリリースレポートに記載されています。このREADMEにある0.1.0の証拠は、この機能を対象にしていません。0.2.0 のリリースレポートの範囲は、0.2.5 の新しいエンジンには及びません。

ユーザーガイド：[docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md)。開発者向けの契約（英語）：[docs/IMAGE_LOOK_API.md](IMAGE_LOOK_API.md)。

## ソースからのインストールと実行（開発者向け）

> 最初のスタンドアロン版は、`PATH` にPythonがない状態で動作しました（クリーンなVMではありません）。ソースからのインストールにはPythonが必要です。

必要なもの：Windows x64、CPython **3.12** x64（パッケージングスクリプトは3.12を前提にしています。`pyproject.toml` は3.13も許容）。コマンドはすべてPowerShellでリポジトリのルートから実行してください。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
# 固定版のネイティブLittleCMS 2.17をビルド（Visual Studio 2022のC++ x64ツールが必要）
powershell -NoProfile -File scripts/build-lcms.ps1
$env:CPS_LCMS2_DLL = (Resolve-Path .tools\packaging\lcms-runtime\lcms2.dll).Path
```

- **依存関係**：`requirements-lock.txt` は完全な固定セットです（基本・GUI・RAW用の `rawpy==0.25.1`・テスト・ビルド）。予定しているスタンドアロン版にはrawpyを同梱します。rawpyを省けるのは最小構成の開発環境だけで、その場合RAWファイルは `raw_decoder_missing` で拒否されますが、それ以外の機能は使えます。
- **LittleCMS**：ソースZIPはネイティブバイナリ（`.dll`）を除外するため、LittleCMSのネイティブランタイムはソースZIPに**含まれません**。
  - `scripts/build-lcms.ps1` は、固定版 `lcms2.17` のソースアーカイブを取得してSHA-256を照合し、公式のVC2022プロジェクト（静的CRT）でビルドしたうえで検証し、`SOURCE.json` とともに `.tools/packaging/lcms-runtime/` に出力します。出力先は `-OutputDirectory` で変更できます。
  - 環境変数 `CPS_LCMS2_DLL` に、ビルドしたDLLのパスを設定してください。作業ツリーに `src/camera_profile_studio/vendor/lcms2/lcms2.dll` がある場合は、自動的にそれを使います。
  - アプリの実行中にダウンロードを行うことはありません。

テストの実行：GUIテストには固定の外部プロファイルが必要なので、先に取得してください。取得にはネットワークを使い、リビジョンとSHA-256は固定です。保存先は `input/ccd_profiles/` です。

```powershell
.venv\Scripts\python.exe tasks/ccd/acquire.py
$env:QT_QPA_PLATFORM = 'offscreen'
.venv\Scripts\python.exe -m pytest
```

GUIの起動：

```powershell
.venv\Scripts\python.exe -m camera_profile_studio.gui
```

## コマンドライン

CLIは [docs/API_CONTRACT.md](API_CONTRACT.md) の契約に従います。下記の例1と例2は2026-10-20にリポジトリのルートから実行し、有効な33³のLUTを出力できました。`match` コマンドは自動テスト（`tests/test_cli.py::test_real_cli_matching_holdout`）で確認しています。このテストは合成したNPZサンプルを使い、検証用データの誤差が改善することと、学習用と検証用のインデックスが重ならないことを確かめます。合成データのテストなので、実機やOEMの精度については何も示しません。パッケージ版のスモークテストでも、合成データでSciPyの `ccm_tone_lut` によるマッチングを実行しました。

```text
python -m camera_profile_studio inspect PROFILE [--json]
python -m camera_profile_studio convert --request REQUEST.json --output OUT.cube [--size 17|33|65] [--strength FLOAT] [--overwrite]
python -m camera_profile_studio match --samples SAMPLES.npz --output OUT.cube [--settings SETTINGS.json] [--size 17|33|65] [--strength FLOAT] [--overwrite]
python -m camera_profile_studio image-look --request REQUEST.json --output OUT.cube [--size 17|33|65] [--strength FLOAT] [--overwrite]
python -m camera_profile_studio gui
```

インストールされるコンソールスクリプト `camera-profile-studio` も同じプログラムです。既定値は `--size 33`、`--strength 1.0` です。成功すると、JSONの結果（`export`、`match` ではさらに `holdout_report`、`image-look` ではさらに `status: "approximate_unverified"`・`warnings`・`conflicts` を含む `image_look_report`）を標準出力に出します。エラーは標準エラー出力にJSON `{"code", "message", "details"}` で出します。

終了コード：

- `0`：成功
- `2`：検証エラーまたは非対応の要求（例：`strength_space_mismatch`）
- `3`：依存関係・画像・エクスポート・I/Oのエラー（例：同名の.cubeまたは付属JSONがすでにあり、`--overwrite` を指定していない場合の `output_exists`）
- `130`：キャンセル

`inspect` は、一部の機能が非対応でも解析に成功すれば0を返します。`image-look` は、参考画像が食い違っていて要求に `"acknowledge_conflicts": true` がない場合、`image_look_conflict` で2を返します。

**例1：ICCの技術変換**（プロファイルのデバイスRGB → sRGB）。リポジトリのルートに `d70-technical-request.json` として保存します。

```json
{
  "schema_version": 1,
  "input_space": {"kind": "icc_rgb", "profile_path": "input/ccd_profiles/Nikon_D70.icc"},
  "output_space": {"kind": "srgb"},
  "mode": "technical",
  "profile_path": "input/ccd_profiles/Nikon_D70.icc",
  "intent": "relative",
  "black_point_compensation": false,
  "gamut": "clip"
}
```

```powershell
.venv\Scripts\python.exe -m camera_profile_studio inspect input\ccd_profiles\Nikon_D70.icc --json
.venv\Scripts\python.exe -m camera_profile_studio convert --request d70-technical-request.json --output output\d70-technical.cube --size 33
```

できあがるLUTの入力はプロファイルのデバイスRGBで、sRGBではありません。JPEGには適用しないでください（ルール1）。

**例2：DCPのルック**（sRGB → sRGB）。`Nikon_D70s.dcp` は2光源のプロファイルなので、色温度を明示的に指定する必要があります。5000 Kは記入例であり、推奨値ではありません。`d70s-look-request.json` として保存します。

```json
{
  "schema_version": 1,
  "input_space": {"kind": "srgb"},
  "output_space": {"kind": "srgb"},
  "mode": "look",
  "profile_path": "input/ccd_profiles/Nikon_D70s.dcp",
  "dcp": {"illuminant_kelvin": 5000},
  "look_mode": "combined"
}
```

```powershell
.venv\Scripts\python.exe -m camera_profile_studio convert --request d70s-look-request.json --output output\d70s-look.cube --size 33 --strength 0.8
```

要求ファイル・設定ファイルのルール：

- エンジンのデータクラス（`TransformRequest`、`DcpSettings`、`MatchSettings`）と同じ構造で、フィールド名はsnake_caseです。省略した任意フィールドには既定値が入ります。
- 未知のフィールド、重複したキー、NaN/Inf、1 MiBを超えるファイルは拒否します。
- `schema_version` は省略できます。指定する場合は `1` だけを受け付けます。
- 相対パスはカレントディレクトリが基準です。リポジトリのルートから実行してください。**例外：** `image-look` の要求ファイルでは、参照画像の相対パスは要求JSONがあるフォルダーが基準です。`image-look` の完全な例は [docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md) の10章にあります。要求ファイルの `references` の各項目には、任意で `"role"`（`"reference"`＝既定、または `"base"`）を指定できます。
- 出力先のフォルダーはあらかじめ作成しておいてください。
- 要求ファイルを、出力の付属JSON（`OUT.cube` と同じ場所の `OUT.json`）と同じ名前にしないでください。CLIは `source_output_collision` として拒否します。

`SAMPLES.npz` には、ちょうど次の4つが必要です：`source_rgb` と `target_rgb`（N×3、0〜1のエンコード済みsRGB）、`groups`（行ごとのグループIDを並べたUnicodeのベクトル）、`evidence`（対応関係の出所を説明するUnicodeのスカラー）。`allow_pickle=False` で読み込み、展開後のサイズの上限は256 MiBです。

## Windows向け成果物のビルド

> **0.2.7 のビルド：** [docs/BUILD_0.2.7.md](BUILD_0.2.7.md) に従ってください。新しいチェックアウト用の手順（`scripts/build-fresh-source.ps1`）と、保護された既存の作業ツリー用の手順（`scripts/build-release.ps1`）を説明しています。この節の以下にある `build-windows.ps1` のコマンドと結果は、過去の **0.1.0** のビルドのもので、現在の 0.2.7 の手順ではありません。

ビルドマシンに必要なもの：Visual Studio 2022のC++ x64ツールとWindows SDK（ビルドマシンでのみ使います）。手順：

```powershell
powershell -NoProfile -File scripts/build-windows.ps1
powershell -NoProfile -File scripts/verify_distribution.ps1
```

ビルドにはNuitka 2.7.12のstandaloneモードとInno Setup 6.0.5を使います。配布物とその状態は次のとおりです（状態の欄は過去の **0.1.0** のビルドの記録です。0.2.0 については、配布物に添付された 0.2.0 のリリースレポートを参照してください）。

| ファイル | 内容 | 状態 |
|---|---|---|
| `output/CameraProfileStudio-Setup.exe` | ユーザー単位のインストーラー（未署名） | ビルド済み、スモーク合格 |
| `output/CameraProfileStudio-Portable.zip` | スタンドアロン版のフォルダー（Python不要） | ビルド済み、スモーク合格 |
| `output/CameraProfileStudio-Source.zip` | フィルター済みのソース（ネイティブバイナリなし） | 作成済み。ペイロード内とポータブルZIP内のコピーとバイト単位で一致 |
| `output/CCD_Profile_LUTs.zip` | タスク01のLUTと証拠 | 作成・検証済み（68エントリ） |
| `output/Medium_Format_LUT_Research.zip` | タスク02の報告書・補足資料・配布用の出典索引・スクリプト | 作成・配布済み（28エントリ） |
| `output/Validation_Evidence.zip` | テスト・監査・GUI検証の証拠 | 作成・検証済み。正確な内容一覧は同梱の `BUNDLE_MANIFEST.json` |

SHA-256は本文書には書かず、配布物と並べて置く `DELIVERY_SHA256SUMS.txt` と `DELIVERY_MANIFEST.json` で公開します。`build-windows.ps1` は、ビルド環境の内容で `requirements-lock.txt` も書き直します。

GitHub Actionsのワークフローは `.github/workflows/windows-distribution.yml`（手動実行、`windows-2022`）です。LittleCMSのビルド、固定のテスト用プロファイルの取得、テストの後にビルドを行いますが、まだ実行していません。詳細とスモークテストの範囲は [packaging/README.md](../packaging/README.md) を参照してください。

### パッケージ版：スモークテストの結果

証拠：`output/distribution/first-build-results.json`、`output/distribution/repack-results.json`、`output/distribution/smoke-results.json`、`output/distribution/final-smoke/`。ビルドマシン上で、`PATH` からPythonを外し、最初のビルドと `-SkipCompile` による再パッケージ後（ソースの指紋とEXEは不変）の両方で実行しました。次の確認はすべて合格です。

- ポータブル版の `--help`、生成したICCの `inspect`、sRGB → リニアsRGB（17³、4,913行。曲線を直接計算した値との最大誤差2.67e-8）。
- D70sのLook Only（17³）：値は有限で、恒等LUTではない。
- SciPyの `ccm_tone_lut` によるマッチング（17³）。グループ分けした合成サンプル（学習用192件、検証用64件、重複なし）で、検証用の平均ΔE00が4.69993から0.06843に下がった。合成データであり、OEMの精度ではない。
- GUIはオフスクリーンで8秒間動作を続けた。ネイティブのウィンドウが開き、UI AutomationでJobsタブとLogタブを選択した。リードがウィンドウのキャプチャーを確認した（ダーク・英語のProfile Inspectorが1画面。文字とアイコンは読め、エラー表示はない）。
- インストーラーは、正確なAppIdで既存の登録を調べ、登録がないことを確認した（HKCU/HKLM、32/64ビット）。ユーザー単位のサイレントインストール、インストール後の `--help`、アンインストールはどれも終了コード0で、アンインストールで登録が削除された。
- インベントリ：ネイティブバイナリ178個、ソースと対応付けたQtのファイル18個、FluentのSVGアイコン28個とそのライセンス表示。ZIPのCRCは合格。カメラプロファイルやRAWファイルは含まない。

確認していないもの：パッケージ版の日本語UI、その他のパッケージ版の操作、DPI設定、パッケージ版GUIでのRAW、クリーンなVM、ネットワークの遮断、コード署名、法的なクリアランス。任意のMatplotlib APIはビルドでは使えませんが、静止画のマッチングはそれなしで合格しました。ビルドのためにエンジンやGUIのコードは変えていません。

最終パッケージの手順：文書だけの更新では、同じコンパイル済みEXEを再利用し（`-SkipCompile`）、公開前にスモークテスト全体をやり直します。最終的なサイズとSHA-256は、配布物と並べて置く `DELIVERY_SHA256SUMS.txt` と `DELIVERY_MANIFEST.json` が正式な値です。

## スクリーンショット

Windows 11上で、`windows11` スタイルを使ってソースから実行し、ウィンドウのクライアント領域を取得したものです。各スケールのフォルダーに、全ワークスペースの英日 × ライト/ダークと、狭いウィンドウの画像があります。

スクリーンショットはリポジトリに含めていません。`python tests/gui_screenshots.py` を実行すると `output/gui-validation/screens/` に生成されます。

スケーリングは、225%設定のホスト上で `QT_SCALE_FACTOR` を使ってエミュレートしたもので、Windowsの表示スケール設定自体は変えていません。各実行の操作記録は `output/gui-validation/manifest_scale*.json` にあります。パッケージ版の画面ではありません。パッケージ版のキャプチャーは `output/distribution/final-smoke/native-app-window.png` です。

## ライセンスと再配布

- **カメラプロファイルは同梱していません。** 使用する権利のあるプロファイルを用意してください。テスト用のサンプルプロファイルの入手方法は [input/ccd_profiles/README.md](../input/ccd_profiles/README.md) を参照してください。サンプルプロファイルとそれから作った LUT は、元の条件を確認するまで再配布しないでください。
- **Qt / PySide6 6.9.1** はLGPL-3.0-onlyで使用しています。スタンドアロン版では、Qtのライブラリを差し替え可能な個別のDLLのまま配置します。差し替えたライブラリのデバッグを目的とするリバースエンジニアリングを、アプリケーションは制限しないと告知文に記載しています。
- **rawpy / LibRaw**：rawpyはMITです。LibRawのランタイムは、差し替え可能な個別のDLLとしてLGPL-2.1で配布されています。
- **ソースアーカイブ**：PySide6/Shiboken6、使用するQt 6.9.1のモジュール、LibRawとそのビルド依存物、LittleCMS 2.17について、上流のソースアーカイブそのものを `output/distribution/third-party-source/` に収集しています。現在の一覧・サイズ・SHA-256は同フォルダーの `manifest.json` を参照してください。
- **配布物（exe）の再配布**：上記のソースアーカイブとライセンス文書は、ポータブル版・インストーラーの両方に同梱します。同梱する Qt の DLL・プラグインがすべてソースアーカイブと対応付けられることをビルド時に検査し、使っていない Qt6Pdf は除外しています。根拠と公開時の確認項目は [licenses/DISTRIBUTION-NOTICES.md](../licenses/DISTRIBUTION-NOTICES.md) に記録しています（配布者による確認で、法的見解ではありません）。
- **LittleCMS 2.17**：MIT。**Fluent UI System Icons**：MIT（固定コミットは `assets/icons/fluent/NOTICE.txt` に記載）。その他の依存ライブラリの告知は `licenses/manifest.json` にあります。
- **Camera Profile Studio 自体は [MIT ライセンス](../LICENSE) で公開します。** 依存ライブラリはそれぞれのライセンスのままです。アプリでは **ヘルプ ＞ ライセンス**（**設定 ＞ バージョン情報** からも開けます）に、同梱しているすべての部品とライセンス本文を `licenses/` フォルダーから読み込んで表示します。

## 関連する成果物

- **CCDプロファイルのLUT（タスク01、受入済み）：** `output/01_CCD_LUTs/production_final/` に14本の.cubeがあり、それぞれに付属JSONとチャートPNGが付いています。すべて `_EXPERIMENTAL` 付きです。
  - 技術用12本：ICC 6機種 × 33/65。入力はプロファイルのデバイスRGBなので、JPEGには適用しないでください。
  - Nikon D70sの写真用ルック2本：sRGB → sRGB。LookTableとToneCurveだけから作成しています。

  使う前に `production_final/README_JA.md` と `RESEARCH_AND_RIGHTS_JA.md` を読んでください。派生LUTの権利は未確定です。
- **デジタル中判LUTの調査（タスク02、受入済み）：** `output/02_Medium_Format_Research.md` と `output/02_Medium_Format_Sources.json` に加え、検証済みの補足資料（`output/02_Medium_Format_Supplement.md` / `.json`）があります。補足資料は、2012年の論文や2017年のPhase One公式ガイドなどを対象を絞って確認したものです。RAW/JPEGのサンプルは先頭64 KiBだけを確認しており、対応画像の全体は取得していません。この調査は証拠を分類したもので、カメラを精度順に順位付けするものではありません。Web検索は**網羅的ではなく**、カメラの色再現精度は未検証です。

  配布した `Medium_Format_LUT_Research.zip` では、出典索引の2つのJSONを、配布用（delivery-safe）に加工した版で、通常と同じパスに格納しています。加工には `tasks/research/delivery_safe.py` を使います（説明は `output/delivery-safe/README_JA.md`）。逐語の出典抜粋105件（21,326語）は、省略の記録に置き換えてあり、各抜粋のSHA-256・語数・位置は残しています。URL、HTTP状態、ハッシュ、数値の事実、分析はそのままです。長い引用の原文はローカルにだけ保持し、別に配布することはありません。これは範囲を限った著作権上の配慮であり、法的な権利のクリアランスではありません。

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) | 色空間、LUT形式、エンジンの動作、エラー、パッケージング |
| [docs/LIMITATIONS.md](LIMITATIONS.md) | できないこと・対応しないこと（英日併記） |
| [docs/FINAL_REPORT.md](FINAL_REPORT.md) | 全成果物の状態と証拠 |
| [docs/REQUIREMENTS.md](REQUIREMENTS.md) | 要件・受入台帳 |
| [docs/UI_DESIGN.md](UI_DESIGN.md) | GUIの設計根拠と検証メモ（英語） |
| [docs/API_CONTRACT.md](API_CONTRACT.md) | エンジンとCLIの契約（開発者向け） |
| [docs/IMAGE_LOOK_GUIDE.ja.md](IMAGE_LOOK_GUIDE.ja.md) | Image Look Creator のユーザーガイド（日本語、0.2.5 の内容） |
| [docs/IMAGE_LOOK_API.md](IMAGE_LOOK_API.md) | Image Look Creator のエンジンとCLIの契約（開発者向け、英語） |
| [packaging/README.md](../packaging/README.md) | ビルドとパッケージングの詳細 |
