# -*- mode: python ; coding: utf-8 -*-
# Сборка: pyinstaller --noconfirm odaa-backup.spec
#
# Программа собирается ПАПКОЙ (onedir), а не одним .exe: такие сборки гораздо реже
# ошибочно считаются вирусами (Chrome, Защитник Windows), и они быстрее запускаются.
# В папке два .exe с общими файлами:
#   odaa-backup.exe     — с окном консоли (двойной щелчок)
#   odaa-backup-bg.exe  — без окна (его запускает Планировщик Windows)

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    datas=[("config.example.yaml", "."), ("odaa_backup/static", "odaa_backup/static")],
    hiddenimports=["keyring.backends.Windows"],
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_console = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="odaa-backup", console=True, upx=False,
)
exe_background = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="odaa-backup-bg", console=False, upx=False,
)
coll = COLLECT(
    exe_console, exe_background, a.binaries, a.datas,
    upx=False, name="ODAA Backup",
)
