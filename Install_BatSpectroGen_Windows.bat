@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
py -3.13 -V >nul 2>nul && set "PYTHON_CMD=py -3.13"
if not defined PYTHON_CMD py -3.12 -V >nul 2>nul && set "PYTHON_CMD=py -3.12"
if not defined PYTHON_CMD py -3.11 -V >nul 2>nul && set "PYTHON_CMD=py -3.11"
if not defined PYTHON_CMD py -3.10 -V >nul 2>nul && set "PYTHON_CMD=py -3.10"
if not defined PYTHON_CMD py -3.9 -V >nul 2>nul && set "PYTHON_CMD=py -3.9"
if not defined PYTHON_CMD if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set PYTHON_CMD="%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PYTHON_CMD if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set PYTHON_CMD="%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYTHON_CMD if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set PYTHON_CMD="%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYTHON_CMD if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set PYTHON_CMD="%LocalAppData%\Programs\Python\Python310\python.exe"
if not defined PYTHON_CMD if exist "%LocalAppData%\Programs\Python\Python39\python.exe" set PYTHON_CMD="%LocalAppData%\Programs\Python\Python39\python.exe"
if not defined PYTHON_CMD where python >nul 2>nul && set "PYTHON_CMD=python"

if not defined PYTHON_CMD (
  echo Python was not found.
  echo Install Python 3.9 through 3.13 from https://www.python.org/downloads/
  pause
  exit /b 1
)

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info[:2] in [(3,9),(3,10),(3,11),(3,12),(3,13)] else 1)" >nul 2>nul
if errorlevel 1 (
  echo BatSpectroGen requires Python 3.9, 3.10, 3.11, 3.12, or 3.13.
  pause
  exit /b 1
)

echo Creating the BatSpectroGen environment...
%PYTHON_CMD% -m venv .venv || goto :error
.venv\Scripts\python.exe -m pip install --upgrade pip || goto :error

.venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 9) else 1)" >nul 2>nul
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install "numpy>=1.23" "matplotlib>=3.7,<3.11" "soundfile>=0.12.1" || goto :error
) else (
  .venv\Scripts\python.exe -m pip install "numpy>=1.23,<2.1" "matplotlib>=3.7,<3.10" "soundfile>=0.12.1,<0.14" || goto :error
)

.venv\Scripts\python.exe -c "import tkinter, numpy, matplotlib, soundfile, BatSpectroGen; print('BatSpectroGen is ready.')" || goto :error
echo.
echo Setup complete. Double-click Run_BatSpectroGen_Windows.bat to start.
pause
exit /b 0

:error
echo.
echo BatSpectroGen setup did not finish. Check your internet connection and try again.
pause
exit /b 1
