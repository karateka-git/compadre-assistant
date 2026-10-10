@echo off
start "Compadre Assistant pipeline-smoke" powershell.exe -NoExit -ExecutionPolicy Bypass -File "%~dp0check-pipeline.ps1"
