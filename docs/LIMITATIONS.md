# Limitations / 制限事項

Version 0.1.0 (pre-release). The English and Japanese lists use the same
numbering, so item N means the same thing in both. Items marked **PENDING** are
current gaps that may close once evidence exists. Unmarked items are limits
by design.

英語版と日本語版は同じ番号で対応しています。**PENDING（未確認）** は証拠が揃えば解消しうる現時点の不足で、印のない項目は設計上の制限です。

---

## English

### Colour and LUT scope

1. **Camera JPEG reproduction is unverified.** No independent camera JPEG,
   paired RAW/JPEG set or measured chart reference has been used to measure
   ΔE00 against a real camera. Every look and match is a profile-derived or
   sample-fitted approximation. `jpeg_accuracy` stays `unverified` unless real
   evidence is supplied.
2. **Technical LUTs are not photo looks.** LUTs whose input is ICC device RGB or
   DCP camera-linear RGB are only valid for data in that domain. They must not be
   applied to JPEG/PNG/sRGB images, and the preview refuses them.
3. **DCP technical input must already be prepared.** It must be black-subtracted,
   demosaiced, normalized camera-linear RGB with white balance applied. The
   application does not infer a sensor pipeline, CFA layout, linearization,
   black level or white balance. Per-camera calibration tags
   (CameraCalibration/AnalogBalance) are rejected rather than guessed.
4. **White balance is never estimated.** DCP rendering needs an explicit camera
   neutral. Dual-illuminant profiles need an explicit temperature in kelvin.
5. **Profiles without look tags yield no look.** Matrix-only profiles contain no
   camera JPEG-look information, and no default Adobe tone or look is
   substituted. ICC profiles always report the look as unsupported.
6. **sRGB means the sRGB transfer function.** It is not the Rec.709/BT.1886 video
   transfer. A video timeline that uses Rec.709 gamma will not match the LUT's
   assumption.
7. **SDR only.** There is no HDR, Log/V-Log, wide-gamut working-space or
   floating-point HDR image support. LUT domain and values are 0–1. Cubes with
   shapers, 1D sections or non-unit domains are rejected.
8. **Fixed grid sizes.** Only 17, 33 and 65. Interpolation is tetrahedral (up to
   0.2.4 it was trilinear). On one analytic test profile, the maximum ΔE00 against
   the direct transform was 2.05 (17), 0.87 (33) and 0.26 (65). These figures were
   measured with trilinear interpolation and have not been re-measured with
   tetrahedral interpolation. This is interpolation error, not camera accuracy,
   and other profiles can differ, especially in dark linear-input regions.
9. **Strength is baked once.** It is allowed only when the input and output
   spaces are equal. The exported file does not contain the full-strength
   version.
10. **Profile inversion is not inferred.** An ICC direction the profile does not
    provide (for example no B2A or matrix/TRC inverse) is reported as
    unsupported. Non-invertible sampled curves (constant, non-monotonic or flat)
    are refused for the output direction. CMYK/Lab device profiles and device
    links are rejected.
11. **Some DCP features are refused.** Unknown processing-affecting tags,
    unsupported table encodings, nonzero BaselineExposureOffset, multiple IFDs
    or embedded DNG images. Bitwise equivalence with the Adobe DNG SDK is not
    claimed.

### Matching

12. **Correspondence is required.** Matching uses measured chart patches,
    user-selected corresponding patches, or a same-scene pair that the user
    confirms and then inspects after registration. Arbitrary reference photos
    are not supported for supervised matching.
13. **Holdout ΔE00 is not camera accuracy.** A single-scene or single-chart
    holdout does not establish accuracy across scenes. Lighting, metamerism,
    clipping, local JPEG processing and sparse chart gamut all limit
    generalization.
14. **RAW development is fixed.** LibRaw/rawpy AHD, 16-bit, sRGB, gamma
    (2.4, 12.92), camera or explicit WB, no auto-brightness. It is not the
    manufacturer's JPEG pipeline and not DCP camera-linear input. rawpy is part
    of the full pinned requirements and the planned standalone build. Only a
    minimal developer install without it refuses RAW files.

### Images

15. Accepted images are JPEG, PNG and single-page 8/16-bit RGB/RGBA TIFF.
    CMYK, Lab, floating-point/HDR, multipage TIFF and malformed embedded ICC are
    rejected. Untagged images are assumed to be sRGB, with a warning.

### Application, packaging and platform

16. **The packaged build is checked only on the build machine.** Setup.exe
    and Portable.zip passed a smoke test with Python removed from `PATH`, before
    and after the `-SkipCompile` repack (`output/distribution/repack-results.json`).
    This was not a clean VM and not network-isolated, so a Python-free clean
    install is not proven.
17. **GUI validation limits.** The GUI was tested from source on Windows 11.
    Known limits:
    - Engine reasons, warnings and diagnostics stay in English in the Japanese
      UI.
    - 100/150/200% scaling was emulated with `QT_SCALE_FACTOR` on a 225% host.
      The Windows display setting was not changed.
    - Screenshots are client-area widget grabs without the title bar.
    - Drag & drop was tested with simulated drop events, not a physical drag.
    - No Narrator (screen reader), high-contrast, clean-VM or
      network-isolation validation was done.
    - Match workflow fixtures are synthetic.
    - Real RAW was checked through the GUI only with a single decode probe
      (Nikon D70s), not across RAW formats.
    - Around 760 px wide, the right settings panel needs horizontal scrolling
      or splitter adjustment. The minimum usable window is about 690×680
      logical px.
18. **CLI `match` has synthetic test coverage only.**
    `tests/test_cli.py::test_real_cli_matching_holdout` runs the handler on
    synthetic NPZ samples, with a held-out improvement and disjoint
    training/validation indices. This says nothing about camera or OEM
    accuracy. The packaged smoke test also ran a SciPy match on synthetic data.
19. **The distribution smoke test is limited.** For its scope, see
    `packaging/README.md`. It is not a clean VM, not network-isolated and not a
    visual-acceptance check of the packaged app. In the packaged GUI, one native
    window was reviewed and only the Jobs and Log tabs were selected. Packaged
    Japanese UI, other workflows, DPI settings and RAW were not exercised. The
    optional Matplotlib API is not in the build.
20. **Unsigned.** Windows SmartScreen may warn. Byte-for-byte reproducible
    builds are not claimed.
21. **Windows x64 only** for the packaged application. The engine avoids Qt and
    loads LittleCMS through a platform lookup, but macOS/Linux are not built or
    tested.
22. **Native calls cannot be interrupted mid-call.** Cancellation takes effect
    at the next checkpoint. If you cancel after the files are committed, you get
    the actual success and the file paths. A genuine partial-publication error
    is still reported as a failure, even when cancelled.

### Licensing and rights

23. **No camera profiles are bundled.** The sample profiles used by some tests
    are not in the repository (`input/ccd_profiles/README.md`,
    `packaging/testing/acquire_profiles.py`). They come from GPL-3.0 projects and
    their per-profile terms are not established, so do not redistribute them or
    LUTs derived from them without checking those terms.
24. **Binary redistribution relies on the publisher's review.** Qt/PySide6 are
    LGPL-3.0-only and LibRaw is LGPL-2.1. They ship as separate replaceable DLLs,
    with their license texts and the exact upstream source archives inside every
    payload; the build fails on an unmapped Qt binary. The basis is recorded in
    `licenses/DISTRIBUTION-NOTICES.md`; it is the publisher's assessment, not a
    legal opinion.
25. **The project's own licence is MIT** (`LICENSE`).

### Research

26. **The task 02 web search was not exhaustive.** Its coverage limits (for
    example, authenticated code search, Discussions, paper full texts and
    multilingual result pages) are listed in section F of
    `output/02_Medium_Format_Research.md`. The verified supplement
    (`output/02_Medium_Format_Supplement.md`) adds targeted checks only. Its
    RAW/JPEG samples were checked by their first 64 KiB, not as full pairs. No
    candidate's camera accuracy was independently measured.

### Image Look Creator (0.2.5)

27. **An image look is a statistical match, not a pixel match.** The look is the
    difference between Oklab colour statistics (lightness quantiles, colour cast per
    lightness band, chroma per lightness band and per hue sector) of the reference
    images and those of the base. No pixel correspondence is used, so hue rotation,
    local edits, masks, subject-dependent grading, noise and grain are not
    reproduced, and the content of the references (for example a sunset) can be
    mistaken for the look. The estimates are bounded (colour cast at most ±0.08 in
    Oklab a/b, chroma gain 0.5–2 per lightness band and 0.6–1.6 per hue sector), so
    strong looks come out weaker. No ΔE or accuracy figure exists; "hue coverage" is
    coverage, not accuracy.
28. **The typical-photo baseline is an average.** Without base images, the base is a
    built-in average of 45 varied finished photographs (warning
    `typical_photo_baseline`). A photo whose statistics differ strongly from that
    average (night scenes, near-monochrome photos, strong colour themes) receives
    the difference to the average as part of the look. Base images (the user's own
    photos the look will be applied to) remove this error but do not remove the
    limits of item 27. The accuracy of either baseline against real edited photos
    has not been verified.
29. **Greys are not protected.** The colour cast is added per lightness band, so
    neutral greys take on the difference between the reference's cast and the
    base's cast. The setting `neutral_protection` of 0.2.0–0.2.4 was removed in
    0.2.5. Base images are not checked for conflicts; only the reference images
    are compared with each other.

---

## 日本語

### 色とLUTの範囲

1. **実機の撮って出しJPEGの再現精度は未検証です。** 独立したカメラJPEG、RAW/JPEGの対応画像、実測した色票リファレンスを使った実機とのΔE00測定は行っていません。ルックとマッチはすべて、プロファイル由来またはサンプルからの推定による近似です。実際の証拠が示されない限り、`jpeg_accuracy` は `unverified` のままです。
2. **技術変換LUTは写真用のルックではありません。** 入力がICCのデバイスRGBやDCPのカメラリニアRGBであるLUTは、その色空間のデータにだけ有効です。JPEG/PNG/sRGB画像には適用しないでください。プレビューもこれらを受け付けません。
3. **DCPの技術変換の入力は事前に準備されている必要があります。** 黒レベル減算・デモザイク・正規化・ホワイトバランス適用済みのカメラリニアRGBが前提です。センサー処理、CFA配列、リニア化、黒レベル、ホワイトバランスは推定しません。個体別の校正タグ（CameraCalibration/AnalogBalance）は推測で扱わず、拒否します。
4. **ホワイトバランスは推定しません。** DCPのレンダリングにはカメラのニュートラル値の明示が必要です。2光源プロファイルではケルビン単位の色温度も明示する必要があります。
5. **ルック用のタグがないプロファイルからはルックを作りません。** 行列のみのプロファイルには撮って出しJPEGのルックの情報がありません。Adobeの既定のトーンやルックで代用することもありません。ICCプロファイルのルックは常に非対応と表示します。
6. **sRGBはsRGB伝達関数を意味します。** Rec.709/BT.1886の映像用伝達関数ではありません。Rec.709ガンマの映像タイムラインは、LUTの前提と一致しません。
7. **SDRのみです。** HDR、Log/V-Log、広色域の作業色空間、浮動小数点のHDR画像には対応しません。LUTの値域と値は0〜1です。シェーパーや1D部を含む.cube、値域が0〜1以外の.cubeは拒否します。
8. **格子サイズは固定です。** 17・33・65のみで、補間は四面体補間です（0.2.4まではトリリニア補間でした）。解析的なテスト用プロファイル1つでは、直接変換に対するΔE00の最大値が2.05（17）、0.87（33）、0.26（65）でした。この数値はトリリニア補間で測定したもので、四面体補間では測り直していません。これは補間誤差であり、実機の精度ではありません。他のプロファイル、特にリニア入力の暗部では結果が異なります。
9. **強度は一度だけ焼き込みます。** 入力と出力の色空間が同じ場合に限り使えます。書き出したファイルに強度100%版は含まれません。
10. **プロファイルの逆変換は推測しません。** プロファイルが持たない方向（B2Aや行列/TRCの逆変換がない場合など）は非対応と表示します。逆変換できないサンプル曲線（定数、単調でないもの、平坦な区間を含むもの）は、出力方向では拒否します。CMYK/Labのデバイスプロファイルとデバイスリンクは拒否します。
11. **一部のDCP機能は拒否します。** 処理に影響しうる未知のタグ、非対応のテーブルEncoding、0以外のBaselineExposureOffset、複数IFD、DNG画像の埋め込みです。Adobe DNG SDKとのビット単位の一致は主張しません。

### マッチング

12. **対応関係が必要です。** 使えるのは、実測した色票パッチ、ユーザーが指定した対応パッチ、またはユーザーが同一シーンと確認したうえで位置合わせ結果を目視確認した画像ペアです。任意の参考写真による教師ありマッチングには対応しません。
13. **検証用データのΔE00は実機精度ではありません。** 1シーンや1枚の色票での検証では、シーンをまたいだ精度は確かめられません。照明、メタメリズム、クリッピング、JPEGの局所処理、色票の色域の狭さが汎化を制限します。
14. **RAW現像のレシピは固定です。** LibRaw/rawpy AHD、16-bit、sRGB、ガンマ(2.4, 12.92)、カメラWBまたは明示WB、自動明るさ補正なし。メーカーのJPEG処理でも、DCP用のカメラリニア入力でもありません。rawpyは固定依存関係の完全なセットと、予定しているスタンドアロン版に含まれます。rawpyを省いた最小構成の開発環境では、RAWファイルを拒否します。

### 画像

15. 読み込めるのは、JPEG、PNG、単一ページの8/16-bit RGB/RGBA TIFFです。CMYK、Lab、浮動小数点/HDR、複数ページのTIFF、不正な埋め込みICCは拒否します。ICCのない画像は警告付きでsRGBとして扱います。

### アプリケーション・配布・プラットフォーム

16. **パッケージ版の確認は、ビルドマシン上だけです。** Setup.exe と Portable.zip は、`PATH` からPythonを外した状態で、`-SkipCompile` による再パッケージの前後ともスモークテストに合格しました（`output/distribution/repack-results.json`）。クリーンなVMでもネットワーク遮断下でもないため、Pythonのないクリーンな環境へのインストールは実証されていません。
17. **GUIの検証には次の限界があります。** GUIはWindows 11上でソースから実行してテストしました。
    - エンジンが返す理由・警告・診断情報は、日本語UIでも英語のままです。
    - 100/150/200%のスケーリングは、225%設定のホスト上で `QT_SCALE_FACTOR` を使ってエミュレートしました。Windowsの表示設定は変えていません。
    - スクリーンショットはクライアント領域の取得で、タイトルバーは含みません。
    - ドラッグ＆ドロップは、ドロップイベントを模擬的に送って確認したもので、実際のマウス操作ではありません。
    - ナレーター（スクリーンリーダー）、ハイコントラスト、クリーンなVM、ネットワークを遮断した環境での検証は行っていません。
    - Matchの操作確認に使ったデータは合成したものです。
    - GUIでの実RAWの確認は、Nikon D70sのデコードプローブ1件だけで、他のRAW形式は確認していません。
    - 幅が760px程度のウィンドウでは、右の設定パネルを横にスクロールするか、スプリッターで調整する必要があります。使える最小サイズは論理ピクセルで約690×680です。
18. **CLIの `match` は、合成データでのテストだけです。** `tests/test_cli.py::test_real_cli_matching_holdout` が合成のNPZサンプルで処理を実行し、検証用データで誤差が改善することと、学習用と検証用のインデックスが重ならないことを確かめています。実機やOEMの精度については何も示しません。パッケージ版のスモークテストでも、合成データでSciPyのマッチングを実行しました。
19. **配布物のスモークテストの範囲は限定的です。** 範囲は `packaging/README.md` を参照してください。クリーンなVMでの試験でも、ネットワークを遮断した試験でも、パッケージ版の画面の受入確認でもありません。パッケージ版のGUIでは、ネイティブのウィンドウ1つを目視確認し、Jobs・Logタブを選択しただけです。パッケージ版の日本語UI、その他の操作、DPI設定、RAWは試していません。任意のMatplotlib APIはビルドに含まれていません。
20. **未署名です。** Windows SmartScreenの警告が出ることがあります。バイト単位で再現可能なビルドとは主張しません。
21. **パッケージ版はWindows x64のみです。** エンジンはQtに依存せず、LittleCMSをプラットフォームの探索で読み込みますが、macOS/Linuxではビルドもテストもしていません。
22. **ネイティブ処理は呼び出しの途中では中断できません。** キャンセルは次のチェックポイントで反映されます。ファイルの書き込みが確定した後にキャンセルした場合は、実際の結果どおり成功として、ファイルのパスが返ります。本当に部分的な書き出しで失敗した場合は、キャンセルしていても失敗として報告します。

### ライセンスと権利

23. **カメラプロファイルは同梱していません。** 一部のテストが使うサンプルプロファイルはリポジトリに含めていません（`input/ccd_profiles/README.md`、`packaging/testing/acquire_profiles.py`）。GPL-3.0 のプロジェクト由来で、プロファイルごとの条件は確定していないため、条件を確認するまで、プロファイルとそれから作った LUT は再配布しないでください。
24. **バイナリの再配布は、配布者の確認に基づきます。** Qt/PySide6 は LGPL-3.0-only、LibRaw は LGPL-2.1 です。差し替え可能な別 DLL のまま配布し、ライセンス文書と上流のソースアーカイブを配布物に同梱しています。対応付けできない Qt バイナリがあるとビルドが止まります。根拠は `licenses/DISTRIBUTION-NOTICES.md` に記録しています（配布者の判断であり、法的見解ではありません）。
25. **プロジェクト自体のライセンスは MIT です**（`LICENSE`）。

### 調査

26. **タスク02のWeb検索は網羅的ではありません。** 検索範囲の限界（認証が必要なコード検索、Discussions、論文の本文、多言語の検索結果ページなど）は、`output/02_Medium_Format_Research.md` の§Fにあります。検証済みの補足資料（`output/02_Medium_Format_Supplement.md`）は、対象を絞った確認を加えただけです。RAW/JPEGのサンプルは先頭64 KiBだけを確認しており、対応画像の全体は取得していません。どの候補についても、実機の色再現精度は独立に測定していません。

### 画像ルック作成（0.2.5）

27. **画像ルックは統計の一致であり、画素の一致ではありません。** ルックは、参考画像の統計と適用先（ベース）の統計の差です。統計は、Oklab での明度の分位点、明るさの帯ごとの色かぶり、明るさの帯ごと・色相区分ごとの彩度です。画素の対応は使わないため、色相の回転、部分補正、マスク、被写体に応じた調整、ノイズ、粒状感は再現しません。参考画像の内容（たとえば夕景）がルックと取り違えられることもあります。推定値の幅は制限しており（色かぶりはOklabのa・bで最大±0.08、彩度は明るさの帯ごとに0.5〜2倍、色相区分ごとに0.6〜1.6倍）、強いルックは弱めに出ます。ΔEなどの精度の数値はありません。「色相のカバー率」は網羅度であり、精度ではありません。
28. **「一般的な写真」の基準は平均値です。** 適用先の画像を登録しない場合、基準には組み込みの平均を使います。これは種類の異なる仕上がり写真45枚の平均です（警告 `typical_photo_baseline`）。統計がこの平均から大きく外れる写真（夜景、単色に近い写真、色調が強く偏った写真など）には、平均との差もルックの一部として加わります。適用先の画像（ルックをかける自分の写真）を登録するとこの誤差は除けますが、27の制限は残ります。どちらの基準についても、実際に編集された写真に対する精度は検証していません。
29. **無彩色は保護されません。** 色かぶりは明るさの帯ごとに加えるため、無彩色のグレーにも、参考画像と基準（適用先の画像または一般的な写真）の色かぶりの差が加わります。0.2.0〜0.2.4 の設定 `neutral_protection` は 0.2.5 で廃止しました。適用先の画像は食い違いの判定に使わず、参考画像どうしだけを比較します。
