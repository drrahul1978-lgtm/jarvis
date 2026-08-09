@echo off
REM Launch Jarvis on Windows. Double-click this file, or run it from a terminal.
REM Starts the Ollama server first if it is not already answering, so this works
REM from a cold boot without opening the Ollama app by hand.

chcp 65001 >nul
title Jarvis
cd /d "%~dp0"

curl -s -o nul --max-time 2 http://127.0.0.1:11434/api/tags
if not errorlevel 1 goto run

echo Starting Ollama...
start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve

set /a tries=0
:wait
set /a tries+=1
curl -s -o nul --max-time 2 http://127.0.0.1:11434/api/tags
if not errorlevel 1 goto run
if %tries% geq 30 goto fail
timeout /t 1 /nobreak >nul
goto wait

:fail
echo.
echo Could not reach Ollama after 30 seconds.
echo Open the Ollama app from the Start menu, then run this again.
echo.
pause
exit /b 1

:run
python jarvis.py %*
echo.
pause
