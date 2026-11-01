# input/ccd_profiles — サンプルカメラプロファイル（テスト用）

このフォルダのファイルはリポジトリに含めていません（`.gitignore` で除外）。出所の GPL-3.0 プロジェクトでのプロファイルごとの再配布条件を確認できていないためです。
アプリ本体の動作には不要です。プロファイルがない場合、それを使うテストは自動でスキップされます（`tests/conftest.py`）。

## 入手方法

リポジトリのルートで次を実行します。固定したコミットから取得し、SHA-256 を照合して、このフォルダに保存します。

```
python packaging/testing/acquire_profiles.py
```

取得記録は `output/test-profile-acquisition/sources.json` に出力されます。

| 保存名 | 取得元（リポジトリ / パス） |
|---|---|
| Nikon_D70s.dcp | RawTherapee/RawTherapee `rtdata/dcpprofiles/NIKON D70s.dcp` |
| Nikon_D200.dcp | RawTherapee/RawTherapee `rtdata/dcpprofiles/Nikon D200.dcp` |
| Olympus_E1.dcp | RawTherapee/RawTherapee `rtdata/dcpprofiles/Olympus E-1.dcp` |
| Nikon_D200.icc | RawTherapee/RawTherapee `rtdata/iccprofiles/input/Nikon D200.icc` |
| Nikon_D70.icc | google-code-export/photivo `Profiles/Camera/Standard/NIKON_D70.icc` |
| Sony_DSC-F828.icc | google-code-export/photivo `Profiles/Camera/Standard/SONY_DSC-F828.icc` |
| Sony_DSC-V3.icc | google-code-export/photivo `Profiles/Camera/Standard/SONY_DSC-V3.icc` |
| Kodak_DCS760.icc | google-code-export/photivo `Profiles/Camera/Standard/Kodak_DCS760C.icc` |
| Kodak_ProBack645.icc | google-code-export/photivo `Profiles/Camera/Standard/Kodak_ProBack645.icc` |

- RawTherapee: https://github.com/RawTherapee/RawTherapee （GPL-3.0、コミット b2ee504）
- Photivo: https://github.com/google-code-export/photivo （GPL-3.0、コミット 1f71427）
- スクリプトは各プロジェクトのライセンス文書なども、このフォルダに `.txt` で保存します。

## 注意

- 取得したプロファイルと、それから作った LUT は、元のプロジェクトの条件を確認するまで再配布しないでください。
- 配布物（exe）にはプロファイルを入れません。ビルド時に `packaging/verify_no_camera_inputs.py` が検査します。

## 確認

```
pytest -q
```

スキップされたテストがなくなれば、プロファイルが正しく置かれています。
