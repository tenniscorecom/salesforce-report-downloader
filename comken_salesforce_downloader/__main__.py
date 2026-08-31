"""comken_salesforce_downloader/__main__.py — `python -m comken_salesforce_downloader` の入口。

CLI の本体は `cli.py` の `main(argv)` に置く。`__main__.py` は薄い入口に
とどめ、テストや埋め込み利用から `cli.main(argv)` を直接呼べる形を保つ。
"""

import sys

from comken_salesforce_downloader.cli import main

if __name__ == "__main__":
    sys.exit(main())

