# tests/contract/

soql-collector（隣のプロジェクト）が出す `列名の対応.csv` の **読み手** 側
契約の見本ファイル。

`tests/test_contract_soql_collector.py` がこのファイルを読んで、
`src.soql_reports.column_names.load_column_names_for()` の挙動が soql-collector
の出力と一致しているかを確かめる。 soql-collector 側の
`tests/contract/列名の対応.csv` にも同じファイルがあり、両方が
**バイト単位で一致**していることを突き合わせるテストも入っている。

このファイルを変えるときは **両リポジトリの tests/contract/ の
列名の対応.contract.csv を同時に変える** こと（片方だけ変えると
突き合わせテストが落ちる）。