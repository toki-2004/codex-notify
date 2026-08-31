@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 setup_notify.py
  goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
  python setup_notify.py
  goto :done
)

echo [ERROR] Python 3 not found. Please install it from https://www.python.org/downloads/ and run this file again.
pause
exit /b 1

:done
pause
