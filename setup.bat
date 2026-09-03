@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (
  py -3 setup.py %*
  exit /b %ERRORLEVEL%
)
where python >nul 2>nul && (
  python setup.py %*
  exit /b %ERRORLEVEL%
)
echo Python 3.11+ not found. Install from https://www.python.org/downloads/ and re-run setup.bat
exit /b 1
