"""UI strings for the LUT Compare workspace (English / Japanese).

Merged into :mod:`strings` so the shared key-parity and placeholder tests
cover them.
"""

# fmt: off
EN: dict[str, str] = {
    "nav.compare": "LUT Compare",
    "compare.subtitle": "See several LUTs on several photos in one table. Export the table as a contact sheet, or chosen cells as before/after images.",
    # groups and controls
    "compare.group.photos": "Photos",
    "compare.group.luts": "LUTs",
    "compare.group.output": "Display and export",
    "compare.cell_size": "Cell size (long side)",
    "compare.selected": "{n} cell(s) selected for before/after images. Click a cell to select or clear it.",
    "compare.empty": "Add at least one photo and one LUT to see the table. LUTs can come from a file, or from the \"Add to comparison\" button on the pages that build LUTs.",
    "compare.grid.accessible": "Table of photos and LUTs",
    "compare.photos.accessible": "Photos in the comparison",
    "compare.luts.accessible": "LUTs in the comparison",
    "compare.col.before": "Before",
    # actions and dialogs
    "compare.action.add": "Add to comparison",
    "compare.action.add_tip": "Keep the LUT as it is now in LUT Compare and open that page. Rebuilding it here later does not change the comparison.",
    "compare.action.add_photos": "Add photos…",
    "compare.action.add_luts": "Add LUT files…",
    "compare.action.remove": "Remove",
    "compare.action.sheet": "Export contact sheet…",
    "compare.action.split": "Export before/after images…",
    "compare.dialog.photos": "Add photos to the comparison",
    "compare.dialog.luts": "Add 3D LUT files to the comparison",
    "compare.dialog.folder": "Choose a folder for the JPEG files",
    # warning
    "compare.warning.log": "Probably a Log-input LUT: colours look wrong",
    # cells
    "compare.cell.pending": "Waiting…",
    "compare.cell.failed": "Could not be shown",
    "compare.cell.accessible": "{photo}, {lut}",
    "compare.cell.accessible_selected": "{photo}, {lut}, selected",
    # reasons
    "compare.reason.need_both": "Add at least one photo and one LUT first.",
    "compare.reason.no_selection": "Click at least one cell in a LUT column first.",
    "compare.reason.exporting": "Export in progress.",
    # status and jobs
    "compare.status.idle": "Ready.",
    "compare.status.working": "Working…",
    "compare.job.photo": "Load photo {name}",
    "compare.job.lut": "Load LUT {name}",
    "compare.job.receive": "Check LUT {name}",
    "compare.job.render": "Render comparison table",
    "compare.job.sheet": "Export contact sheet",
    "compare.job.split": "Export before/after images",
    # log
    "compare.log.duplicate": "{name} is already in the comparison, so it was not added again.",
    "compare.log.photo_failed": "Could not add photo {name}: {reason}",
    "compare.log.lut_failed": "Could not add LUT {name}: {reason}",
    "compare.log.lut_not_checked": "LUT {name} was added, but it could not be checked for Log input: {reason}",
    "compare.log.cancelled_add": "Adding {name} was cancelled.",
    "compare.log.cell_failed": "Could not show {photo} with {lut}: {reason}",
    "compare.log.exists": "Nothing was written, because these files already exist in the folder:",
    "compare.log.collision": "{name} was not written: another selected cell uses the same file name.",
    "compare.log.item_failed": "{name} failed: {reason}",
    "compare.log.done": "Wrote {n} file(s).",
    "compare.log.done_failed": "Wrote {n} file(s); {failed} failed.",
    "compare.log.cancelled": "Export cancelled after {n} file(s). Finished files were kept.",
    "compare.log.stopped": "Export stopped at {name} after {n} file(s): {reason}",
    "compare.error.exists": "Nothing was written. These files already exist in the folder: {files}",
}

JA: dict[str, str] = {
    "nav.compare": "LUT 比較",
    "compare.subtitle": "複数の LUT と複数の写真を1つの表で見比べます。表はコンタクトシートとして、選んだセルは適用前・適用後の比較画像として書き出せます。",
    # groups and controls
    "compare.group.photos": "写真",
    "compare.group.luts": "LUT",
    "compare.group.output": "表示と書き出し",
    "compare.cell_size": "セルの大きさ（長辺）",
    "compare.selected": "比較画像にするセルを {n} 件選んでいます。セルをクリックすると選択と解除を切り替えます。",
    "compare.empty": "写真と LUT を1つ以上追加すると表が表示されます。LUT はファイルから追加するか、LUT を作る画面の「比較に追加」ボタンで追加します。",
    "compare.grid.accessible": "写真と LUT の表",
    "compare.photos.accessible": "比較する写真",
    "compare.luts.accessible": "比較する LUT",
    "compare.col.before": "適用前",
    # actions and dialogs
    "compare.action.add": "比較に追加",
    "compare.action.add_tip": "今の LUT を LUT 比較に保持して、その画面を開きます。あとでこの画面で作り直しても、比較の内容は変わりません。",
    "compare.action.add_photos": "写真を追加…",
    "compare.action.add_luts": "LUT ファイルを追加…",
    "compare.action.remove": "削除",
    "compare.action.sheet": "コンタクトシートを書き出す…",
    "compare.action.split": "比較画像を書き出す…",
    "compare.dialog.photos": "比較に写真を追加",
    "compare.dialog.luts": "比較に 3D LUT ファイルを追加",
    "compare.dialog.folder": "JPEG ファイルの保存先フォルダーを選択",
    # warning
    "compare.warning.log": "Log 入力用の LUT と推定: 色が正しく見えません",
    # cells
    "compare.cell.pending": "待機中…",
    "compare.cell.failed": "表示できません",
    "compare.cell.accessible": "{photo}、{lut}",
    "compare.cell.accessible_selected": "{photo}、{lut}、選択中",
    # reasons
    "compare.reason.need_both": "先に写真と LUT を1つ以上追加してください。",
    "compare.reason.no_selection": "先に LUT の列のセルを1つ以上クリックしてください。",
    "compare.reason.exporting": "書き出しの途中です。",
    # status and jobs
    "compare.status.idle": "準備完了。",
    "compare.status.working": "処理中…",
    "compare.job.photo": "写真 {name} を読み込む",
    "compare.job.lut": "LUT {name} を読み込む",
    "compare.job.receive": "LUT {name} を確認する",
    "compare.job.render": "比較の表を描画する",
    "compare.job.sheet": "コンタクトシートを書き出す",
    "compare.job.split": "比較画像を書き出す",
    # log
    "compare.log.duplicate": "{name} はすでに比較に入っているので、追加しませんでした。",
    "compare.log.photo_failed": "写真 {name} を追加できませんでした: {reason}",
    "compare.log.lut_failed": "LUT {name} を追加できませんでした: {reason}",
    "compare.log.lut_not_checked": "LUT {name} を追加しましたが、Log 入力用かどうかを確認できませんでした: {reason}",
    "compare.log.cancelled_add": "{name} の追加をキャンセルしました。",
    "compare.log.cell_failed": "{photo} と {lut} の組み合わせを表示できませんでした: {reason}",
    "compare.log.exists": "フォルダーに次のファイルがすでにあるので、1ファイルも書き出しませんでした:",
    "compare.log.collision": "{name} は書き出しませんでした。選択した別のセルと同じファイル名になります。",
    "compare.log.item_failed": "{name} は失敗しました: {reason}",
    "compare.log.done": "{n} 件のファイルを書き出しました。",
    "compare.log.done_failed": "{n} 件のファイルを書き出しました。{failed} 件は失敗しました。",
    "compare.log.cancelled": "{n} 件を書き出したところでキャンセルしました。書き終えたファイルは残しています。",
    "compare.log.stopped": "{n} 件を書き出したあと、{name} で止まりました: {reason}",
    "compare.error.exists": "1ファイルも書き出しませんでした。フォルダーに次のファイルがすでにあります: {files}",
}
# fmt: on
