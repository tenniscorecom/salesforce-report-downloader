# Salesforceレポートダウンローダー

**定期取得の対象になっている Salesforce レポートを、毎日まとめて落とすバッチ。**

中身は数行しかありません。何を落とすか・どこへ置くかは、共通ライブラリ
[comken](https://github.com/tenniscorecom/original_libs) が持つ**管理表（Excel）**に書いてあります。

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

詳しくは comken の [docs/salesforce-downloader.md](https://github.com/tenniscorecom/original_libs/blob/master/docs/salesforce-downloader.md) を参照してください。
