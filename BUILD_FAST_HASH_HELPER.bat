@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo === Build Fast Hash Helper compatibility launcher ===
echo This version uses the Native C helper:
echo   tools\cd_hashlittle_native.exe
echo.
echo BUILD_FAST_HASH_HELPER.bat is kept for compatibility with older instructions.
echo It will call BUILD_NATIVE_C_HELPER.bat.
echo.

if not exist "BUILD_NATIVE_C_HELPER.bat" (
  echo ERROR: BUILD_NATIVE_C_HELPER.bat was not found.
  exit /b 1
)

call BUILD_NATIVE_C_HELPER.bat
exit /b %ERRORLEVEL%
