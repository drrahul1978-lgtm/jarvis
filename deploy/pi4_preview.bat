@echo off
REM Preview the Raspberry Pi 4 configuration on a Windows PC.
REM
REM Runs the same small model, the same short context, and the same
REM reasoning-off setting the Pi 4 installer applies. Use it to judge the
REM ANSWERS before you commit to the hardware.
REM
REM It cannot tell you about SPEED. Your desktop has a GPU and a far faster
REM processor; a Pi 4 does the same work on four slow cores. Expect this to be
REM roughly twenty times quicker than the real thing.

chcp 65001 >nul
title Jarvis - Raspberry Pi 4 preview
cd /d "%~dp0.."

set JARVIS_MODEL=jarvis-pi4
set JARVIS_CTX=2048
set JARVIS_THINK=0
set JARVIS_MAX_TOKENS=400
set JARVIS_HISTORY_TURNS=6
set JARVIS_FACTS_IN_PROMPT=15
set JARVIS_TEMPERATURE=0.6
set JARVIS_WHISPER_MODEL=tiny.en

echo.
echo   Raspberry Pi 4 preview
echo   model %JARVIS_MODEL%, %JARVIS_CTX%-token context, reasoning off
echo   Answers match the Pi. Speed does not - this machine is far faster.
echo.

python jarvis.py %*
echo.
pause
