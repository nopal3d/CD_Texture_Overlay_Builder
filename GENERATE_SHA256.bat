@echo off
setlocal
cd /d "%~dp0"
set "EXE=dist\CD_Texture_Overlay_Builder\CD_Texture_Overlay_Builder.exe"
set "OUT=dist\CD_Texture_Overlay_Builder\SHA256SUMS.txt"
if not exist "%EXE%" (
  echo ERROR: %EXE% not found.
  exit /b 1
)
for /f "usebackq tokens=*" %%H in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-FileHash -Algorithm SHA256 '%EXE%').Hash"`) do set "HASH=%%H"
> "%OUT%" echo %HASH%  CD_Texture_Overlay_Builder.exe
echo SHA256 written to: %OUT%
type "%OUT%"
