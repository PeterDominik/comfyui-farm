@echo off
rem Run the gateway in this console (Ctrl+C to stop). For always-on use, install the service.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.ps1 first.
  exit /b 1
)
".venv\Scripts\python.exe" -m comfyfarm serve %*
