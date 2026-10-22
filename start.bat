@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title LawBoard - do not close
set PY=
py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY python -c "import sys" >nul 2>nul && set PY=python
if not defined PY (
  echo  Python was not found. Run LawBoard-Setup.bat again.
  pause
  exit /b 1
)
start "" http://127.0.0.1:8780
:run
%PY% server.py
if %errorlevel%==3 goto run
pause
