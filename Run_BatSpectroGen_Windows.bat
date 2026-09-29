@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo BatSpectroGen is not installed yet.
  echo Run Install_BatSpectroGen_Windows.bat first.
  pause
  exit /b 1
)

.venv\Scripts\python.exe BatSpectroGen.py
if errorlevel 1 pause
