@echo off
setlocal
set "PYTHONUTF8=1"
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
echo 未找到 Python 3.9 或更高版本。
pause
exit /b 1

:use_py
py -3 "%~dp0codex-image2.py" configure
if errorlevel 1 pause
exit /b %errorlevel%

:use_python
python "%~dp0codex-image2.py" configure
if errorlevel 1 pause
exit /b %errorlevel%
