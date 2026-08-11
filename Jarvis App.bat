@echo off
REM Jarvis as a desktop window. Double-click this.
REM Starts Ollama first if it is not already answering.

chcp 65001 >nul
cd /d "%~dp0"

curl -s -o nul --max-time 2 http://127.0.0.1:11434/api/tags
if not errorlevel 1 goto run

start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve

set /a tries=0
:wait
set /a tries+=1
curl -s -o nul --max-time 2 http://127.0.0.1:11434/api/tags
if not errorlevel 1 goto run
if %tries% geq 30 goto run
timeout /t 1 /nobreak >nul
goto wait

:run
REM pythonw runs it without a console window behind the app.
start "" pythonw "%~dp0app.py"
