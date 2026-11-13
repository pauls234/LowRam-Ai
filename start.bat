@echo off
rem Start LowRAM AI Studio from the source folder (Windows).
rem First run: creates .venv with Python 3.10+ and installs the requirements.
rem Later runs: reinstalls only when a requirements file (or Python) changed.
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "STAMP=.venv\.lowram-requirements.sha256"
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"

if exist "%VENV_PY%" (
  "%VENV_PY%" -c "%CHECK%" >nul 2>nul && goto :venv_ready
)

set "PY="
py -3 -c "%CHECK%" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "%CHECK%" >nul 2>nul && set "PY=python"
)
if not defined PY goto :no_python

echo Setting up LowRAM AI (first start, takes a minute)...
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv
if errorlevel 1 (
  echo Could not create the Python environment in "%CD%\.venv".
  goto :fail
)

:venv_ready
for /f "usebackq delims=" %%H in (`"%VENV_PY%" -c "import hashlib, sys; h = hashlib.sha256(sys.version.encode()); [h.update(open(n, 'rb').read()) for n in ('requirements.txt', 'requirements-desktop.txt')]; print(h.hexdigest())"`) do set "HASH=%%H"
set "OLD="
if exist "%STAMP%" set /p OLD=<"%STAMP%"
if not "%OLD%"=="%HASH%" (
  echo Installing LowRAM AI's components ^(only needed after an update^)...
  "%VENV_PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
  "%VENV_PY%" -m pip install --disable-pip-version-check --quiet -r requirements.txt -r requirements-desktop.txt
  if errorlevel 1 (
    echo Installing the components failed. Check your internet connection and try again.
    goto :fail
  )
  >"%STAMP%" echo %HASH%
)

"%VENV_PY%" -m desktop.launcher %*
if errorlevel 1 goto :fail
exit /b 0

:no_python
echo.
echo LowRAM AI needs Python 3.10 or newer, which was not found on this computer.
echo Install it from https://www.python.org/downloads/windows/
echo (tick "Add python.exe to PATH" in the installer), then start LowRAM AI again.
start "" "https://www.python.org/downloads/windows/"

:fail
echo.
pause
exit /b 1
