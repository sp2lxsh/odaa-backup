"""Точка входа для PyInstaller (odaa-backup.exe / odaa-backup-bg.exe)."""

import sys

from odaa_backup.cli import main

if __name__ == "__main__":
    sys.exit(main())
