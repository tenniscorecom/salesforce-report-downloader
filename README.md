# Salesforceレポートダウンローダー

**定期取得の対象になっている Salesforce レポートを、毎日まとめて落とすバッチ。**

中身は数行しかありません。何を落とすか・どこへ置くかは、共通ライブラリ
[comken](https://github.com/tenniscorecom/comken) が持つ**管理表（Excel）**に書いてあります。

```python
from comken.services.salesforce_downloader import download_scheduled

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

**このプロジェクトは触りません。** comken の管理表（`レポート管理表.xlsx`）に行を足すだけです。

| 列 | 何を書くか |
|---|---|
| ID | 社内で決める管理番号（1001, 1002…）。Salesforce のレポート ID ではない |
| 概要 | 人が読んで分かる説明 |
| Salesforce URL | レポートを開いたときのアドレスをそのまま貼る |
| 実行方式 | `定期`（このバッチが取る）か `個別`（呼ばれたときだけ） |
| 保存先 | 落としたファイルを置くフォルダ |
| 有効 | 使わなくなったら `無効`（行は消さない） |

## 1日に何度も最新が必要なとき

**このバッチを増やさないでください。** そのプロジェクト側から直接呼べます。

```python
from comken.services.salesforce_downloader import download_report

CUSTOMER_LIST = 1001
path = download_report(CUSTOMER_LIST, "案件集計")   # その場で Salesforce へ取りに行く
```

詳しくは comken の [docs/salesforce-downloader.md](https://github.com/tenniscorecom/comken/blob/master/docs/salesforce-downloader.md) を参照してください。

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

from comken.services.salesforce_downloader.schedule import ScheduleRule

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
