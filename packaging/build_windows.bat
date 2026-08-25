@echo off
REM Build the Windows installer.
REM
REM   packaging\build_windows.bat
REM
REM Produces dist\IPTV-Player-1.3.0-Windows-x64-Setup.exe
REM
REM Requirements:
REM   * 64-bit Python 3.10-3.14   https://www.python.org/downloads/windows/
REM   * Inno Setup 6.3+           https://jrsoftware.org/isdl.php
REM
REM This must run ON WINDOWS. PyInstaller cannot cross-compile, so a Windows
REM .exe cannot be produced from macOS or Linux.

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo ==^> Checking Python
where python >nul 2>&1 || (echo ERROR: python not found on PATH & exit /b 1)
python -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)" || (
  echo ERROR: 64-bit Python is required ^(you appear to have 32-bit^).
  exit /b 1
)

echo ==^> Creating virtual environment
if not exist .venv (
  python -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat

echo ==^> Installing dependencies
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet || exit /b 1
python -m pip install pyinstaller Pillow --quiet || exit /b 1

echo ==^> Generating icons
python packaging\make_icons.py

echo ==^> Building the executable
if exist build rmdir /s /q build
if exist dist\IPTVPlayer rmdir /s /q dist\IPTVPlayer
python -m PyInstaller packaging\iptvplayer.spec --noconfirm --log-level WARN || exit /b 1

if not exist "dist\IPTVPlayer\IPTVPlayer.exe" (
  echo ERROR: the executable was not produced.
  exit /b 1
)

echo ==^> Smoke-testing the executable
REM --selftest exits immediately; a crash here means a missing hidden import.
start /wait "" "dist\IPTVPlayer\IPTVPlayer.exe" --selftest
if errorlevel 1 (
  echo WARNING: self-test returned a non-zero exit code.
) else (
  echo     executable starts cleanly
)

echo ==^> Building the installer
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo.
  echo Inno Setup 6 was not found.
  echo Install it from https://jrsoftware.org/isdl.php then re-run this script.
  echo The unpacked application is ready in dist\IPTVPlayer\ in the meantime.
  exit /b 1
)

"%ISCC%" packaging\windows_installer.iss || exit /b 1

echo.
echo Done. Installer written to dist\
dir /b dist\*Setup.exe
endlocal
