@echo off
REM Starts the brainrot bot and restarts it automatically if it ever crashes.
REM Double-click this file, or put a shortcut to it in your Startup folder (Win+R, shell:startup).
cd /d "%~dp0"
title Brainrot bot

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

:loop
%PY% brainrot.py %*
set "CODE=%errorlevel%"
REM 0 = finished, 2 = setup problem, 3 = already running, 130 = you stopped it
if "%CODE%"=="0" goto end
if "%CODE%"=="2" goto end
if "%CODE%"=="3" goto end
if "%CODE%"=="130" goto end
echo.
echo The bot stopped unexpectedly (exit code %CODE%). Restarting in 10 seconds... (close this window to quit)
timeout /t 10 /nobreak >nul
goto loop

:end
echo.
pause
