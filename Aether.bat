@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run Install-Aether.bat first.
  pause
  exit /b 1
)
if exist "%~dp0Aether.vbs" (
  wscript.exe //nologo "%~dp0Aether.vbs"
  exit /b 0
)
start "" ".venv\Scripts\pythonw.exe" desktop.py
