@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "TOOLS=%~dp0tools"
set "SRC=%TOOLS%\cd_hashlittle_native.c"
set "OUT=%TOOLS%\cd_hashlittle_native.exe"

if exist "%OUT%" (
  echo Native C hash helper already exists:
  echo %OUT%
  exit /b 0
)

if not exist "%SRC%" (
  echo ERROR: Native helper source not found:
  echo %SRC%
  exit /b 1
)

echo.
echo === Building Native C Hash Helper ===
echo Source: %SRC%
echo Output: %OUT%
echo.

REM ------------------------------------------------------------------
REM 1) Try MSVC if this script is already running inside a VS Developer
REM    Command Prompt.
REM ------------------------------------------------------------------
where cl.exe >nul 2>nul
if not errorlevel 1 (
  echo Using MSVC cl.exe from current environment...
  cl.exe /O2 /MT /nologo "%SRC%" /Fe:"%OUT%"
  if exist "%OUT%" (
    echo OK: %OUT%
    exit /b 0
  )
)

REM ------------------------------------------------------------------
REM 2) Try to locate Visual Studio with vswhere, then load vcvars64.bat.
REM ------------------------------------------------------------------
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if exist "%VSWHERE%" (
  for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSINSTALL=%%i"
  if defined VSINSTALL (
    if exist "!VSINSTALL!\VC\Auxiliary\Build\vcvars64.bat" (
      echo Using Visual Studio from: !VSINSTALL!
      call "!VSINSTALL!\VC\Auxiliary\Build\vcvars64.bat" >nul
      cl.exe /O2 /MT /nologo "%SRC%" /Fe:"%OUT%"
      if exist "%OUT%" (
        echo OK: %OUT%
        exit /b 0
      )
    )
  )
)

REM ------------------------------------------------------------------
REM 3) Try gcc/clang already on PATH.
REM ------------------------------------------------------------------
where gcc.exe >nul 2>nul
if not errorlevel 1 (
  echo Using gcc.exe from PATH...
  gcc.exe -O3 -static -s "%SRC%" -o "%OUT%"
  if exist "%OUT%" (
    echo OK: %OUT%
    exit /b 0
  )
)

where clang.exe >nul 2>nul
if not errorlevel 1 (
  echo Using clang.exe from PATH...
  clang.exe -O3 "%SRC%" -o "%OUT%"
  if exist "%OUT%" (
    echo OK: %OUT%
    exit /b 0
  )
)

REM ------------------------------------------------------------------
REM 4) Try common w64devkit locations, including your layout:
REM    C:\TempCDUMM\w64devkit
REM    and sibling folders next to this project.
REM ------------------------------------------------------------------
set "PARENT=%~dp0.."
set "GCC_CANDIDATES="
set "GCC_CANDIDATES=!GCC_CANDIDATES!;%PARENT%\w64devkit\bin\gcc.exe"
set "GCC_CANDIDATES=!GCC_CANDIDATES!;%PARENT%\w64devkit\w64devkit\bin\gcc.exe"
set "GCC_CANDIDATES=!GCC_CANDIDATES!;%~dp0w64devkit\bin\gcc.exe"
set "GCC_CANDIDATES=!GCC_CANDIDATES!;C:\TempCDUMM\w64devkit\bin\gcc.exe"
set "GCC_CANDIDATES=!GCC_CANDIDATES!;C:\TempCDUMM\w64devkit\w64devkit\bin\gcc.exe"
set "GCC_CANDIDATES=!GCC_CANDIDATES!;C:\w64devkit\bin\gcc.exe"
set "GCC_CANDIDATES=!GCC_CANDIDATES!;C:\w64devkit\w64devkit\bin\gcc.exe"

for %%G in (!GCC_CANDIDATES!) do (
  if exist "%%~G" (
    echo Using w64devkit gcc:
    echo %%~G
    "%%~G" -O3 -static -s "%SRC%" -o "%OUT%"
    if exist "%OUT%" (
      echo OK: %OUT%
      exit /b 0
    )
  )
)

echo.
echo ERROR: Could not build cd_hashlittle_native.exe.
echo.
echo Install one of these:
echo - Visual Studio / Build Tools with Desktop development with C++
echo - w64devkit extracted to C:\TempCDUMM\w64devkit or C:\w64devkit
echo.
echo Without this helper the tool still works, but PAZ hashing will be much slower.
exit /b 1
