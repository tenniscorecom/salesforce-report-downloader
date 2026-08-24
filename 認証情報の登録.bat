@echo off
setlocal
rem 認証情報（client_secret・パスワード・トークン）の登録画面を開きます。
rem ダブルクリックで起動してください。入れた値は Windows の DPAPI で暗号化して保存され、
rem 登録した Windows ユーザー・その PC でしか読めません。
rem comken を別の場所へ移したときは、ここと 実行.bat・.vscode\settings.json を直してください。

set "COMKEN_ROOT=F:\dev\comken"
set "PYTHONPATH=%COMKEN_ROOT%;%PYTHONPATH%"

rem 共有フォルダ（\\サーバー名\...）から起動されても動くよう pushd を使う（cd は UNC 不可）
pushd "%~dp0"
python -m comken.credentials gui
if errorlevel 1 pause
popd
endlocal
