@echo off
rem Сборка odaa-backup.exe и odaa-backup-bg.exe (для Планировщика, без окна).
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
pyinstaller --noconfirm --clean --onefile --console --name odaa-backup ^
  --add-data "config.example.yaml;." --add-data "odaa_backup/static;odaa_backup/static" --hidden-import keyring.backends.Windows launcher.py || goto :error
pyinstaller --noconfirm --onefile --windowed --name odaa-backup-bg ^
  --add-data "config.example.yaml;." --add-data "odaa_backup/static;odaa_backup/static" --hidden-import keyring.backends.Windows launcher.py || goto :error
copy /y config.example.yaml dist\ >nul
copy /y README.md dist\ >nul
echo.
echo Готово! Программа в папке dist\: odaa-backup.exe, odaa-backup-bg.exe
echo Скопируйте всю папку dist в удобное место, например C:\OdaaBackup
pause
exit /b 0
:error
echo.
echo ОШИБКА СБОРКИ — см. сообщения выше.
pause
exit /b 1
