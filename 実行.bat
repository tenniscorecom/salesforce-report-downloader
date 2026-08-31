@echo off
setlocal
rem ���̃c�[���̋N���p�B�_�u���N���b�N�� main.py �����s���܂��B
rem comken ��ʂ̏ꏊ�ֈڂ����Ƃ��́A������ COMKEN_ROOT �� .vscode\settings.json �̗����𒼂��Ă��������B
rem comken.services.salesforce_downloader パッケージは comken 本体にあるため、PYTHONPATH への追加は不要です。

set "COMKEN_ROOT=F:\dev\comken"
set "PYTHONPATH=%COMKEN_ROOT%;%PYTHONPATH%"

rem ���L�t�H���_�i\\�T�[�o�[��\...�j����N������Ă������悤 pushd ���g���icd �� UNC �s�j
pushd "%~dp0"
python main.py
if errorlevel 1 pause
popd
endlocal
