# 開発メモ

機能を追加・修正するときの前提をまとめたものです。AI に作業を頼むときは、この文書と `video_date_editor.py` を一緒に渡してください。

## 構成

- `video_date_editor.py` 1 ファイル構成。外部ライブラリなし（標準ライブラリ + tkinter）
- ExifTool はコマンドとして呼び出す（同じフォルダ → `/opt/homebrew/bin` → `/usr/local/bin` → PATH の順に探す）

## 実装上の決めごと（変更時に壊さないこと）

- **日本語ファイル名対策**：ExifTool には引数を UTF-8 の引数ファイル（`-@`）で渡し、`-charset filename=utf8` を付ける
- **タイムゾーン**：QuickTime の日時は UTC 保存なので常に `-api QuickTimeUTC` を付け、書き込む値に `+09:00` などを付ける
- **書き込むタグ**：QuickTime の CreateDate / ModifyDate と Track・Media の Create/Modify（全トラック）。iPhone 形式の `Keys:CreationDate` は、対象ファイルが持っている場合か、元動画が持っている場合に書く
- **未設定の日時**：QuickTime の 1904 年は「日時なし」として扱う
- **元動画の日時の優先順**：Keys:CreationDate → QuickTime:CreateDate → DateTimeOriginal（AVCHD 等）→ Sony MXF の `C0001M01.XML` の CreationDate → MXF 内の CreateDate（タイムゾーンなし。「MXF の日時を UTC として扱う」チェックで解釈を切り替え）
- **Mac の tkinter 対策**：起動時にウィンドウを最前面に出し、1px リサイズして再描画（黒い画面対策）。`VDE_THEME=clam` で描画テーマを変更可能
- 書き込みは別スレッドで 1 ファイルずつ実行し、進捗バーを更新

## 主な関数

| 関数 | 役割 |
|---|---|
| `run_exiftool` | 引数ファイル経由で ExifTool を実行 |
| `read_dates` | 書き換え対象の現在日時・長さを読む |
| `read_sources` | 元動画の日時・長さを読む（MXF/XML 含む） |
| `plan_matches` | 名前→長さの順で元動画を対応付ける |
| `build_write_args` | 書き込み用の ExifTool 引数を作る |
| `App.compute_new` | モードごとの変更後日時（プレビューと書き込みで共通） |

## 今後やりたいこと（メモ）

- （追加したい機能をここに書いておく）
