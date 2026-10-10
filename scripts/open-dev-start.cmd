@echo off
start "Compadre Assistant dev-start" powershell.exe -NoExit -ExecutionPolicy Bypass -File "%~dp0dev-start.ps1" %*
