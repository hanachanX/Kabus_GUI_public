@echo off
setlocal

REM 一時環境変数（このウィンドウだけ有効）
set "KABU_API_PASSWORD=9694825a"

REM Pythonスクリプトのフルパス
set "APP=C:\Users\mu54b\Documents\python\Kabus_Gui_public\Kabus_gui_v4_1.py"

REM 実行（preset=volatile は 高ボラ と同義）
python "%APP%" 

endlocal
