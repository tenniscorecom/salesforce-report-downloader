"""src/exceptions.py — Salesforce レポートダウンローダーの例外。

ダウンローダーが送出する例外クラス（管理表・スケジュール・SOQL・フォルダまわりの
業務例外）をこのモジュールに置く。``DownloaderError`` は comken 側の
``comken.exceptions.DownloaderError`` をそのまま使うので、基底クラスとしてだけ
import する（再エクスポートはしない。利用側で ``from comken.exceptions import
DownloaderError`` を直接書いてください）。
"""

from pathlib import Path

from comken.exceptions import ComkenError, DownloaderError
from comken.services.salesforce_downloader.history import (
    ROUTE_BROWSER_FALLBACK_TRUNCATED,
)

__all__ = [
    "ReportNotRegisteredError",
    "GroupNotRegisteredError",
    "SoqlReportNotRegisteredError",
    "EmptyReportError",
    "ReportFolderNotFoundError",
    "ReportReservePathLimitError",
    "ScheduledDownloadFailedError",
    "BrowserFallbackFailedError",
    "BrowserFallbackIncompleteError",
    "MasterTableError",
    "MasterRowValueError",
    "MasterDuplicateValueError",
]


class ReportNotRegisteredError(DownloaderError):
    """指定した管理番号が管理表に無い

    管理番号はコードに定数で書く（CUSTOMER_LIST = "1001"）。管理表から行を消したり、
    番号を打ち間違えたりすると、どのレポートを指しているか決められない。

    発生箇所: download_scheduled() の `_validate_filters_by_report()` /
    src.paths の `_find()`

    対処:
        管理表を開いて、その管理番号の行があるか確認する。
        新しく使うレポートは、先に管理表へ登録する
    """

    def __init__(self, report_key: str, registered: list[str], master_path: Path) -> None:
        known = "、".join(str(key) for key in registered) or "（登録なし）"
        super().__init__(
            f"管理表に登録されていない管理番号です: {report_key}\n"
            f"登録済みの管理番号: {known}\n"
            f"管理表: {master_path}"
        )


class GroupNotRegisteredError(DownloaderError):
    """管理表の「グループ」列に設定シートに登録されていない値が書かれている

    出力先フォルダは「グループ→ベースパス」の対応を、設定シート（レポート管理表
    と同じブック内の「設定」シート）で管理する。管理表にないグループ名が書かれて
    いると、出力先を決められない。

    発生箇所: src.paths の report_folder()

    対処:
        管理表の「グループ」列に書かれた値が、設定シート（`group_settings.py` の
        `GroupSetting`）の「グループ」列に存在するか確認する。新しく部署・グループを
        追加するときは、設定シート側にも同じ名前で行を足す
    """

    def __init__(self, group: str, registered: list[str], master_path: Path) -> None:
        known = "、".join(registered) or "（登録なし）"
        super().__init__(
            f"管理表の「グループ」列に設定されていないグループ名です: {group}\n"
            f"設定シートに登録済みのグループ: {known}\n"
            f"管理表: {master_path}"
        )


class SoqlReportNotRegisteredError(DownloaderError):
    """管理表の「SOQL」列が「○」なのに、同じ管理番号の SoqlReport が登録されていない

    管理表と ``reports/`` 配下の ``SoqlReport`` 実装は別々に編集できるため、「SOQL」
    列だけ「○」にして ``SoqlReport`` の追加（``reports/<ファイル>.py`` への
    サブクラス定義）を忘れると、どの SOQL クエリを使えばいいか決められない。

    発生箇所: src.soql_reports の soql_report_for()

    対処:
        管理番号に対応する ``SoqlReport`` サブクラスを ``reports/`` 配下に追加し、
        ``KEY`` を管理表と同じ値にする（ファイル名を ``_`` で始めると
        走査対象外になるので、必ず実レポート名にする）。まだ SOQL 化していないなら、
        管理表の「SOQL」列を「×」に戻す
    """

    def __init__(self, report_key: str, registered: list[str]) -> None:
        known = "、".join(str(key) for key in registered) or "（登録なし）"
        super().__init__(
            f"管理番号 {report_key} はSOQL列が「○」ですが、SoqlReportが登録されていません。\n"
            f"登録済みのSOQL管理番号: {known}"
        )


class EmptyReportError(DownloaderError):
    """レポートは実行できたが明細が 0 行だった

    空のファイルを置くと、使う側は「データが無い日」と「取得が失敗した日」を
    区別できなくなる。0 行のときはファイルを作らず、失敗として扱う。

    発生箇所: download_scheduled() の `_save()`

    対処:
        Salesforce の画面で同じレポートを開き、本当に 0 件か確認してください。
        0 件が普通に起きるレポートなら、管理表の「0件あり」を「○」にしてください。
    """

    def __init__(self, report_key: str, summary: str, url: str) -> None:
        super().__init__(
            f"レポートの明細が 0 行でした: {report_key}（{summary}）\n"
            f"{url}\n"
            "取得の失敗と区別できないため、ファイルは作りません。\n"
            "Salesforce の画面で同じレポートを開き、本当に 0 件か確認してください。\n"
            "0 件が普通に起きるレポートなら、管理表の「0件あり」を「○」にしてください。"
        )


class ReportFolderNotFoundError(DownloaderError):
    """保存先として組み立てたフォルダが無い

    保存先フォルダは、管理表の「グループ」で引いた設定シートの「ベースURL」（フォルダのパス）
    そのものである（`src.paths.report_folder()`）。そのフォルダが存在しない場合にこの例外になる。
    無いフォルダを作らないのは、書き間違いのことが多いため。
    勝手に作ると、誰も読まない場所へ置き続けることになる。

    発生箇所: download_scheduled() の `_require_folder()`

    対処:
        設定シートの「ベースURL」（フォルダのパス）と、管理表の「グループ」を
        確認する。共有フォルダなら、つながっているか・権限があるかも確認する
    """

    def __init__(self, report_key: str, folder: Path) -> None:
        super().__init__(
            f"保存先のフォルダがありません: {report_key}\n"
            f"{folder}\n"
            "設定シートの「ベースURL」（フォルダのパス）と、管理表の「グループ」を"
            "確認してください。\n"
            "共有フォルダの場合は、つながっているか（権限があるか）も確認してください。"
        )


class ReportReservePathLimitError(DownloaderError):
    """保存ファイル名の連番が上限に達した

    `_reserve_unique_path()` は同じフォルダに既存ファイルがあると連番を足して別の
    ファイル名を探す。 上限（ ``RESERVE_PATH_LIMIT`` ）まで試しても確保できない
    のは権限・同期の異常など、運用側に原因があることが多い。

    発生箇所: download_scheduled() の `_reserve_unique_path()`

    対処:
        保存先フォルダが想定どおりか確認する。 共有フォルダなら、 古い取得
        ファイルを退避するか、 別の保存先に変える。 連発する場合は権限・排他
        制御の設定も見直す
    """

    def __init__(self, report_key: str, base_path: Path, limit: int) -> None:
        super().__init__(
            f"保存ファイル名の連番が上限に達しました: {report_key}\n"
            f"{base_path}\n"
            f"{limit} 回試しても空きのファイル名が見つかりませんでした。\n"
            "保存先フォルダの権限・排他制御と、 古い取得ファイルの数を確認してください。"
        )


class ScheduledDownloadFailedError(DownloaderError):
    """定期取得で1件以上が失敗した

    取得できたものは保存済み。**1件失敗しても残りは続けたうえで、最後にまとめて知らせる。**
    ログだけに出して正常終了すると、スケジューラや RPA 基盤から見て成功と区別が付かず、
    落ちていることに誰も気づかない。

    発生箇所: download_scheduled() の `_download_scheduled_locked()`

    対処:
        履歴（ダウンロード履歴.csv）の「エラー内容」で、失敗した理由を確認する。
        急いで必要なものは download_scheduled() をスケジュール外で実行する。
        権限を持つ人が Salesforce から手動でダウンロードしてもよい
    """

    def __init__(self, failed_keys: list[str], history_path: Path) -> None:
        keys = "、".join(str(key) for key in failed_keys)
        super().__init__(
            f"定期取得で {len(failed_keys)} 件が失敗しました: {keys}\n"
            f"失敗した理由は履歴を確認してください: {history_path}"
        )


class BrowserFallbackFailedError(DownloaderError):
    """Report API 経路の自動切替で、ブラウザの取り直し自体が失敗した

    ``SalesforceReportTruncatedError``（2000件超）または 0 行 + ``allow_empty=False``
    でブラウザ経由に取り直そうとしたが、ブラウザ取得中（``_fetch_via_browser()``）
    に別の例外（ログイン失敗・CredentialNotFoundError・CredentialError・通信断など）
    が出て失敗したときに送出する。**この例外は ``DownloaderError`` 系なので、
    ``_failure_row()`` 側で「Salesforce」区分に分類される**（プログラム扱い
    にしない）。

    引数で受けるのは ``report_key`` / ``summary`` / ``reason`` / ``route``
    （``"2000件超"`` または ``"0件"``、 ``route`` は ``ROUTE_BROWSER_FALLBACK_TRUNCATED``
    / ``ROUTE_BROWSER_FALLBACK_EMPTY`` のいずれか。履歴の「取得経路」列に書くため）。
    **ダウンローダー側で例外クラスを組み立てる都合上、
    ``SalesforceReportTruncatedError.__init__(self, report_id, row_limit)``
    のような「引数 2 つのシグネチャ」を経由しない**（文字列 1 つで ``raise``
    すると ``TypeError`` になり、原因区分が「プログラム」化ける）。

    発生箇所: ``src/service.py`` の ``_fetch_with_auto_fallback()`` と
    ``_download()`` の 0 行取り直し経路

    対処:
        Salesforce の画面から手動でダウンロードするか、管理表の「2000件超」/
        「0件あり」を「○」にして次回の実行を待ってください。
        認証情報が未登録なら ``CredentialNotFoundError`` の案内に従う
    """

    def __init__(self, report_key: str, summary: str, reason: str, route: str) -> None:
        super().__init__(
            f"Report API で{reason}だったためブラウザで取り直したが失敗しました: "
            f"{report_key}（{summary}）\n"
            "対処: Salesforce の画面から手動でダウンロードするか、"
            "管理表の「2000件超」/「0件あり」を「○」にして次回の実行を待ってください。"
        )
        # ``_download()`` 側で ``record_failure()`` の ``route`` 引数に転写するための
        # 属性。``ROUTE_BROWSER_FALLBACK_TRUNCATED`` / ``ROUTE_BROWSER_FALLBACK_EMPTY``
        # のいずれか（自動切替を試みたが失敗した、という意味での値）
        self.route = route


class BrowserFallbackIncompleteError(BrowserFallbackFailedError):
    """Report API では2000件超だったのに、ブラウザで取り直した結果も2000行以下だった

    Report API 経路（``2000件超`` × かつ ``SOQL`` ×）で ``SalesforceReportTruncatedError``
    が出たため、自動でブラウザ経由に取り直したが、**ブラウザでも Report API の
    上限（2000行）以下しか取れなかった**場合に送出する。2000件超のはずなので、
    全件取れていない。**管理表の「0件あり」が ○ でも
    空ファイルは作らずエラー扱いする**（空ファイルが保存されると、利用側は
    「データが無い日」と「取得が失敗した日」を区別できなくなるため）。

    引数で受けるのは ``report_key`` / ``summary`` / ``row_count`` （ブラウザで取れた
    行数 ``int``）。 ``self.route`` には ``ROUTE_BROWSER_FALLBACK_TRUNCATED``
    （自動切替を試みたが取り切れなかった、という意味での値）を入れる。 **親
    ``BrowserFallbackFailedError.__init__`` の文言（"Report API で...だったため
    ブラウザで取り直したが**失敗**しました"）は「ブラウザ取得自体が失敗」用なので、
    ここでは合わないため呼ばず、 ``DownloaderError.__init__`` に自前の文言を直接渡す**。
    ``route`` の値は comken の ``history`` モジュールから取る（循環 import にならない
    ことは ``src.exceptions`` を import するときに ``history`` 側の依存が無いことで
    確認済み）。

    発生箇所: ``src/service.py`` の ``_fetch_with_auto_fallback()``

    対処:
        Salesforce の画面からレポートを手動でエクスポートしてください。
    """

    def __init__(self, report_key: str, summary: str, row_count: int) -> None:
        DownloaderError.__init__(
            self,
            "Report API では 2000 件超だったのに、ブラウザで取り直した結果が"
            f" {row_count} 行でした。全件取れていません: {report_key}（{summary}）\n"
            "対処: Salesforce の画面からレポートを手動でエクスポートしてください。",
        )
        # 親 ``BrowserFallbackFailedError`` の ``__init__`` を呼ばないので ``self.route``
        # はここで必ず自分で入れる（``_download()`` 側の ``record_failure(route=...)``
        # への転写と、外側 ``except BrowserFallbackFailedError`` の ``exc.route`` 参照
        # で使われる）
        self.route = ROUTE_BROWSER_FALLBACK_TRUNCATED


class MasterTableError(ComkenError):
    """Excel の管理表に関するエラー。具体的な状況はメッセージに出る

    対処:
        メッセージに書かれた対処に従う。直らなければ画面全体のスクリーンショットを管理者へ
    """


class MasterRowValueError(MasterTableError):
    """管理表の値が正しくない

    数字を書く列に文字が入っている、決まった書き方以外を書いた、空にできない列が空、など。

    発生箇所: src.report_master の `MasterRow._build()` /
    src.sheets.schedule.ScheduleRule.validate()

    対処:
        メッセージに出ている行と列を、管理表で確認して直す
    """

    def __init__(self, row_number: int, header: str, value: object, reason: str) -> None:
        super().__init__(
            f"管理表 {row_number} 行目の「{header}」が正しくありません: {value!r}\n{reason}"
        )


class MasterDuplicateValueError(MasterTableError):
    """一意であるべき列に、同じ値が2つ以上ある

    管理番号のように「1つに決まる」ことが前提の列で重複すると、
    どの行を指しているか決められない。

    発生箇所: src.report_master の `MasterRow._build()`

    対処:
        管理表を開いて、重複している値のどちらかを別の値に変える
    """

    def __init__(self, header: str, value: object, path: Path) -> None:
        super().__init__(
            f"管理表の「{header}」に同じ値が2つあります: {value!r}\n"
            f"{path}\n"
            "この列は1つに決まる必要があるため、どちらかを変えてください。"
        )
