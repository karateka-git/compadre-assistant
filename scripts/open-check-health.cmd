@echo off
start "Compadre Assistant healthcheck" powershell.exe -NoExit -ExecutionPolicy Bypass -File "%~dp0check-health.ps1"
