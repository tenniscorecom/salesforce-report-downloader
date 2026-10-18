"""src/exceptions.py — Salesforce レポートダウンローダーの例外。

管理表・スケジュール・SOQL・フォルダまわりの業務例外を置く。基底の ``DownloaderError`` は
comken 側のものを使うので再エクスポートしない（利用側は ``comken.exceptions`` から import する）。
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
    "SoqlColumnNameConflictError",
    "SoqlCountMismatchError",
    "ColumnNamesCSVFormatError",
    "SoqlTransformError",
    "SoqlIncompleteError",
    "EmptyReportError",
    "ReportFolderNotFoundError",
    "ScheduledDownloadFailedError",
    "BrowserFallbackFailedError",
    "BrowserFallbackIncompleteError",
    "SavedFileEmptyError",
    "MasterTableError",
    "MasterRowValueError",
    "MasterDuplicateValueError",
]


class ReportNotRegisteredError(DownloaderError):
    """指定した管理番号が管理表に無い

    管理番号はコードに定数で書く（CUSTOMER_LIST = "1001"）。管理表から行を消したり、
    番号を打ち間違えたりすると、どのレポートを指しているか決められない。

    発生箇所: download_scheduled() の `_validate_filters_by_report()`

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

    出力先フォルダの「グループ→ベースパス」は設定シート（管理表と同じブック内）で管理する。
    管理表にないグループ名が書かれていると、出力先を決められない。

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
    列だけ「○」にして ``SoqlReport`` の追加を忘れると、どの SOQL を使えばいいか決められない。

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


class SoqlColumnNameConflictError(DownloaderError):
    """``COLUMN_NAMES`` の置き換えで、出力 CSV の見出しが 2 つ以上重なった

    ``runner.apply_column_names()`` は ``SoqlReport.COLUMN_NAMES`` で結果列（API 名）を
    人が読む見出しへ変える。複数の列を同じ見出しに割り当てると同じ列名が 2 回現れるため、
    **そのレポートは失敗扱い**（ファイルは書かない）。重なった見出しと元列をメッセージに書く。

    発生箇所: ``src.soql_reports.runner`` の ``apply_column_names()``

    対処:
        ``reports/<ファイル>.py`` の ``COLUMN_NAMES`` を見直す。重なった
        見出しに割り当てている列の片方を、別の見出しに分ける。 ``soql()``
        側の ``SELECT`` に無い列は ``COLUMN_NAMES`` に残っていても無視される
    """

    def __init__(self, report_key: str, conflicts: dict[str, list[str]]) -> None:
        lines = [f"COLUMN_NAMES の置き換えで見出しが衝突しました: {report_key}"]
        for header, sources in conflicts.items():
            joined = "、".join(sources)
            lines.append(f"  「{header}」: {joined}")
        lines.append("COLUMN_NAMES を見直して、衝突している列を別の見出しに分けてください。")
        super().__init__("\n".join(lines))


class SoqlCountMismatchError(DownloaderError):
    """SOQL で取った件数が、 レポート API で取った件数と合わない

    SOQL は人や AI が ``SoqlReport.soql()`` に書くため、条件の書き間違いでレポートと
    違う範囲を取ることがある。件数は **取得のたびに** 比較し、**合わなければ保存しない**。
    件数一致は中身の一致を意味しない。0 件同士は一致扱い（``ALLOW_EMPTY`` の判定はその後）。

    発生箇所: ``src.service`` の ``fetch_via_soql()``（``bulk_query()`` 直後に
    ``Report API`` で総件数を取って比較する）

    対処:
        ``SoqlReport.soql()`` の条件がレポートの条件と一致しているか確認する。
        取得の間に Salesforce 側のデータが変わった場合は、次回の取得で一致
        することがある
    """

    def __init__(
        self,
        report_key: str,
        summary: str,
        soql_count: int,
        report_count: int,
    ) -> None:
        super().__init__(
            f"SOQL の件数とレポートの総件数が一致しませんでした: {report_key}（{summary}）\n"
            f"SOQL の件数: {soql_count}\n"
            f"レポートの総件数: {report_count}\n"
            "SOQL の条件がレポートと同じか確認してください。"
            "データが取得の間に変わった場合は次回の取得で一致します。"
        )


class ColumnNamesCSVFormatError(DownloaderError):
    """``列名の対応.csv`` が読めない／必要な列が無い

    ``soql-collector`` が出す ``列名の対応.csv``（UTF-8 BOM 付き CRLF。列は
    ``主オブジェクト, SOQL列名, 表示名, 備考, 更新日``）を
    ``column_names.load_column_names_for()`` が読む。読めない・必要な列が無い・見出しだけ
    の場合は **失敗として止める**（ファイルが無い／該当行が無いのは WARNING だけで続行）。

    発生箇所: ``src.soql_reports.column_names`` の ``_read_rows()``

    対処:
        ``列名の対応.csv`` を ``soql-collector`` で作り直す。フォーマットが
        正しいか（UTF-8 BOM 付き CRLF、列名が ``主オブジェクト, SOQL列名,
        表示名, 備考, 更新日``）を確認する
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class SoqlTransformError(DownloaderError):
    """``SoqlReport.transform()`` の実行中に例外が起きた／戻り値が Table でない

    ``transform()`` は取得した Table を加工して保存用の Table を返す関数。人が書く Python
    なので ``KeyError`` など ``ComkenError`` 系でない例外が出やすく、そのままでは 1 本の
    ミスで他のレポートの取得が止まる。そこで中で起きた例外は ``DownloaderError`` 系に
    包み直して、取得フローがまとめて拾えるようにする（``from exc`` で ``__cause__`` に残す）。
    戻り値が ``Table`` でない場合も同じ例外にする。

    メッセージには **行の値は出さない**（元の例外クラス名と失敗した事実だけ）。

    発生箇所: ``src.service.fetch_via_soql()``（加工前の Table に対して呼ばれる）。

    対処:
        ``reports/<ファイル>.py`` の ``transform()`` を見直す。 ``SoqlReport``
        メッセージの ``KEY`` と ``__cause__`` を辿って、どこで何が起きたか確認する。
    """

    def __init__(
        self,
        report_key: str,
        summary: str,
        report_class_name: str,
        exc_type_name: str,
        *,
        reason: str = "加工中にエラーが発生しました",
    ) -> None:
        super().__init__(
            f"SOQL の transform() で{reason}: 管理番号={report_key}（{summary}）"
            f", クラス={report_class_name}, 種類={exc_type_name}"
        )


class SoqlIncompleteError(DownloaderError):
    """soql-collector の下書きに未完成の印（``【未完成】``）が残っている

    soql-collector は補えなかった部分を ``【未完成】`` で始まる行として残す。その印が
    1 行目（前後の空白を除く）にある SOQL は、Salesforce に問い合わせる前に止める。
    本文の途中に出てくるだけなら止めない。

    印の文字列は ``src.service.INCOMPLETE_SOQL_MARKER`` で、soql-collector 側の
    ``INCOMPLETE_MARKER`` と同じ。変えるときは両方を同時に変える。

    発生箇所: ``src.service.fetch_via_soql()`` （``bulk_query()`` を呼ぶ前）

    対処:
        soql-collector の下書きに作れなかった部分が残っています。
        SOQL 資料の「作れなかった部分」を補い、 1 行目の ``【未完成】`` の行を
        消してから使ってください。
    """

    def __init__(self, report_key: str, summary: str) -> None:
        super().__init__(
            f"SOQL の下書きに未完成の印があります: 管理番号={report_key}（{summary}）\n"
            "soql-collector の下書きに作れなかった部分が残っています。"
            "SOQL資料の『作れなかった部分』を補い、"
            "1 行目の【未完成】の行を消してから使ってください。"
        )


class EmptyReportError(DownloaderError):
    """レポートは実行できたが明細が 0 行だった

    空のファイルを置くと、使う側は「データが無い日」と「取得が失敗した日」を
    区別できなくなる。0 行のときはファイルを作らず、失敗として扱う。

    発生箇所: download_scheduled() の `save_report()`

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

    発生箇所: download_scheduled() の `require_folder()`

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
    でブラウザ経由に取り直そうとしたが、``fetch_via_browser()`` 中に別の例外が出た
    ときに送出する。``DownloaderError`` 系なので ``_failure_row()`` で「Salesforce」区分になる。

    ``route`` は ``ROUTE_BROWSER_FALLBACK_TRUNCATED`` / ``ROUTE_BROWSER_FALLBACK_EMPTY``
    のいずれかで、履歴の「取得経路」列に書くために持つ。
    ``SalesforceReportTruncatedError`` のような 2 引数シグネチャの例外は経由しない
    （文字列 1 つで ``raise`` すると ``TypeError`` になるため）。

    発生箇所: ``src/service.py`` の ``fetch_with_auto_fallback()`` と
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
        # ``_download()`` が ``record_failure()`` の ``route`` に転写する
        self.route = route


class BrowserFallbackIncompleteError(BrowserFallbackFailedError):
    """Report API では2000件超だったのに、ブラウザで取り直した結果も2000行以下だった

    ``SalesforceReportTruncatedError`` で自動的にブラウザ経由に取り直したが、
    **ブラウザでも Report API の上限（2000行）以下しか取れなかった**場合に送出する。
    管理表の「0件あり」が ○ でも空ファイルは作らずエラー扱いにする（「データが無い日」と
    「取得が失敗した日」を区別できなくなるため）。

    ``self.route`` には ``ROUTE_BROWSER_FALLBACK_TRUNCATED`` を入れる。親の
    ``__init__`` の文言は「ブラウザ取得自体が失敗」用で合わないため、呼ばずに
    ``DownloaderError.__init__`` に自前の文言を渡す。

    発生箇所: ``src/service.py`` の ``fetch_with_auto_fallback()``

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
        # 親の ``__init__`` を呼ばないので ``self.route`` はここで入れる
        self.route = ROUTE_BROWSER_FALLBACK_TRUNCATED


class SavedFileEmptyError(DownloaderError):
    """保存したはずのファイルが 0 バイトだった

    保存先（ファイルサーバー）の書き込みは手元でバッファしてから送られるため、
    クォータ不足で中身が捨てられても Python 側ではエラーにならない。
    空のファイルを成功扱いにしないための検査。``DownloaderError`` 系なので
    ``_failure_row()`` で「Salesforce」区分になる。

    発生箇所: ``src/service.py`` の ``save_report()``

    対処:
        保存先フォルダの空き容量・クォータが足りているか確認する（古い取得
        ファイルを整理するか、情報システムの担当にクォータを増やしてもらう）。
        急ぐときは Salesforce の画面から手動でエクスポートする。
    """

    def __init__(self, report_key: str, summary: str, path: Path, row_count: int) -> None:
        super().__init__(
            f"保存したファイルが 0 バイトでした（{row_count} 行を書き込んだはず）: "
            f"{report_key}（{summary}） {path}\n"
            "対処: 保存先フォルダの空き容量・クォータが足りているか確認してください"
            "（古い取得ファイルを整理するか、情報システムの担当にクォータを増やしてもらう）。"
            "急ぐときは Salesforce の画面から手動でエクスポートしてください。"
        )


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
