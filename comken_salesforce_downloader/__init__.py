r"""comken_salesforce_downloader/__init__.py — Salesforce レポートの集約取得。

各プロジェクトが個別に Salesforce からレポートを落としていると、**どのプロジェクトが
どのレポートを、どれくらいの頻度で取っているのか**が分からなくなる。取得をここに集約し、
何を取っているかは管理表（Excel）に、いつ何を取ったかは履歴（CSV）に集める。

    from comken_salesforce_downloader import cached_report, download_report

    CUSTOMER_LIST = "1001"        # プロジェクトごとに、意味の分かる名前を付ける
    SALES_RESULT = "1003"

    rows = download_report(CUSTOMER_LIST).read_rows()             # 今すぐ取りに行く
    by_code = cached_report(SALES_RESULT).index("顧客コード")

**プロジェクトのコードに Salesforce の URL もレポート ID も書かない。** 書くのは
管理番号だけで、参照先の差し替えは管理表を直せば済む（コードは変えない）。

    download_report      今すぐ Salesforce から取得して、ファイルを CSV で返す
    cached_report        本日の定期取得キャッシュを CSV で返す（取りに行かない）
    download_scheduled   「定期」登録の全件を取得する（定期実行のプロジェクトが呼ぶ）
    file_path_of         そのレポートが保存されるパス
    load_master          管理表を読む
    shared_report_ids    同じ Salesforce レポートを指している管理番号を返す
    ReportEntry          管理表の1行
    ReportEntry.create_template  管理表の雛形（Excel）を作る

管理表の検査はコマンドからも呼べる（保守用。業務の定期実行ではない）:

    python -m comken_salesforce_downloader check

---

**このリポジトリは `comken` から分離した独立ライブラリ。** 元は comken 内の
`services/salesforce_downloader` だったが、Salesforce を触る書き方が comken 内に
2つ（`toolbox/salesforce` と `services/`）ある状態を解消するため、別リポジトリへ
切り出した。comken 本体側の共有例外（`ComkenError` / `SalesforceReportIDNotFoundError`
など）は引き続き `from comken.exceptions import ...` で読み込む。

---

**このファイルが持つもの:**
- Salesforce レポートの「定義（管理表）・取得・保存・履歴」

**ここに書かないもの:**
- いつ取るか（毎日・平日・月末などのスケジュール判定） → 呼び出す側のプロジェクト
- 取ったデータの加工・DB登録・帳票化 → 利用プロジェクト
- 取得成功時の通知（メール・チャット等） → 利用プロジェクト
- 「このプロジェクトのときはこうする」という業務ルール → 利用プロジェクト

**迷ったときの判定:**
- **1つのプロジェクトだけが困っているなら、そのプロジェクトに書く**
- **全プロジェクトが同じように困るなら、ここに書く**

迷ったら入れない。プロジェクト側に書いたものは後から共通へ引き上げられるが、
ここに入れたものは利用者が付いた後だと外せなくなるため（後から動かせる方向へ倒す）。

---

**`__init__.py` 経由の import で `requests` を読ませない設計。**

`service.py` を import すると `requests` が必要になる。BO 環境のように
`requests` が入っていないところで `cached_report` /
`file_path_of` / `load_master` / `shared_report_ids` / `ReportEntry`
だけ動かせるよう、`__getattr__` (PEP 562) で遅延 import する。

`download_report` / `download_scheduled` を import したときだけ
`service.py` が読み込まれ、`requests` がロードされる。
"""

from comken_salesforce_downloader.master import (
    ReportEntry,
    load_master,
    shared_report_ids,
)
from comken_salesforce_downloader.schedule import ScheduleRule

__all__ = [
    "download_report",
    "download_report_path",
    "download_scheduled",
    "cached_report",
    "cached_report_path",
    "file_path_of",
    "load_master",
    "shared_report_ids",
    "ReportEntry",
    "ScheduleRule",
]

# 遅延 import する対象。値はその属性が定義されているサブモジュールの絶対パス。
# import 時に service.py を読み込むと requests が要るので、必要なときにだけ読む。
_LAZY_TARGETS: dict[str, str] = {
    "download_report": "comken_salesforce_downloader.service",
    "download_report_path": "comken_salesforce_downloader.service",
    "download_scheduled": "comken_salesforce_downloader.service",
    "cached_report": "comken_salesforce_downloader.provider",
    "cached_report_path": "comken_salesforce_downloader.provider",
    "file_path_of": "comken_salesforce_downloader.provider",
}


def __getattr__(name: str) -> object:
    """`from ... import X` の X を必要になったタイミングでだけ import する。

    Raises:
        AttributeError: 定義されていない属性を要求したとき。
    """
    module_name = _LAZY_TARGETS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # 2回目以降は module ロードをスキップして globals() から返す (PEP 562 の慣例)
    import importlib

    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """`dir(comken_salesforce_downloader)` で遅延対象も返す。"""
    return sorted(set(__all__) | set(_LAZY_TARGETS))

