@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "dist\CD_Texture_Overlay_Builder\CD_Texture_Overlay_Builder.exe" (
  echo ERROR: Build output not found. Run BUILD_WINDOWS_EXE.bat first.
  if not defined CI pause
  exit /b 1
)
set "OUT=CD_Texture_Overlay_Builder_v1.2.5_Portable_ONEDIR.zip"
if exist "%OUT%" del "%OUT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\CD_Texture_Overlay_Builder' -DestinationPath '%OUT%' -Force"
if exist "%OUT%" (
  echo Release zip created: %OUT%
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-FileHash '%OUT%' -Algorithm SHA256 | ForEach-Object { $_.Hash + '  %OUT%' }" > "%OUT%.sha256"
  type "%OUT%.sha256"
)
if not defined CI pause
