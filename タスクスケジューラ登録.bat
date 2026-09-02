@echo off
setlocal

rem ─── タスクスケジューラ登録.bat ──────────────────────────────────────────
rem
rem このプロジェクトの 実行.bat を、Windows タスクスケジューラに
rem 高頻度（既定 1 時間おき）で定期実行するタスクとして登録する。
rem
rem 使い方:
rem   1. この .bat を右クリック → 「管理者として実行」
rem      （schtasks /create は管理者権限が必要です）
rem   2. 「登録しました」と出れば完了。タスクは次の実行予定時刻から動きます
rem
rem 登録内容の確認:
rem   schtasks /query /tn "%TASK_NAME%"
rem
rem 登録したタスクを削除したいとき:
rem   schtasks /delete /tn "%TASK_NAME%" /f
rem
rem 開発機では登録しない運用です（共有サーバーへ配置したあと、会社PCで実行する）。
rem あくまで登録用 .bat を置いておくだけで、本スクリプト自体は登録動作をしません。

rem ▼ ここを変えれば間隔や登録内容を調整できる ──────────────────────
set "TASK_NAME=Salesforceレポートダウンローダー_定期取得"
set "INTERVAL_HOURS=1"
set "RUN_BAT=%~dp0実行.bat"
set "TASK_DESC=comken 経由で Salesforce レポートを定期取得するバッチ（download_scheduled）。内部でスケジュール判定と重複防止をするため、このタスクを高頻度で動かしても二重取得は起きない。"
rem ▲ ────────────────────────────────────────────────────────────

rem ── 管理者権限で実行されたか確認 ────────────────────────────────
net session >nul 2>&1
if errorlevel 1 (
    echo このスクリプトは「管理者として実行」してください。
    pause
    exit /b 1
)

rem ── 同じ名前のタスクが既にあれば削除してから作り直す（二重登録を避ける） ──
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if not errorlevel 1 schtasks /delete /tn "%TASK_NAME%" /f >nul

rem ── タスクを登録する ───────────────────────────────────────────
rem /sc hourly /mo N で「N 時間おき」を指定する（既定 1）。
rem /F を付けると既存があれば上書きされるが、ここでは明示的に削除してから作るので
rem /F は付けない（誤解を避けるため）。
schtasks /create ^
    /tn "%TASK_NAME%" ^
    /tr "\"%RUN_BAT%\"" ^
    /sc hourly ^
    /mo %INTERVAL_HOURS% ^
    /rl highest ^
    /ru "%USERNAME%" ^
    /description "%TASK_DESC%"

if errorlevel 1 (
    echo タスクの登録に失敗しました。schtasks のエラーメッセージを確認してください。
    pause
    exit /b 1
)

echo タスク "%TASK_NAME%" を %INTERVAL_HOURS% 時間おきで登録しました。
echo 内容を確認する場合: schtasks /query /tn "%TASK_NAME%"
echo 削除する場合:        schtasks /delete /tn "%TASK_NAME%" /f
pause
endlocal