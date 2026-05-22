@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo === CD Texture Overlay Builder v1.2.5 ===
echo Checking Native C Hash Helper...
echo.

if not exist "tools\cd_hashlittle_native.exe" (
  call BUILD_NATIVE_C_HELPER.bat
)

if exist "tools\cd_hashlittle_native.exe" (
  set "CDOB_FAST_HASH_EXE=%~dp0tools\cd_hashlittle_native.exe"
  echo Fast helper ready: tools\cd_hashlittle_native.exe
) else (
  echo.
  echo WARNING: Fast Native C Hash Helper is missing.
  echo The app can still run, but large texture builds will be MUCH slower.
  echo.
  choice /C YN /N /M "Continue with slow Python fallback? [Y/N]: "
  if errorlevel 2 exit /b 1
)

if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements_overlay_builder.txt
python texture_overlay_builder.py
if not defined CI pause
