# 最終報告 / Final report

**English summary (2026-10-20).**

| Area | Status |
|---|---|
| Task 01 (CCD LUTs) | Accepted: 14 cubes with sidecars and charts in `output/01_CCD_LUTs/production_final/`, 7 tests pass. `output/CCD_Profile_LUTs.zip` built and verified (68 entries) |
| Task 02 (research) | Accepted as an evidence classification (no accuracy ranking, non-exhaustive search). `output/Medium_Format_LUT_Research.zip` is built and delivered, with delivery-safe source indices (quoted passages omitted) |
| Engine, CLI and GUI | Frozen. Final combined suite: **281 passed, 0 failed, 0 skipped** (239 headless + 27 GUI + 7 CCD + 8 independent audit). The GUI was accepted from source on Windows 11 at 100/150/200% (emulated DPR) |
| Windows artifacts | **All six artifacts are built and checked.** Setup and Portable passed a bounded smoke test on the build machine with Python removed from `PATH`, before and after a `-SkipCompile` repack (EXE and source fingerprint unchanged). Not a clean VM, not network-isolated, unsigned. S3-13 is PARTIAL. Not every requirement is complete (see section 4) |
| Camera JPEG ΔE00 | **Unverified for every camera.** Implemented features and passing tests do not establish camera accuracy |

Requirement IDs refer to [REQUIREMENTS.md](REQUIREMENTS.md).

---

状態：2026-10-20時点。ネイティブビルド、`-SkipCompile` による再パッケージ、スモークテストの結果を反映しています。凡例は [REQUIREMENTS.md](REQUIREMENTS.md) と同じです（`PASS` / `PARTIAL` / `PENDING` / `N/A`）。

**注意：「実装・テスト済み」と「実機の精度」は別の事柄です。** 全機種で、撮って出しJPEGとのΔE00は未検証です。タスク02のWeb検索は網羅的ではありません。

## 1. 成果物一覧

| 成果物 | パス | 状態 |
|---|---|---|
| 01 CCD LUT | `output/01_CCD_LUTs/production_final/`（.cube 14本、付属JSON、チャートPNG 14枚、`manifest.json`、`validation.json`、`README_JA.md`）、実行ログ `output/01_CCD_LUTs/final-run.log` | 受入済み |
| 01 根拠・権利 | `output/01_CCD_LUTs/sources.json`、`profile_facts.json`、`RESEARCH_AND_RIGHTS_JA.md` ほか | あり |
| 02 中判LUT調査 | `output/02_Medium_Format_Research.md`、`output/02_Medium_Format_Sources.json` | 受入済み（証拠分類として） |
| 02 補足資料 | `output/02_Medium_Format_Supplement.md`、`output/02_Medium_Format_Supplement.json` | 受入済み（`python -B tasks/research/supplement_verify.py` がPASS） |
| 03 ソース | `src/camera_profile_studio/`（エンジン・CLI・GUIとも凍結済み） | あり |
| 03 lockファイル | `requirements-lock.txt`（`rawpy==0.25.1` を含む） | あり |
| 03 ビルドスクリプト | `scripts/build-lcms.ps1`、`build-windows.ps1`、`build-bootstrap.ps1`、`verify_distribution.ps1`、`packaging/` | あり |
| 03 GitHub Actions | `.github/workflows/windows-distribution.yml` | あり。未実行 |
| 03 文書 | `README.md`、`README.ja.md`、`docs/TECHNICAL_SPEC.md`、`docs/LIMITATIONS.md`、`docs/UI_DESIGN.md`、本報告 | あり |
| 03 自動テスト | `tests/`、`tasks/ccd/test_*.py`、`output/engine-audit/test_independent_audit.py` | 281 passed（§5） |
| 04 スクリーンショット | `output/gui-validation/screens/scale{100,150,200}/`（各29枚） | あり |
| 検証の証拠 | `output/final-validation/`、`output/engine-audit/`、`output/lut-quality/`、`output/raw-validation/`、`output/gui-validation/` | あり |
| 第三者ソース | `output/distribution/third-party-source/`（一覧は `manifest.json`） | 収集済み。再配布は未クリア |

**配布物**（`output/`）：

| ファイル | 状態 |
|---|---|
| `Medium_Format_LUT_Research.zip` | **作成・配布済み**（28エントリ、199,098バイト。ZIPのCRCと、同梱マニフェストの全SHA-256を検証済み）。§3を参照 |
| `CameraProfileStudio-Setup.exe` | ビルド済み（未署名）。スモークテスト合格（§5） |
| `CameraProfileStudio-Portable.zip` | ビルド済み。ZIPのCRC・アイコン・ソース対応・入力除外の確認に合格。スモークテスト合格（§5） |
| `CameraProfileStudio-Source.zip` | 作成済み（フィルター済み）。ペイロード内とポータブルZIP内のコピーとバイト単位で一致 |
| `CCD_Profile_LUTs.zip` | **作成・検証済み**（68エントリ。.cube 14個、チャート14枚、サイドカーのハッシュはすべて一致。同梱のソースZIPのCRCは正常、.cubeは本番出力と完全一致、プロファイルとRAWは含まない） |
| `Validation_Evidence.zip` | **作成・検証済み**（スクリーンショット87枚とマニフェスト3件、281件のテストの証拠、CLI・DCP・SciPy・ネイティブGUI・インストール/アンインストールのスモーク証跡を含む。禁止した入力は含まない。正確な内容一覧は同梱の `BUNDLE_MANIFEST.json`、検証記録は `tasks/delivery/logs/verify_validation.txt`、`validation_zip_checks.txt`） |

各ZIPのSHA-256は本報告に書きません。本報告自体がZIPに含まれるため、ハッシュが循環してしまうからです。最終的なサイズとSHA-256は、配布物と並べて置く `DELIVERY_SHA256SUMS.txt` と `DELIVERY_MANIFEST.json` が正式な値です。テストに合格したネイティブコード（EXE）は再パッケージの前後で同一で、変わるのは文書などのパッケージのメタデータだけです。

## 2. タスク01：CCDプロファイル → .cube

9プロファイルを固定コミット・SHA256で照合しました（`sources.json`）。

`production_final` は、凍結したエンジンを使って生成しました。実行中にエンジンのハッシュは変わっていません（`manifest.json` の `shared_module_sha256`）。また、14本はすべて `production_02` の同名ファイルとバイト単位で一致しており、再実行で同じ出力になることを確認しています。

| 機種 | 技術用LUT（ICCデバイスRGB → sRGB） | 写真用ルック |
|---|---|---|
| Sony DSC-F828 | `Sony_DSC-F828_RAW_DEVICE_TO_sRGB_EXPERIMENTAL_33/65.cube`（RGBE 4色の4行目はICCで省略されている） | 省略 |
| Sony DSC-V3 | `Sony_DSC-V3_RAW_DEVICE_TO_sRGB_EXPERIMENTAL_33/65.cube` | 省略 |
| Nikon D70 | `Nikon_D70_RAW_DEVICE_TO_sRGB_EXPERIMENTAL_33/65.cube` | 省略 |
| Nikon D70s | なし（実撮影の `camera_neutral` がないため、技術用DCPは生成しない） | `Nikon_D70s_CCD_EXPERIMENTAL_33/65.cube`（LookTable＋ToneCurveのみ） |
| Nikon D200 | `Nikon_D200_RAW_DEVICE_TO_sRGB_EXPERIMENTAL_33/65.cube`（A2B0のみのため、perceptual intentを使用） | 省略 |
| Kodak DCS760 | `Kodak_DCS760_RAW_DEVICE_TO_sRGB_EXPERIMENTAL_33/65.cube` | 省略 |
| Kodak ProBack645 | `Kodak_ProBack645_RAW_DEVICE_TO_sRGB_EXPERIMENTAL_33/65.cube` | 省略 |
| Olympus E-1 | なし | 省略（校正用のHueSatMapしかない） |

要求された無印のファイル名は使っていません。名前を変えた理由は、実測の根拠がないこと（`_EXPERIMENTAL`）と、入力がデバイスRGBであること（`RAW_DEVICE`）を示すためです。技術用LUTは、通常のsRGB写真用ではありません。

実機JPEGとのΔE00は、参照データ（RAW/JPEGの対応画像、測色済みの色票）がないため測定できません。合成チャートへの適用画像と、直接演算とLUT補間の差は `validation.json` にあります。派生LUTの再配布権は未確定です。

## 3. タスク02：デジタル中判LUT調査

報告書のA〜Dを、**証拠分類として**受入れました。

- 実際に解析した資産：.cube 8本（全行）、DCP 26個、ICC 3個。
- 必須の5リポジトリを固定SHAで記録し、92ファイルをGitのblobと照合。
- `python -B tasks/research/verify_task02.py` がPASS。

**補足資料（受入済み）**：`output/02_Medium_Format_Supplement.md` / `.json`。対象を絞った確認の記録です。

- HTTP記録21件、Git blobの照合3件。
- 2012年の論文（5ページ）は全文を読了。
- 2017年のPhase One公式ガイド（50ページ）は取得し、関係する節だけを読んだ。
- 指定のパッチは404だったことをそのまま記録。
- RAW/JPEGのサンプル5件は先頭64 KiBだけを確認し、対応画像の全体は取得していない。

補足資料によって、主報告の資産数（.cube 8、DCP 26、ICC 3）は変わりません。検索範囲が網羅的になったわけでも、実機の精度が検証されたわけでもありません。

報告書は精度の順位を付けていません。今回の証拠からは、実証された順位を決められないためです。Web検索は**網羅的ではありません**（報告書§F）。各候補の実機の色再現精度は未検証です。却下された以前のドラフトの内容は使っていません。

**配布物（作成・配布済み）**：`output/Medium_Format_LUT_Research.zip` には、主報告、補足資料、検証・生成用のスクリプト、同梱のマニフェストが入っています。

出典索引の2つのJSON（`output/02_Medium_Format_Sources.json`、`output/02_Medium_Format_Supplement.json`）は、著作権への配慮から加工した**配布用の版**を、通常と同じパスに格納しています。

- 生成元：`tasks/research/delivery_safe.py`。出力は `output/delivery-safe/`（`manifest.json`、`README_JA.md`）です。
- 逐語の抜粋105件（計21,326語）は、本文を省略の記録に置き換えました。記録には、原文のSHA-256、語数、位置（JSONポインターと行・ページ情報）が残っています。
- URL、HTTP状態、元資料のハッシュ、数値の事実、独自の分析はそのままです。残した項目が元のJSONと一致することは、機械的に確認しています。
- 長い引用を含む元のJSONと取得した原文は、検証用としてローカルにだけ保持し、別に提供することはありません。
- これは範囲を限った機械的な監査であり、法的な権利のクリアランスではありません。

## 4. 要件ごとの状態（概要）

詳細と証拠は [REQUIREMENTS.md](REQUIREMENTS.md) の台帳にあります。

| 区分 | PASS | PARTIAL | PENDING |
|---|---|---|---|
| X（横断原則） | X-01〜X-06 | — | — |
| 01 | L1-01〜L1-12 | — | — |
| 02 | R2-01、R2-02、R2-04〜R2-11 | R2-03（検索は網羅的でない） | — |
| 03 | S3-01〜S3-12、S3-16 | S3-13（`PATH` からPythonを外したスモークはPASS。クリーンなVM・オフライン遮断は未検証）、S3-14（CI未実行）、S3-15（再配布は未クリア） | — |
| 04 | U4-01〜U4-05、U4-08 | U4-06（ナレーター未確認）、U4-07（DPIはエミュレーション、D&Dは模擬イベント） | — |

## 5. テストと検証の証拠

| 項目 | 結果 | 出典 |
|---|---|---|
| **最終の一括テスト**（凍結後のソース、実RAWあり） | **281 passed、0 failed、0 skipped**（ヘッドレス239＋GUI 27＋CCD 7＋独立監査8）。警告はmatplotlib未導入の1件のみ | `output/final-validation/pytest-combined.stdout.txt`、`REPORT.md` |
| ソースのハッシュの安定性 | テスト前後で `src/`・`tests/` の.pyファイル54個に差分なし | `output/final-validation/hashes-*.json` |
| GUIのネイティブ実行 | 100/150/200%（エミュレーションしたDPR）で各29枚、操作28件、表示エラー0件。150%と200%は凍結後に撮り直し、終了コード0。リードが抽出して目視確認（87枚すべてではない）。ネイティブの日本語画面では文字が正しく表示されている。ソースからの受入は完了 | `output/gui-validation/manifest_scale*.json`、`output/final-validation/gui-capture-*` |
| GUIで実RAWを読み込む確認（任意） | PASS：D70s NEF（2048×1357）のBefore画像を0.66秒で読み込み、エラーなし。範囲はデコードだけで、Afterの推定もOEMの確認もしていない。オフスクリーンの画像では日本語が豆腐表示になるため、日本語UIの証拠には使わない | `output/final-validation/probe_result.json` |
| ICC不具合の修正確認 | 元の再現手順が `unsupported_direction` で拒否され、供給ICC 6個はネイティブLCMSとの差0.0 | `output/engine-audit/closure/REPORT.md` |
| LUT補間品質 | ΔE00平均/最大：17 = 0.050976/2.045431、33 = 0.012305/0.867180、65 = 0.003430/0.260683（直接LCMSとの比較であり、実機との比較ではない） | `output/lut-quality/quality.json` |
| 実RAWデコーダ | COMPLETE | `output/raw-validation/validation.log`、`README.md` |
| CLI | `--help`、README例1・例2、上書き拒否、強度の不整合をそれぞれ確認。`match` は `tests/test_cli.py::test_real_cli_matching_holdout`（281件に含まれる）で確認。合成NPZを使い、検証用データで誤差が改善し、学習用と検証用は重ならない。OEMの精度の確認ではない | `output/final-validation/cli-help.stdout.txt`、`pytest-combined.stdout.txt`、本文書の作成時の実行 |
| 配布物のスモークテスト | **PASS（範囲限定）**：最初のビルドと、`-SkipCompile` による再パッケージ後（ソースの指紋とEXEは不変）の両方で実施。ビルドマシンで `PATH` からPythonを外して実行。`--help`、生成ICCのinspect、sRGB→リニアsRGB（17³、4,913行、最大誤差2.67e-8）、D70s Look Only（17³、有限値・非恒等）、SciPy `ccm_tone_lut` マッチング（合成データ、学習192・検証64で重複なし、検証用の平均ΔE00 4.69993→0.06843。OEMの精度ではない）、オフスクリーンGUI 8秒、ネイティブウィンドウでUI AutomationによるJobs・Logタブの選択、AppIdの事前確認で既存登録なし、インストール・アンインストールは終了コード0で登録も削除。インベントリ：ネイティブバイナリ178個、Qtの対応付け18件、SVGアイコン28個とライセンス表示、カメラ入力なし。ビルドのためのエンジン・GUIコードの変更なし | `output/distribution/first-build-results.json`、`repack-results.json`、`smoke-results.json`、`final-smoke/` |
| パッケージ版の画面の目視確認 | PASS（1画面のみ）：リードが `native-app-window.png` を確認（再パッケージ後の画面も改めて確認）。ダークテーマ・英語のProfile Inspectorが1つ、文字とアイコンは読め、エラー表示なし。パッケージ版の日本語UI、その他の操作、DPI、RAWは対象外。ソースからのGUIの証拠とは別扱い | `output/distribution/final-smoke/native-app-window.png`、`visual-review.txt` |
| 実機JPEGとのΔE00 | **未検証**（全機種） | 独立した参照データがない |

## 6. ビルド・再現手順

リポジトリのルートでPowerShellから実行します。

```powershell
python -m venv .venv                      # CPython 3.12 x64
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
powershell -NoProfile -File scripts/build-lcms.ps1      # VS 2022のC++ x64ツールが必要
$env:CPS_LCMS2_DLL = (Resolve-Path .tools\packaging\lcms-runtime\lcms2.dll).Path
.venv\Scripts\python.exe tasks/ccd/acquire.py           # GUIテスト用の固定プロファイル（ネットワークを使用）
$env:QT_QPA_PLATFORM = 'offscreen'; .venv\Scripts\python.exe -m pytest
python -B tasks/research/verify_task02.py
python -B tasks/research/supplement_verify.py
powershell -NoProfile -File scripts/build-windows.ps1
powershell -NoProfile -File scripts/verify_distribution.ps1
```

281件の一括テストに使った正確なコマンドは、`output/final-validation/pytest-combined.cmd.txt` にあります。

## 7. 残る課題

1. 配布物の未確認事項：クリーンなVM、ネットワーク遮断、パッケージ版GUIでのRAW・日本語UI・DPI、コード署名。
2. GitHub Actionsの実行。
3. 一般への再配布：配布物とソースの対応付け・出所の確認、書面によるソース提供の申し出の有無、プロジェクト自体のライセンス、派生LUTの権利。法的な保証はしない。
4. GUIの未確認事項：ナレーター、ハイコントラスト、Windowsの実際の表示倍率、物理的なドラッグ＆ドロップ、クリーンなVM、ネットワークを遮断した環境。
5. 実機JPEGとのΔE00：独立した参照データがない限り未検証のまま。
6. 02の検索範囲の拡大（任意）。

## 8. 最終パッケージの手順

文書だけを更新するときは、同じコンパイル済みEXEを再利用し（`-SkipCompile`）、公開前にスモークテスト全体をやり直します。01と検証の証拠のZIPも、同じ文書で作り直します。これは通常の最終パッケージ工程です。最終的なサイズとSHA-256は `DELIVERY_SHA256SUMS.txt` と `DELIVERY_MANIFEST.json` を正とします。
