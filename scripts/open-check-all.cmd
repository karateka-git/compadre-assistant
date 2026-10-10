@echo off
start "Compadre Assistant check-all" powershell.exe -NoExit -ExecutionPolicy Bypass -File "%~dp0check-all.ps1"
