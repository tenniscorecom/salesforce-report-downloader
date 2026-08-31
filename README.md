# Salesforceレポートダウンローダー

**定期取得の対象になっている Salesforce レポートを、毎日まとめて落とすバッチ。**
加えて、その下層にある **Salesforce レポートの集約取得ライブラリ
（`comken_salesforce_downloader`）**を内蔵する自己完結リポジトリです。

```
comken_salesforce_downloader/   ← ライブラリ本体（pip install -e で他PJからも使える）
src/                            ← このバッチの入口（download_scheduled を呼ぶだけ）
main.py                         ← バッチの実行ファイル
```

何を落とすか・どこへ置くかは、このリポジトリの `comken_salesforce_downloader` パッケージが持つ
**管理表（Excel）**に書いてあります。（`comken` は認証・`Table` などの共通基盤であり、
管理表機能そのものは持ちません。）

```python
from comken_salesforce_downloader import download_scheduled

download_scheduled("Salesforceレポートダウンローダー")   # 「定期」かつ有効なものを全部
```

## 使い方

```bat
認証情報の登録.bat    :: 初回だけ。sandbox の client_id / client_secret を登録する
実行.bat              :: 定期取得（スケジューラから毎日1回呼ぶ）
```

失敗があれば**終了コード 1 で止まります**（取得できたものは保存済み）。ログだけ出して
正常終了すると、スケジューラから見て成功と区別が付かないためです。

## レポートを増やす・減らす

**このプロジェクトは触りません。** このリポジトリの `comken_salesforce_downloader` が持つ管理表
（`レポート管理表.xlsx`）に行を足すだけです。（`comken` 側には管理表機能はありません。）

| 列 | 何を書くか |
|---|---|
| ID | 社内で決める管理番号（1001, 1002…）。Salesforce のレポート ID ではない |
| 概要 | 人が読んで分かる説明 |
| Salesforce URL | レポートを開いたときのアドレスをそのまま貼る |
| 実行方式 | `定期`（このバッチが取る）か `個別`（呼ばれたときだけ） |
| 保存先 | 落としたファイルを置くフォルダ |
| 有効 | 使わなくなったら `無効`（行は消さない） |
| 0件あり | 0 件が普通のレポートなら `○`、普通はデータがあるなら `×` |

**管理表（Excel）は非エンジニアが手動で用意・編集するものであり、コード側で
雛形を自動生成する機能は持たない。** 雛形が必要な場合は、
`ReportEntry.create_template()` を Python から直接呼ぶ
（[`docs/master-table.md`](docs/master-table.md) 参照）。

**編集した直後に `check` で読み込みを確かめる:**

```bat
python -m comken_salesforce_downloader check
```

`check` は、管理表を編集したあとに「プログラムから正しく読めるか」を確かめるためのもの。
書き方の誤り（管理番号の重複、URL からレポート ID を取り出せない等）は取得のときにも
止まるが、**編集した直後にその場で分かる**ほうが直すのが早い。

## 1日に何度も最新が必要なとき

**このバッチを増やさないでください。** そのプロジェクト側から直接呼べます。

```python
from comken_salesforce_downloader import download_report

CUSTOMER_LIST = 1001
path = download_report(CUSTOMER_LIST, "案件集計")   # その場で Salesforce へ取りに行く
```

## comken への依存が必須

このライブラリは `comken` を**外部ライブラリとして**利用します。共通例外（`ComkenError`
/ `SalesforceReportIDNotFoundError` など）は引き続き `from comken.exceptions import ...`
で読みます。**単体では動きません。**

## ローカル開発でのセットアップ

`comken` を兄弟ディレクトリに clone して、`pip install -e` で編集可能インストールします。

```bat
cd F:\dev
git clone https://github.com/tenniscorecom/comken
git clone https://github.com/tenniscorecom/salesforce-report-downloader

cd salesforce-report-downloader
python -m venv .venv
.venv\Scripts\activate
pip install -e ..\comken
pip install -e .
pip install pytest ruff
```

## 業務での配置

comken と同じ「共有サーバー直接参照（PYTHONPATH）」方式を使います。

- 利用側プロジェクトの `実行.bat` で、**`COMKEN_ROOT` だけを `PYTHONPATH` に足す**。
  `comken_salesforce_downloader` パッケージはこのリポジトリに内包されているため、
  別途 `PYTHONPATH` に足す必要はない。
- 配置時に書き換えるファイルは `comken_salesforce_downloader/_paths.py` の
  `MASTER_PATH` / `HISTORY_PATH` の2か所（仕様の詳細は
  [`docs/salesforce-downloader.md`](docs/salesforce-downloader.md) の
  「配置するときの設定」を参照）

## 利用側プロジェクトから（API）

このリポジトリを `pip install -e` して、他プロジェクトから `download_report` /
`cached_report` / `download_scheduled` を使う想定。

```python
from comken_salesforce_downloader import (
    cached_report,
    cached_report_path,
    download_report,
    download_report_path,
)

CUSTOMER_LIST = "1001"    # 管理表の「ID」。意味の分かる名前を付ける
SALES_RESULT = "1003"

table = download_report(CUSTOMER_LIST, "案件集計")
by_code = cached_report(SALES_RESULT).index("顧客コード")
# 中身が要らずファイルパスだけ欲しいときは download_report_path() / cached_report_path()
print(download_report_path(CUSTOMER_LIST))   # 保存した CSV のパス（読み込みは走らない）
```

**API の戻り値は `Table`**（`comken.core.table.model.Table`）。`index()` / `filter()` /
`replace()` など、`Table` の API がそのまま使えます。

| 関数 | 意味 | Salesforce へ問い合わせるか |
|---|---|---|
| `download_report(ID)` | **今この瞬間に取りに行く** | **必ず行く**（今日すでに取っていても取り直す） |
| `cached_report(ID)` | **本日の定期取得キャッシュを受け取る** | **行かない**（無ければ例外） |
| `download_scheduled()` | 管理表で「定期」のものをまとめて取得 | **行く**（呼び出しは定期実行プロジェクト） |

## 取得スケジュールのたたき台

スケジュールは「1行につき1つの取得ルール」として管理します。同じレポートを月・水・金に
取得する場合は、スケジュール管理表に3行登録します。

| スケジュールキー | レポートキー | 取得頻度 | 曜日 | 取得時刻 | 祝日対応 | 有効 |
|---|---|---|---|---|---|---|
| S001 | R001 | 毎週 | 月 | 09:00 | 取得しない | ○ |
| S002 | R001 | 毎週 | 水 | 09:00 | 取得しない | ○ |
| S003 | R001 | 毎週 | 金 | 09:00 | 取得しない | ○ |

Python側では、表の1行を `ScheduleRule.from_row()` に渡して判定します。

```python
from datetime import datetime

from comken_salesforce_downloader.schedule import ScheduleRule

rule = ScheduleRule.from_row(
    {
        "スケジュールキー": "S001",
        "レポートキー": "R001",
        "取得頻度": "毎週",
        "曜日": "月",
        "取得時刻": "09:00",
        "祝日対応": "取得しない",
        "有効": "○",
    }
)

if rule.is_due(datetime.now(), holidays=set()):
    print("このレポートを取得する")
```

`src/schedule.py` はスケジュール判定だけを担当し、Salesforceへの接続や履歴の保存は行いません。

## ドキュメント

- [`docs/salesforce-downloader.md`](docs/salesforce-downloader.md) — 利用ガイド・配置・履歴の仕様
- [`docs/master-table.md`](docs/master-table.md) — Excel 管理表の仕組み（`MasterRow` / `column()`）

## ライセンス / 配布

社内ライブラリ。PyPI には公開していません。共有サーバーへ `checkout` して配置する
運用です。
