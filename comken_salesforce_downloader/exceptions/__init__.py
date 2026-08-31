"""comken_salesforce_downloader/exceptions/__init__.py — 例外体系。

このパッケージは comken 本体から独立した「Salesforce レポートの集約ダウンローダー」専用の
例外だけをまとめる。comken 本体側の例外（`ComkenError` を基底とする）は引き続き
`from comken.exceptions import ...` で読み込む。

DownloaderError
├── HistoryWriteError
├── HistoryLockTimeoutError
├── HistoryHeaderMismatchError
├── ReportNotRegisteredError
├── InvalidReportURLError
├── ReportDisabledError
├── CachedReportNotRegisteredError
├── CachedReportNotFoundError
├── EmptyReportError
├── ReportFolderNotFoundError
├── ReportReservePathLimitError
├── ScheduledDownloadFailedError
├── UnsupportedScheduleFrequencyError
├── ScheduleIntervalMissingError
├── ScheduleRequiredValueMissingError
└── ScheduleWeekdayInvalidError

MasterTableError
├── MasterSheetNotDefinedError
├── MasterColumnNotFoundError
├── MasterRowValueError
└── MasterDuplicateValueError

カテゴリ基底クラスはまとめて捕捉するために使い、直接送出しない。
"""

from comken_salesforce_downloader.exceptions.downloader import (
    CachedReportNotFoundError,
    CachedReportNotRegisteredError,
    DownloaderError,
    EmptyReportError,
    HistoryHeaderMismatchError,
    HistoryLockTimeoutError,
    HistoryWriteError,
    InvalidReportURLError,
    ReportDisabledError,
    ReportFolderNotFoundError,
    ReportNotRegisteredError,
    ReportReservePathLimitError,
    ScheduledDownloadFailedError,
    ScheduleIntervalMissingError,
    ScheduleRequiredValueMissingError,
    ScheduleWeekdayInvalidError,
    UnsupportedScheduleFrequencyError,
)
from comken_salesforce_downloader.exceptions.master_table import (
    MasterColumnNotFoundError,
    MasterDuplicateValueError,
    MasterRowValueError,
    MasterSheetNotDefinedError,
    MasterTableError,
)

__all__ = [
    "DownloaderError",
    "HistoryWriteError",
    "HistoryLockTimeoutError",
    "HistoryHeaderMismatchError",
    "ReportNotRegisteredError",
    "InvalidReportURLError",
    "ReportDisabledError",
    "CachedReportNotRegisteredError",
    "CachedReportNotFoundError",
    "EmptyReportError",
    "ReportFolderNotFoundError",
    "ReportReservePathLimitError",
    "ScheduledDownloadFailedError",
    "UnsupportedScheduleFrequencyError",
    "ScheduleIntervalMissingError",
    "ScheduleRequiredValueMissingError",
    "ScheduleWeekdayInvalidError",
    "MasterTableError",
    "MasterSheetNotDefinedError",
    "MasterColumnNotFoundError",
    "MasterRowValueError",
    "MasterDuplicateValueError",
]

