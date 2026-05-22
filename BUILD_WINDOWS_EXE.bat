@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo === CD Texture Overlay Builder v1.2.5 Compatibility / Nexus-safe build ===
echo Build mode: PyInstaller ONEDIR, windowed, no UPX, native C helper only.
echo Recommended Python: 3.12 x64 or 3.11 x64. Avoid building public releases with Python 3.14.
echo.

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.12 -c "import sys" >nul 2>nul
  if %errorlevel%==0 set "PY_CMD=py -3.12"
)
if not defined PY_CMD (
  where python >nul 2>nul
  if %errorlevel%==0 set "PY_CMD=python"
)
if not defined PY_CMD (
  echo ERROR: Python was not found. Install Python 3.12 x64 and add it to PATH.
  if not defined CI pause
  exit /b 1
)

echo Using Python command: %PY_CMD%
%PY_CMD% -c "import sys,platform; print('Python:', sys.version); print('Arch:', platform.architecture()[0]); raise SystemExit(0 if sys.version_info[:2] in [(3,11),(3,12)] else 3)"
if errorlevel 3 (
  echo.
  echo WARNING: This is not Python 3.11/3.12.
  echo Building with Python 3.14 can cause python314.dll runtime issues on some systems.
  echo For public Nexus releases, install Python 3.12 x64 and rebuild.
  echo.
  choice /C YN /N /M "Continue anyway? [Y/N]: "
  if errorlevel 2 exit /b 1
)

if not exist "tools\cd_hashlittle_native.exe" (
  call BUILD_NATIVE_C_HELPER.bat
)

if exist "tools\cd_hashlittle_native.exe" (
  echo Native C helper will be bundled.
) else (
  echo.
  echo WARNING: Native C hash helper was not built.
  echo The final app will still work, but large builds will be slow.
  echo For a public Nexus release, it is strongly recommended to include tools\cd_hashlittle_native.exe.
  echo.
  choice /C YN /N /M "Continue building without native helper? [Y/N]: "
  if errorlevel 2 exit /b 1
)

if not exist .venv (
  %PY_CMD% -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements_overlay_builder.txt

rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

set "ADD_TOOLS="
if exist "tools\cd_hashlittle_native.exe" set "ADD_TOOLS=%ADD_TOOLS% --add-data tools\cd_hashlittle_native.exe;tools"

python -m PyInstaller --noconfirm --clean --noupx --onedir --windowed --name CD_Texture_Overlay_Builder --hidden-import lz4.block --collect-binaries lz4 --collect-binaries cryptography %ADD_TOOLS% texture_overlay_builder.py
if exist "dist\CD_Texture_Overlay_Builder\CD_Texture_Overlay_Builder.exe" (
  echo.
  echo Copying Visual C++ runtime DLLs if available...
  python COPY_RUNTIME_DLLS.py
  echo.
  echo Build OK: dist\CD_Texture_Overlay_Builder\CD_Texture_Overlay_Builder.exe
  call GENERATE_SHA256.bat
  echo.
  echo IMPORTANT: Distribute the entire folder:
  echo dist\CD_Texture_Overlay_Builder\
  echo Do NOT distribute only CD_Texture_Overlay_Builder.exe.
) else (
  echo.
  echo Build failed. Check the error above.
)
if not defined CI pause
