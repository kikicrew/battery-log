@echo off
rem exe 만들기: dist\BatteryLog\BatteryLog.exe (먼저 py -m pip install pyinstaller)
cd /d "%~dp0"
py battery_log.py --test || exit /b 1
py -m PyInstaller --noconfirm --onedir --noconsole --name BatteryLog --icon battery.ico --add-data "battery.ico;." battery_log.py
