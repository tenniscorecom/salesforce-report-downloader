"""comken_salesforce_downloader/exceptions/master_table.py — Excel の表を設定として読むときの例外。

非エンジニアが編集する表なので、**どの行のどの列が、なぜ駄目なのか**を必ず示す。
"""

from pathlib import Path

from comken.exceptions import ComkenError


class MasterTableError(ComkenError):
    """Excel の管理表に関するエラー

    対処:
        画面に表示された具体的なエラー名を上の表から探す
    """


class MasterSheetNotDefinedError(MasterTableError):
    """管理表の場所が決まっていない

    `load()` を引数なしで呼ぶには、クラス変数 `PATH` に既定の場所を書いておく必要がある。

    発生箇所: comken_salesforce_downloader.report_master の load()

    対処:
        `load(パス)` のようにファイルを渡すか、クラスに PATH を書く（コードの直し方の話なので、
        非エンジニアが見た場合は管理者へ連絡する）
    """

    def __init__(self, class_name: str) -> None:
        super().__init__(
            f"{class_name} に管理表の場所が指定されていません。\n"
            "load(パス) でファイルを渡すか、クラス変数 PATH を書いてください。"
        )


class MasterColumnNotFoundError(MasterTableError):
    """管理表に必要な列（見出し）が無い

    見出しの行を書き換えた・列を消した・別のシートを見ている、のいずれか。
    **プログラムは見出しの名前で列を探す**ので、見出しが変わると読めなくなる。

    発生箇所: comken_salesforce_downloader.report_master の load()

    対処:
        管理表の1行目（見出し）を元に戻す。消してしまった場合は、
        メッセージに出ている「今ある見出し」と見比べて足す
    """

    def __init__(self, header: str, existing: list[str], path: Path, sheet_name: str) -> None:
        known = "、".join(str(name) for name in existing) or "（見出しなし）"
        super().__init__(
            f"管理表に「{header}」の列がありません: {path}（シート: {sheet_name}）\n"
            f"今ある見出し: {known}\n"
            "1行目の見出しは変えないでください。"
        )


class MasterRowValueError(MasterTableError):
    """管理表の値が正しくない

    数字を書く列に文字が入っている、決まった書き方以外を書いた、空にできない列が空、など。

    発生箇所: comken_salesforce_downloader.report_master の load()

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

    発生箇所: comken_salesforce_downloader.report_master の load()

    対処:
        管理表を開いて、重複している値のどちらかを別の値に変える
    """

    def __init__(self, header: str, value: object, path: Path) -> None:
        super().__init__(
            f"管理表の「{header}」に同じ値が2つあります: {value!r}\n"
            f"{path}\n"
            "この列は1つに決まる必要があるため、どちらかを変えてください。"
        )

