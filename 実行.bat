@echo off
setlocal
rem このツールの起動用。ダブルクリックで main.py を実行します。
rem comken を別の場所へ移したときは、ここと .vscode\settings.json の両方を直してください。

set "COMKEN_ROOT=F:\dev\original_libs"
set "PYTHONPATH=%COMKEN_ROOT%;%PYTHONPATH%"

rem 共有フォルダ（\\サーバー名\...）から起動されても動くよう pushd を使う（cd は UNC 不可）
pushd "%~dp0"
python main.py
if errorlevel 1 pause
popd
endlocal
