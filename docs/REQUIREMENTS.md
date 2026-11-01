# 要件・受入台帳

出典: `input/Astra_Prompts/01_CCD_Profile_to_CUBE.md`、`02_Medium_Format_LUT_Research.md`、`03_Camera_Profile_Studio_Development.md`、`04_Windows11_Fluent_UI_UX.md`（4件ともユーザー指定）。
本台帳はプロンプト全文の写しではありません。詳細は原文を正とします。最終報告では要件IDで参照してください。

状態の凡例: `PENDING` = 未着手または証拠なし / `PARTIAL` = 一部のみ証拠あり / `PASS` = 受入確認の証拠あり / `N/A` = 根拠を添えて対象外と判断。
**2026-10-20 更新**：各行の状態欄に、判定と証拠の場所を記載しました。ネイティブビルド、`-SkipCompile` による再パッケージ、スモークテストの結果を反映しました。クリーンなVMでの確認がないため、S3-13は `PARTIAL` です。「実装・テスト済み」は実機JPEGの精度を意味しません（X-05）。

## X. 横断原則（全タスク共通）

| ID | 要件 | 受入確認 | 状態 |
|---|---|---|---|
| X-01 | RAW入力用ICC/DCPの色補正と、撮って出しJPEGのルックを区別する。プロファイルを逆変換・流用しただけのものを「実機JPEG再現」と呼ばない | 各出力・報告に区分（RAW入力変換 / ルック近似）が明記されている | PASS：01のファイル名（`RAW_DEVICE` / `_EXPERIMENTAL`）、capabilityの表示、README/LIMITATIONS |
| X-02 | ソースにない色処理や色調特徴を捏造しない。元データ・加工処理・独自推定を分けて記録する | 出典・処理・推定が分離された記録がある | PASS：01の `production_final/README_JA.md`・`manifest.json`、02の4層の証拠区分 |
| X-03 | 処理できない入力に対してダミーのLUTを生成せず、成功扱いもしない。理由を報告する | 失敗ケースで出力なし＋理由表示を確認 | PASS：CLIの終了コード2（出力なし）、GUIのバッチで失敗項目のファイルなし（`output/gui-validation/manifest_scale*.json`）、D200の逆変換の拒否 |
| X-04 | 色空間・伝達関数・白色点（D50/D65）・色順応・変換方向を明示する | LUT/報告ごとに明記 | PASS：.cubeの `CPS_INPUT`/`CPS_OUTPUT` と付属JSON、01の `technical_input_contract` |
| X-05 | ΔE00などの実機検証がないものを高精度再現と断定しない | 未検証の項目に「未検証 / 精度未確認」の表示 | PASS：`jpeg_accuracy: unverified`、02は精度の順位を付けていない |
| X-06 | 途中段階の成果を全機能完成と宣言しない | 最終報告で要件IDごとに状態と証拠を列挙 | PASS：`docs/FINAL_REPORT.md` と本台帳で、要件IDごとに状態と証拠を記載。PARTIALの項目もそのまま残している |

## 01. CCD実機プロファイル → .cube 生成

| ID | 要件 | 受入確認 | 状態 |
|---|---|---|---|
| L1-01 | 指定9ソース（F828/V3/D70 ICC、D70s DCP、D200 ICC+DCP、DCS760/ProBack645 ICC、E-1 DCP）と Kodak-DCS-Tools `profiles.md` を実際に取得する | 取得ファイル・URL・ハッシュの記録 | PASS：`output/01_CCD_LUTs/sources.json` |
| L1-02 | ICCタグ（A2B0/B2A0、TRC、CLUT、行列）とDCPタグ（ColorMatrix、ForwardMatrix、HueSatMap、LookTable、ToneCurve）のうち、存在するものを解析して利用する | 機種ごとのタグ一覧と利用有無 | PASS：`profile_facts.json`、`production_final/manifest.json` |
| L1-03 | 実機JPEG・RAWの色票データや信頼できる比較資料を調査する（あれば精度向上に使う） | 調査結果と採否の記録 | PASS：`research_retrievals.json`、`RESEARCH_AND_RIGHTS_JA.md`（校正セットは入手できず、不採用の理由を記録） |
| L1-04 | 形式は標準 `.cube`、33³（可能なら65³も）、値域0.0〜1.0、正しいチャンネル順 | バリデータでヘッダー・行数・値域・RGB順を確認 | PASS：`production_final/validation.json`（33³/65³） |
| L1-05 | 入力・出力ともsRGB/Rec.709色域、sRGB伝達関数。HDR/Log向けとは明確に区別する | ヘッダーまたは付属文書に明記 | PASS：写真用はsRGB→sRGB。技術用は入力がデバイスRGBであることを明記（L1-07） |
| L1-06 | 根拠のないグレイン・セピア・ハレーションなどを加えない | 処理内容の記録で確認 | PASS：`production_final/README_JA.md` の処理記録 |
| L1-07 | RAW入力プロファイル変換LUTとJPEGルックLUTを必要に応じて別出力する。前者には入力色空間・現像条件を明記する | 出力一覧と注記 | PASS：技術用12本と写真用2本を別に出力し、入力条件を明記 |
| L1-08 | 8機種の個別ファイル: `Sony_DSC-F828_CCD` `Sony_DSC-V3_CCD` `Nikon_D70_CCD` `Nikon_D70s_CCD` `Nikon_D200_CCD` `Kodak_DCS760_CCD` `Kodak_ProBack645_CCD` `Olympus_E1_KodakCCD`（`.cube`） | 機種ごとに「生成済み」か「生成不可（理由あり）」のどちらか | PASS：8機種すべてについて、生成ファイルまたは省略理由を記録（無印のファイル名は不採用） |
| L1-09 | 実測根拠のないルックはファイル名に `_EXPERIMENTAL` を付ける。情報不足の機種は生成せず理由を報告する | ファイル名と報告の整合 | PASS：`_EXPERIMENTAL` を付与。E-1と技術用DCPは理由を付けて省略 |
| L1-10 | 実際に動作する再生成プログラムを作成し、実行する | 実行ログ・再実行で同一出力 | PASS：`tasks/ccd/generate.py`、`final-run.log`、14本が `production_02` とバイト単位で一致 |
| L1-11 | 色票または共通画像への適用結果を確認する。可能なら実機リファレンスとのΔE00を測定する | 適用画像・ΔE00表（測定不能ならその理由） | PASS：合成チャートへの適用PNG。実機ΔE00は参照データがないため測定不能（理由を記録） |
| L1-12 | 全LUTと再生成スクリプトをZIPにまとめ、機種ごとの根拠と制限を報告する | ZIPの内容一覧と機種別報告 | PASS：機種別の報告に加え、`output/CCD_Profile_LUTs.zip` を作成・検証済み（68エントリ。.cube 14個、チャート14枚、サイドカーのハッシュはすべて一致。同梱のソースZIP 457エントリのCRCは正常、.cubeは本番出力と完全一致、プロファイルとRAWは含まない） |

## 02. デジタル中判LUT調査（調査が主目的。LUTの新規生成は不要）

| ID | 要件 | 受入確認 | 状態 |
|---|---|---|---|
| R2-01 | 対象: Hasselblad（H3D〜H6D、X1D/X1D II、X2D、HNCS、Phocus Standard/Nature）、PENTAX 645D(CCD)/645Z(CMOS)、Phase One（P/P+、IQ1〜IQ4、XF、Capture One）、GFX 50S/50R/100、Leica S、Mamiya/Leaf、Kodak ProBack 645 | 機種ごとの調査結果（該当なしを含む） | PASS：`output/02_Medium_Format_Research.md` のD節（リード受入） |
| R2-02 | 必須リポジトリ: V-Log-Alchemy `Luts/Hasselblad`、songjiun10-collab/Hncs、TingfengLuo/Camera-Profile-for-Fujifilm-Film-Simulation、RawTherapee、darktable | 5件それぞれの確認結果 | PASS：5リポジトリを固定SHAで記録（同報告の「固定した調査範囲」） |
| R2-03 | 検索範囲: GitHub Code Search/Releases/Issues/Discussions/Fork、GitLab、Codeberg、Hasselblad/Phase One/Capture One/Ricoh の公式資料、フォーラム、論文、RAW/JPEG対応サンプル。英・日・中・韓で検索する | 検索した場所とクエリの記録 | PARTIAL：検索は網羅的でない（同報告§F）。検証済みの補足資料 `output/02_Medium_Format_Supplement.md` / `.json`（`supplement_verify.py` がPASS）も、対象を絞った確認にとどまる |
| R2-04 | 証拠評価10項目（実在・アクセス可否、機種対応、実機データ、純正処理の解析か、画像統計による近似か、スクリプト公開、入出力色空間・伝達関数、検証、README と実ファイルの一致、再現可否）を候補ごとに確認する | 候補ごとの評価記録 | PASS：同報告のA〜C、`02_Medium_Format_Sources.json` |
| R2-05 | 区別を守る: V-Log/V-Gamut入力LUTと sRGB直接適用LUT、645Zと K-1 の混在モデル、645Dと645Z、CCDとCMOS、44×33mm級と約53.4×40mm級、公式情報と第三者の主張 | 報告中で区別を明記 | PASS：同報告（V-Log入力とsRGBの区別など） |
| R2-06 | 評価の優先順は、根拠 > 忠実度 > 実用性(.cube/格子/入力色空間) > 再現性 > 信頼性。根拠が不足する場合は順位を付けず「精度未確認」とする | 最終評価で確認 | PASS：根拠不足のため順位を付けず、「精度未確認」とした |
| R2-07 | 成果物A: 既存LUT一覧表（機種、ファイル名、直リンク、形式、解像度、入出力色空間、根拠、検証状況、制限） | 表の全列が埋まっている（不明の場合は「不明」と記載） | PASS：同報告A。配布物 `output/Medium_Format_LUT_Research.zip` は作成・配布済み（出典索引は逐語の抜粋を省略した配布用の版） |
| R2-08 | 成果物B: LUTがない機種向けのICC/DCP・カラーマトリクス等（直リンクと内容） | 一覧 | PASS：同報告B |
| R2-09 | 成果物C: 有力候補の詳細分析（処理、再現できる要素とできない要素、入力条件、問題点） | 候補ごとの節 | PASS：同報告C |
| R2-10 | 成果物D: 機種別・世代別の最終評価 | 評価表 | PASS：同報告D |
| R2-11 | 禁止事項: 「中判固有の色」を前提にしない、被写界深度や光学特性をLUTで再現できると主張しない、コミット日時の新しさを精度の根拠にしない | 報告をレビューして確認 | PASS：同報告（リード確認済み） |

## 03. Camera Profile Studio 開発（Phase 1〜7）

| ID | 要件 | 受入確認 | 状態 |
|---|---|---|---|
| S3-01 | 環境: Python 3.12/3.13、PySide6/Qt6、LittleCMS 2、NumPy/SciPy、colour-science、Pillow/OpenCV、pytest、Nuitka または pyside6-deploy、GitHub Actions | 隔離環境のバージョン記録 | PASS：CPython 3.12.15、固定版の依存関係（`requirements-lock.txt`）、LittleCMS 2.17、Nuitka 2.7.12。ワークフローは用意済みで未実行 |
| S3-02 | 色変換エンジンをGUIから分離し、GUIを経由せず自動テストとCLIから使える。原文に挙がっているモジュール（GUI、ICC、DCP、Color Space、Look、Matching、LUT、Preview、Export、CLI、Packaging）を独立させる | GUIなしで pytest と CLI が動作 | PASS：ヘッドレス239件のテストとCLI（`output/final-validation/`） |
| S3-03 | Ph1: ICC v2/v4（クラス・色空間・PCS・行列・TRC・CLUT）とDCPの解析およびテスト。不正なプロファイル、未対応タグ、特異行列、色空間の不一致を検出する | 正常系・異常系のテスト結果 | PASS：解析・異常系のテスト、`output/engine-audit/closure/REPORT.md` |
| S3-04 | Ph2: LittleCMS 2 による32-bit float RGB変換、Rendering Intent、色順応・白色点、色域外処理の選択。高精度経路に8-bitの量子化を挟まない | LittleCMS参照出力との比較テスト | PASS：float32のLCMS。ネイティブとの差0.0、逆変換の拒否（同closureレポート） |
| S3-05 | Ph2: DCP ColorMatrix/ForwardMatrix、CalibrationIlluminant/WB を処理する。実行できない変換方向は理由を示す | テストと理由メッセージ | PASS：`docs/DCP_IMPLEMENTATION.md`、独立監査テスト |
| S3-06 | Ph3: 33/65（可能なら17）の .cube を出力。SIZE・RGB順・サンプル数が正しく、NaN/Infがない。入出力色空間・伝達関数・色域外の扱いを明示する | LUTバリデータのテスト | PASS：`lut.py` のテスト、01とCLIの出力 |
| S3-07 | Ph3: 17/33/65 の品質比較。直接変換に対するΔE00分布と最大誤差（可能な場合）。チャート、グラデーション、既知の変換を使う | 比較レポート | PASS：`output/lut-quality/quality.json`（直接LCMSとの比較であり、実機との比較ではない） |
| S3-08 | Ph4: Look Extractor（HueSatMap、LookTable、ToneCurve、補間、Encoding、Dual Illuminant補間、行列との合成）。出力は Colors Only / Tone Curve Only / Look Only / Combined | テストと出力4種 | PASS：4モードのテスト、D70sのルックLUT（実機JPEGの精度は未検証） |
| S3-09 | Ph5: Match Studio（RAW/JPEGペア、2台のカメラ、色票、参照画像）。CCM、トーンカーブ、3D LUTフィッティング、正則化、ΔE00、学習用と検証用の分離、位置合わせ。非対応シーンを無条件にピクセル対応させない | テストと検証用ΔE00 | PASS：matchingのテスト、CLIの `match`（`tests/test_cli.py::test_real_cli_matching_holdout`、合成NPZ）、GUIでのCSV・画像ペアの検証用ΔE00（合成データ。実機・OEMの精度は未検証） |
| S3-10 | ファイルごとの状態表示: Technical 対応/非対応、Look 対応/部分/不可、Match 参照あり/なし、JPEG精度 検証済/未検証 | GUIとCLIで表示を確認 | PASS：CLIの `inspect`、GUIのInspector（`manifest_scale*.json`） |
| S3-11 | Ph6 GUIの必須12項目: D&D、ダイアログ、メタデータ、解析一覧、モード、色空間、LUTサイズ、Before/After、強度、エクスポート、バッチ、エラー/ログ。Advancedの分離、ワーカースレッド、Light/Dark、日英切替 | 各項目を実操作で確認 | PASS（ソースから実行）：`output/gui-validation/`、GUIテスト27件、`docs/UI_DESIGN.md` |
| S3-12 | プレビューとエクスポートしたLUTの結果が一致する | 一致テスト | PASS：エンジンとGUIの一致テスト |
| S3-13 | Ph7: `CameraProfileStudio-Setup.exe` と `CameraProfileStudio-Portable.zip`。Python・pip・コンパイラ・Qt SDKなしで動作し、オフラインで基本機能が使える | Pythonのないクリーン環境でのインストール・起動・変換 | PARTIAL：Setup.exe と Portable.zip はビルド済み。開発ホストで `PATH` からPythonを外した状態のスモークテスト（`--help`、ICCのinspect、変換、Look、マッチング、GUI起動、インストールとアンインストール）は、`-SkipCompile` による再パッケージの前後ともPASS（`output/distribution/first-build-results.json`、`repack-results.json`）。クリーンなVMとネットワーク遮断下のオフライン動作は未検証 |
| S3-14 | 同梱物: ソース、lockファイル、ビルドスクリプト、GitHub Actions（Windowsランナー）、README（日本語・英語）、技術仕様と制限、自動テスト | ファイルの存在とCIの実行結果 | PARTIAL：ソース、lock、スクリプト、ワークフロー、README、仕様、テストはある。CIは未実行。6つの配布物はすべて作成・検証済み（`Medium_Format_LUT_Research.zip` は配布済み） |
| S3-15 | ライセンス: 同梱するプロファイルの再配布条件、PySide6・依存ライブラリの義務 | ライセンス一覧 | PARTIAL：`licenses/`、第三者ソース一覧。一般への再配布は未クリア |
| S3-16 | 最終報告: 実装済み機能、未対応事項、テスト結果、生成された実ファイル、ビルド方法 | 報告の各節 | PASS：`docs/FINAL_REPORT.md`（実装済み機能、未対応事項、テスト結果、6つの配布物、ビルド方法） |

## 04. Windows 11 Fluent UI/UX

| ID | 要件 | 受入確認 | 状態 |
|---|---|---|---|
| U4-01 | Python 3.12+、PySide6 Qt Widgets、Qt公式 `windows11` QStyle（使えない場合は標準スタイルへフォールバック）、Fluent UI System Icons、Model/View、Layout。QML や第三者Fluentライブラリは必要性を検証せずに混在させない。独自のデザインシステムを作らない | スタイル選択とフォールバックのテスト | PASS：`windows11` スタイルとフォールバックのテスト、Fluentアイコン |
| U4-02 | 公式ドキュメント6件（Windows App Design、QStyle、Navigation、Fluent 2 Layout、Fluent Icons、Accessibility）を根拠として参照する | 設計記録に参照箇所がある | PASS：`docs/UI_DESIGN.md` §1 |
| U4-03 | 左ナビゲーションのワークスペース: Profile Inspector、Technical Converter、Camera Look Extractor、Camera Match Studio、Batch Processing、Settings。中央・右・下・上の配置、QSplitter | 画面で確認 | PASS：スクリーンショット、`docs/UI_DESIGN.md` §2 |
| U4-04 | デザイン: 標準の余白、情報階層、Light/Dark、システムフォント、一貫したアイコン、フォーカス・ホバー表示。グローバルQSSを多用しない。Mica/Acrylic/アニメーションは必要な場合だけ | レビューとスクリーンショット | PASS：100/150/200%のスクリーンショット。リードが抽出して目視確認し、ソースからの受入とした（87枚すべては確認していない） |
| U4-05 | UX: Inspector はD&D直後に解析結果を表示。Converter の操作順と Advanced の分離。Look の再現可否表示。Match の Before/After・ズーム・パン・分割表示・プレビュー更新。処理中/成功/失敗/キャンセルの区別、失敗時の原因表示 | 実操作で確認 | PASS：`manifest_scale*.json` の操作記録 |
| U4-06 | アクセシビリティ: キーボードだけで基本操作ができる、フォーカスが明確 | キーボード操作の記録 | PARTIAL：キーボード操作のテストのみ。ナレーターでの確認はしていない |
| U4-07 | 表示検証: Windows 11 で Light/Dark、100/150/200% スケーリング、リサイズ、日英の崩れ、長いパス、D&D、大きな画像での応答性、フリーズなし、エラー通知の整合 | 検証マトリクス | PARTIAL：DPIは `QT_SCALE_FACTOR` によるエミュレーション、D&Dは模擬イベント、ハイコントラスト未確認 |
| U4-08 | 主要画面の実スクリーンショットを取得して検証する。起動しただけでGUI完成と判定しない | スクリーンショットのファイルと所見 | PASS：各スケール29枚のスクリーンショット（`output/gui-validation/screens/`）、`output/final-validation/REPORT.md`、リードによる抽出の目視確認 |
