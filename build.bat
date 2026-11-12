@echo off
rem Сборка программы в папку "dist\ODAA Backup" (odaa-backup.exe + odaa-backup-bg.exe для Планировщика).
rem Нужен установленный Python 3.12+ (https://python.org, галочка "Add python.exe to PATH").
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt || goto :error
python -m pytest -q || goto :error
pyinstaller --noconfirm --clean odaa-backup.spec || goto :error
copy /y config.example.yaml "dist\ODAA Backup\" >nul
copy /y README.md "dist\ODAA Backup\" >nul
echo.
echo Готово! Программа в папке "dist\ODAA Backup".
echo Перенесите эту папку целиком в удобное место, например C:\
pause
exit /b 0
:error
echo.
echo ОШИБКА СБОРКИ — см. сообщения выше.
pause
exit /b 1
