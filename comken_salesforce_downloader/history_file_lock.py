"""comken_salesforce_downloader/history_file_lock.py — 履歴CSVの排他制御。"""

import msvcrt
import time
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from comken_salesforce_downloader.exceptions import HistoryLockTimeoutError

LOCK_TIMEOUT_SECONDS = 10.0
LOCK_RETRY_SECONDS = 0.05


class HistoryFileLock:
    """Windows のファイルロックで、履歴CSVの読み書きを別プロセス間で直列化する。

    ロック用ファイル自体は残す。ロックはファイルハンドルに結び付くため、プロセスが
    異常終了しても Windows が解放する。共有サーバー上でも同じパスを使うプロセス同士が
    同じ1バイトをロックすることで、見出し作成と1行追記をひとまとまりに保つ。
    """

    def __init__(self, history_path: str | Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self._path = Path(f"{Path(history_path)}.lock")
        self._timeout = timeout
        self._file: BinaryIO | None = None

    def __enter__(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._path.open("a+b")
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                self._file = lock_file
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    lock_file.close()
                    raise HistoryLockTimeoutError(self._path, self._timeout) from exc
                time.sleep(LOCK_RETRY_SECONDS)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._file.close()
            self._file = None

