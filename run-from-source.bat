@echo off
rem Запуск без сборки .exe (нужен Python 3.12+).
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
python -m odaa_backup %*
pause
