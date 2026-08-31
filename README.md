# Salesforceレポートダウンローダー

**定期取得の対象になっている Salesforce レポートを、毎日まとめて落とすバッチ。**

ライブラリ本体は comken（[`comken.services.salesforce_downloader`](https://github.com/tenniscorecom/comken)）
に統合済みのため、このリポジトリは**実行バッチのみ**を置きます。

```
src/                            ← このバッチの入口（download_scheduled を呼ぶだけ）
main.py                         ← バッチの実行ファイル
```

何を落とすか・どこへ置くかは、comken が持つ **管理表（Excel）** に書いてあります。

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

**このプロジェクトは触りません。** comken が持つ管理表（`レポート管理表.xlsx`）に
行を足すだけです。

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
（comken 側の [`docs/master-table.md`](https://github.com/tenniscorecom/comken/blob/master/docs/master-table.md) 参照）。

**編集した直後に `check` で読み込みを確かめる:**

```bat
python -m comken sfdl check
```

`check` は、管理表を編集したあとに「プログラムから正しく読めるか」を確かめるためのもの。
書き方の誤り（管理番号の重複、URL からレポート ID を取り出せない等）は取得のときにも
止まるが、**編集した直後にその場で分かる**ほうが直すのが早い。

## 1日に何度も最新が必要なとき

**このバッチを増やさないでください。** そのプロジェクト側から直接呼べます。

```python
from comken.services.salesforce_downloader import download_report

CUSTOMER_LIST = 1001
path = download_report(CUSTOMER_LIST, "案件集計")   # その場で Salesforce へ取りに行く
```

## comken への依存が必須

このバッチは comken を**外部ライブラリとして**利用します。共通例外（`ComkenError`
/ `SalesforceReportIDNotFoundError` など）は引き続き `from comken.exceptions import ...`
で読みます。**単体では動きません。**

## 業務での配置

comken と同じ「共有サーバー直接参照（PYTHONPATH）」方式を使います。

- 利用側プロジェクトの `実行.bat` で、**`COMKEN_ROOT` だけを `PYTHONPATH` に足す**。
  `comken.services.salesforce_downloader` パッケージは comken 内にあるため、
  別途 `PYTHONPATH` に足す必要はない。
- 配置時に書き換えるファイルは comken 側の
  `comken/services/salesforce_downloader/_paths.py` の `MASTER_PATH` / `HISTORY_PATH`
  の2か所（仕様の詳細は comken 側の
  [`docs/salesforce-downloader.md`](https://github.com/tenniscorecom/comken/blob/master/docs/salesforce-downloader.md)
  の「配置するときの設定」を参照）

## comken への再統合の経緯

このリポジトリはかつて `comken_salesforce_downloader` という独立ライブラリを内包していたが、

- 他のプロジェクトが呼び出すたびに comken 用とは別の `PYTHONPATH` / `pip install`
  設定が必要になる不便が判明したこと
- comken 本体側で完結する方が、ドキュメント・テスト・リリースが揃えられること

から、2026-08-31 に comken 本体へ再統合しました。このリポジトリは**実行バッチのみ**を残します。

## ドキュメント

- comken 側 [`docs/salesforce-downloader.md`](https://github.com/tenniscorecom/comken/blob/master/docs/salesforce-downloader.md) — 利用ガイド・配置・履歴の仕様
- comken 側 [`docs/master-table.md`](https://github.com/tenniscorecom/comken/blob/master/docs/master-table.md) — Excel 管理表の仕組み（`MasterRow` / `column()`）

## ライセンス / 配布

社内ライブラリ。PyPI には公開していません。共有サーバーへ `checkout` して配置する
運用です。
