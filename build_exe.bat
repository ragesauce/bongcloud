@echo off
cd /d "%~dp0"
python -m PyInstaller Bongcloud.spec --noconfirm
