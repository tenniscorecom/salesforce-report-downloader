r"""src/service/scheduler.py — 定期取得の対象選定と共有レポート警告。

``download_scheduled()`` から呼ばれる「対象選定」ヘルパーを置く。

    from src.service.scheduler import (
        select_targets,
        _matched_schedule_key,
        warn_shared_reports,
    )

``_matched_schedule_key()`` は ``entry`` のスケジュール行を見て「今取るべきか」
を返す。``comken.services.salesforce_downloader.history.schedule_succeeded_today()``
を呼ぶため、テストは ``monkeypatch.setattr(history, "schedule_succeeded_today", ...)``
で差し替える運用（= 同じモジュール属性の差し替え）。
"""

from __future__ import annotations

import datetime as dt
import logging

from comken.services.salesforce_downloader import history

from src.sheets.master import ReportEntry, shared_report_ids
from src.sheets.schedule import ScheduleRule

logger = logging.getLogger("src.service")


def warn_shared_reports(entries: dict[str, ReportEntry]) -> None:
    """同じ Salesforce レポートを複数の管理番号が指していればログに出す。

    エラーにはしない（意図している場合もある）。**気づけるようにするだけ**。
    """
    for report_id, keys in shared_report_ids(entries).items():
        logger.info(
            "同じ Salesforce レポートを %d 件の管理番号が指しています: %s（%s）",
            len(keys),
            "、".join(str(key) for key in keys),
            report_id,
        )


def _matched_schedule_key(
    entry: ReportEntry,
    rules_by_report: dict[str, list[ScheduleRule]],
    current: dt.datetime,
    history_path,
) -> tuple[bool, str, str]:
    """このレポートを今取得すべきか、すべきなら根拠のスケジュールキーを返す。

    戻り値は ``(取得すべきか, スケジュールキー, 外した理由)``。
    スケジュールキーは、取得すべきだった場合に「どのスケジュール行が根拠になったか」を
    表し、履歴の ``スケジュールキー`` 列にそのまま記録する。取得しないときは
    ``(False, "", 理由)`` を返し、第3要素で外した理由を返す（外さないときは空文字）。
    理由は「予定外」/「当日取得済」の2値で、``select_targets()`` の内訳ログに使う
    （両方「スケジュール行は有るが今回は取らない」ケースの細分）。

    判定ロジック:

    - スケジュール行が無いレポートは取得しない（``(False, "", "")`` を返す）。
      「スケジュール」シートに紐付かないレポートは黙って取らない（気づけるよう、
      ``select_targets()`` 側で 1 実行 1 回のログを出す）
    - スケジュール行がある場合、いずれかの行が ``is_due()`` True で、かつ
      ``schedule_succeeded_today()`` が False（=今日まだ成功していない）なら取得。
      複数の行が True を返す場合は取得時刻が一番遅い行のキーを採用（それより早い
      時刻の行は無視する）。``start_time is None`` の行は最も早い扱いとし、具体的な
      時刻を持つ行がある限りそちらを優先する
    - いずれの行も ``is_due()`` False なら ``(False, "", "予定外")``
    - いずれかの行が ``is_due()`` True でも、今日すでに成功済みなら ``(False, "", "当日取得済")``

    祝日判定は ``ScheduleRule.is_due()`` が ``comken.core.dates`` の統一
    カレンダーを直接見るため、呼び出し側でカレンダーを用意する必要はない。

    ``current`` は呼び出し元で固定した基準日時。dedup 判定にも ``current.date()``
    を渡し、スケジュール判定と履歴チェックが同じ「日」を見るようにする
    （23:59 をまたぐ実行で「判定は開始日・履歴は翌日」となる事故を防ぐ）。

    ``history_path`` は呼び出し時点で ``comken.services.salesforce_downloader.paths``
    から読んだパスを受け取る形にした（以前の ``src.service._history_path()`` は
    パッケージ ``__init__`` 側に置いた）。
    """
    # スケジュール行が無いレポートは ``due_rules`` が空になり、取らない。警告ログは
    # ``select_targets()`` 側で出す。 ``is_due()`` と ``rules_by_report.get()`` は
    # ここで 1 回ずつだけ評価し、 戻り値（ ``due_rules`` ）から後の判定を組み立てる
    # ——元の実装は ``due_rules`` のリスト内包と末尾の ``any(...)`` で同じ評価を
    # 2 回ずつ走らせていた
    rules = rules_by_report.get(entry.key, ())
    due_rules: list[ScheduleRule] = [rule for rule in rules if rule.is_due(current)]
    due_and_unsucceeded = [
        rule
        for rule in due_rules
        if not history.schedule_succeeded_today(
            history_path, rule.schedule_key, date=current.date()
        )
    ]
    if due_and_unsucceeded:
        # ``start_time is None`` の行は具体的な時刻より優先度が低い（時刻条件なしの行で
        # 取得すると、後の時刻の行を再評価する余地がなくなるため）。
        latest = max(due_and_unsucceeded, key=lambda rule: rule.start_time or dt.time.min)
        return True, latest.schedule_key, ""
    # 取らない理由を「予定外」と「当日取得済」に分ける。後者は is_due は真だが
    # 同日内に既に成功履歴があるケースで、再実行で黙って取られなくなる事故の兆候を
    # 拾えるように区別する。 is_due は上で 1 回だけ評価済み
    if due_rules:
        return False, "", "当日取得済"
    return False, "", "予定外"


def select_targets(
    entries: dict[str, ReportEntry],
    rules_by_report: dict[str, list[ScheduleRule]],
    current: dt.datetime,
    history_path,
) -> tuple[
    list[tuple[ReportEntry, str, ScheduleRule | None]],
    list[str],
    dict[str, list[str]],
]:
    """定期取得の対象を「有効」かつ「取得すべき」レポートに絞る。

    ``download_scheduled()`` から対象選定ロジックだけを抜き出したヘルパー。
    関数本体が複雑にならないように分離している（``download_scheduled`` 自体は
    既に10近くの分岐があり、複雑度の上限に近い）。

    **自動切替で再問い合わせする設計。** Report API が 2000件超で打ち止められたら
    自動でブラウザ経由に取り直すので、当日中に ``SalesforceReportTruncatedError``
    で失敗したレポートも再問い合わせする。同じ日の 2 回目の実行でも
    Report API → ブラウザ自動切替が動くので、毎回 Salesforce への問い合わせ
    を行う。
    戻り値の ``already_failed`` は常に空リスト。

    **「スケジュール」シートに行が無いレポートは取得しない。**
    ``_matched_schedule_key()`` が ``(False, "", ...)`` を返し、ここでは対象外になる。
    ただし「有効」なレポートのうちスケジュール行が 1 つも無いものがあれば、
    1 回の実行につき警告ログを 1 件出す（黙って取らなくなると気づけないため）。
    対象は「有効」なレポートだけ（``enabled=False`` は対象外）。

    戻り値の ``targets`` は ``(ReportEntry, schedule_key, ScheduleRule | None)``
    の3要素タプル。``ScheduleRule`` を一緒に運ぶのは、唯一の出力ファイル名に
    スケジュール時刻を埋め込むために ``start_time`` が必要だから。
    ``_matched_schedule_key()`` が ``True`` を返すケースは
    ``rules_by_report`` に 1 つ以上の ``ScheduleRule`` があるときだけなので、
    この関数が作る ``targets`` の ``ScheduleRule`` は必ず ``None`` 以外。

    戻り値の ``skipped_by_reason`` は「対象外にした管理番号」を理由ごとに分けた
    ``dict``。キーは「有効×」「スケジュール行なし」「予定外」「当日取得済」の
    4 値で、値は該当する管理番号のリスト。呼び出し側で 1 実行 1 行の INFO
    内訳ログと、1 件ごとの DEBUG ログに使う。

    Returns:
        ``(targets, already_failed, skipped_by_reason)``。``targets`` は実際に
        取得を試みる ``(ReportEntry, schedule_key, ScheduleRule | None)`` のリスト。
        ``already_failed`` は空（``_download_scheduled_locked()`` の呼び出し
        シグネチャを保つために残してある）。``skipped_by_reason`` は対象外理由を
        管理番号で引ける ``dict``。
    """
    targets: list[tuple[ReportEntry, str, ScheduleRule | None]] = []
    unscheduled_enabled: list[str] = []
    skipped: dict[str, list[str]] = {
        "有効×": [],
        "スケジュール行なし": [],
        "予定外": [],
        "当日取得済": [],
    }
    for entry in entries.values():
        if not entry.enabled:
            # 「有効」× のレポートは対象外。理由別の内訳にも積む
            skipped["有効×"].append(entry.key)
            continue
        rules = rules_by_report.get(entry.key)
        if not rules:
            # 「有効」だがスケジュール行が無い → 取らないが、気づけるよう集める
            unscheduled_enabled.append(entry.key)
            skipped["スケジュール行なし"].append(entry.key)
            continue
        is_due, schedule_key, skip_reason = _matched_schedule_key(
            entry, rules_by_report, current, history_path
        )
        if not is_due:
            # 予定外 / 当日取得済 のどちらかを ``skip_reason`` で返す。
            # 旧コードは区別していなかったが、内訳ログで「2 回実行しても
            # 取れていない」を見える化したいため、ここで分ける
            skipped[skip_reason].append(entry.key)
            continue
        # ``schedule_key`` は取得後に履歴へ記録し、``schedule_succeeded_today()``
        # が再判定に使う
        matched_rule: ScheduleRule | None = None
        for rule in rules:
            if rule.schedule_key == schedule_key:
                matched_rule = rule
                break
        targets.append((entry, schedule_key, matched_rule))
    if unscheduled_enabled:
        logger.warning(
            "スケジュール行が無いため取得しません: %s"
            "（管理表の「スケジュール」シートに行を追加してください）",
            "、".join(unscheduled_enabled),
        )
    return targets, [], skipped
