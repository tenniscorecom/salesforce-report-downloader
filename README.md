# Salesforceレポートダウンローダー

**定期取得の対象になっている Salesforce レポートを、まとめて落とすバッチ。**

管理表・履歴の**形式**（列定義・読み取り関数）は comken
（[`comken.services.salesforce_downloader`](https://github.com/tenniscorecom/comken)）
側の共有契約のままですが、**実際に Salesforce へ取りに行き、保存し、履歴へ書く実行部分は
2026-09 にこのリポジトリへ戻しました**（`src/salesforce_downloader/`）。理由は下の
「comken との分担の経緯」を参照。

```
src/
  run.py                         ← バッチの入口（download_scheduled を呼ぶだけ）
  salesforce_downloader/
    service.py                   ← 取得・保存・スケジュール判定の本体
    history_writer.py            ← 履歴CSVへの書き込み
main.py                          ← バッチの実行ファイル
```

何を落とすか・どこへ置くかは、comken が持つ **管理表（Excel）** に書いてあります。

```python
from src.salesforce_downloader import download_scheduled

download_scheduled("Salesforceレポートダウンローダー")  # スケジュールに従い、今取るべきものを全部
```

`download_scheduled()` は内部で「スケジュールに従うか」「今日すでに成功済みか」を
判定するため、**頻繁に呼んでも二重取得は起きない**。呼ぶ頻度を上げるほど、
「取るべき時刻」に早く追従できる、というだけの違い。

**API・ブラウザ経由・SOQLのどれで取るかは、管理表の列（「2000件超」「SOQL」）で決まる。**
呼び出し側のコード（`src/run.py`）は意識しない。優先順位は「SOQL」列 →「2000件超」列 →
通常のReport API。ブラウザ経由は事前に人が一度だけ手動ログインしておく必要がある
（詳しくは `src/salesforce_downloader/service.py` の `_fetch_via_browser()` を参照）。
SOQL経由は同じ管理番号の `SoqlReport` が
`comken.services.salesforce_downloader.soql_reports` に登録されている必要がある
（詳しくは `_fetch_via_soql()` を参照）。

## 使い方

```bat
認証情報の登録.bat      :: 初回だけ。sandbox の client_id / client_secret を登録する
実行.bat                :: 定期取得。WinActor（社内RPA基盤）から高頻度（例: 1 時間おき）で呼ぶ
```

`実行.bat` は、**WinActor（社内RPA基盤）等から高頻度（例: 1 時間おき）で繰り返し呼ぶ**
運用です。`download_scheduled()` が「取るべき時刻」と「本日の成功済み」を内部で判定する
ため、1 日に何度動いても安全です（このリポジトリ側は「何度呼ばれても正しく動く」ところ
までを担当し、実際に何時間おきに呼ぶかという定期実行の設定自体は WinActor 側の仕事。
「実行される単位（バッチ）は comken に置かない」という方針と同じ理由で、Windows
タスクスケジューラへの登録もこのリポジトリには持たせない）。

失敗があれば**終了コード 1 で止まります**（取得できたものは保存済み）。ログだけ出して
正常終了すると、WinActor から見て成功と区別が付かないためです。

## レポートを増やす・減らす

**このプロジェクトは触りません。** comken が持つ管理表（`レポート管理表.xlsx`）に
行を足すだけです。

| 列 | 何を書くか |
|---|---|
| ID | 社内で決める管理番号（1001, 1002…）。Salesforce のレポート ID ではない |
| 概要 | 人が読んで分かる説明。**記録用**。出力ファイル名・フォルダ階層には使わない |
| Salesforce URL | レポートを開いたときのアドレスをそのまま貼る |
| グループ | 出力先の起点となるグループ名。同じブック内の「設定」シートに登録したグループ名と一致させる |
| 担当者 | 担当者名。**記録用**。出力パス（フォルダ階層）には使わない |
| 有効 | 使わなくなったら `無効`（行は消さない） |
| 0件あり | 0 件が普通のレポートなら `○`、普通はデータがあるなら `×` |

**保存先（フォルダ）は管理表に書かない。** 出力先は「設定」シート（管理表と同じブック内の
別シート）の「グループ→ベースURL」の対応だけで組み立てる（`provider.report_folder()`）。
2026-09 に「`ベースパス / 担当者 / 概要 / ファイル`」の3階層組み立ては廃止し、
「`ベースパス / ファイル`」の単一階層に1本化した。`担当者` / `概要` は管理表に
記録用として残るが、出力パスには影響しない（詳しくは comken 側の
[`docs/salesforce-downloader.md`](https://github.com/tenniscorecom/comken/blob/master/docs/salesforce-downloader.md) 参照）。

**管理表（Excel）は非エンジニアが手動で用意・編集する。** 雛形の生成は
`src/salesforce_downloader/template_writer.py` の関数
（`create_combined_workbook()` / `create_template()`）で行う。**「管理表」
「スケジュール」「設定」の3シートを1つのブックにまとめて生成**したい場合は
`create_combined_workbook(path)` を呼ぶ（詳しくは同モジュールの docstring を参照）。

**編集した直後に `check` で読み込みを確かめる:**

```bat
python -m comken sfdl check
```

`check` は、管理表を編集したあとに「プログラムから正しく読めるか」を確かめるためのもの。
書き方の誤り（管理番号の重複、URL からレポート ID を取り出せない等）は取得のときにも
止まるが、**編集した直後にその場で分かる**ほうが直すのが早い。

## 1日に何度も最新が必要なとき

**このバッチを増やすのは避けてください。** 取ってきた「本日の最新キャッシュ」を
別プログラムから読む方が速くて安全です。

```python
from comken.services.salesforce_downloader import cached_report, cached_report_path

CUSTOMER_LIST = "1001"
table = cached_report(CUSTOMER_LIST)  # 本日のキャッシュを Table で受け取る
path = cached_report_path(CUSTOMER_LIST)  # パスだけ欲しいとき
```

`cached_report()` / `cached_report_path()` は**取りに行かない**。
本日の定期取得がまだ走っていない等の理由でキャッシュが無いときは
`CachedReportNotFoundError`（`from comken.exceptions import ...`）が出るので、
「今回は諦めて次回のポーリングに任せる」「ログだけ出してスキップする」等の
ポリシーを呼び出し側で決めてください。

1 日に何度も欲しいレポートを**このバッチの中**で完結させたいときは、
comken 側の「スケジュール」シートにスケジュールキーを分けて複数行登録すれば、
このバッチの次の実行時刻に自動で追従します。

## comken への依存が必須

このバッチは comken を**外部ライブラリとして**利用します。管理表・履歴の形式
（`ReportEntry` / `HistoryRow` / `COLUMNS` / パスの置き場所）・読み取り関数
（`cached_report()` など）・共通例外（`ComkenError` / `SalesforceReportIDNotFoundError` /
`CachedReportNotFoundError` など）はすべて comken 側から import します。**単体では
動きません。**

## 業務での配置

comken と同じ「共有サーバー直接参照（PYTHONPATH）」方式を使います。

- 利用側プロジェクトの `実行.bat` で、**`COMKEN_ROOT` だけを `PYTHONPATH` に足す**。
- 配置時に書き換えるファイルは comken 側の
  `comken/services/salesforce_downloader/paths.py` の `MASTER_PATH` / `HISTORY_PATH`
  の2か所（仕様の詳細は comken 側の
  [`docs/salesforce-downloader.md`](https://github.com/tenniscorecom/comken/blob/master/docs/salesforce-downloader.md)
  の「配置するときの設定」を参照）
- 定期実行の登録（何時間おきに呼ぶか）は WinActor（社内RPA基盤）側の設定で行う。
  このリポジトリには持たせない

## comken との分担の経緯

このリポジトリと comken の分担は何度か変わっている（新しい方を先に書く）:

- **2026-09: 取得を実行する側（`download_scheduled()` / `service.py`）を、再び
  comken の外（このリポジトリ）へ切り出した。** `download_scheduled()` を実際に
  呼ぶプロジェクトは今のところこのリポジトリだけで、しかもここで完結している。
  単一消費者の実行部分を comken に置き続ける理由が無くなっていたため。
  管理表・履歴の**形式**（列定義・読み取り関数）は、将来別プロジェクトが
  `cached_report()` で読みに来たときのために、引き続き comken 側に残した。
- 2026-08-30 にこのリポジトリ（当時 `comken-salesforce-downloader`）へ分離
- 他のプロジェクトが呼び出すたびに comken 用とは別の `PYTHONPATH` / `pip install`
  設定が必要になる不便が判明したため、2026-08-31 に comken 本体へ再統合
  （この時点では取得実行部分も含めて丸ごと comken 側にあった）

経緯の詳細は comken 側の `comken/services/salesforce_downloader/__init__.py` の
履歴メモを参照。

## ドキュメント

- comken 側 [`docs/salesforce-downloader.md`](https://github.com/tenniscorecom/comken/blob/master/docs/salesforce-downloader.md) — 利用ガイド・配置・履歴の仕様
- comken 側 [`docs/master-table.md`](https://github.com/tenniscorecom/comken/blob/master/docs/master-table.md) — Excel 管理表の仕組み（`MasterRow` / `column()`）

## ライセンス / 配布

社内ライブラリ。PyPI には公開していません。共有サーバーへ `checkout` して配置する
運用です。