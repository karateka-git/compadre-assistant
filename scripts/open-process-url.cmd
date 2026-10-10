@echo off
if "%~1"=="" (
    echo Usage: %~nx0 URL
    echo Example: %~nx0 "https://habr.com/ru/articles/198682/"
    pause
    exit /b 1
)

start "Compadre Assistant process-url" powershell.exe -NoExit -ExecutionPolicy Bypass -File "%~dp0process-url.ps1" "%~1"
