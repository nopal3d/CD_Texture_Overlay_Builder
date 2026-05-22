@echo off
setlocal EnableExtensions EnableDelayedExpansion

title CD Texture Overlay Builder - Nexus Safe Build

cd /d "%~dp0"

echo ============================================================
echo CD Texture Overlay Builder - Nexus-safe build
echo Build mode: PyInstaller ONEDIR, windowed, no UPX
echo Recommended Python: 3.12 x64 or 3.11 x64
echo Avoid public builds with Python 3.14
echo ============================================================
echo.

set "APP_NAME=CD_Texture_Overlay_Builder"
set "ENTRY=texture_overlay_builder.py"
set "VENV_DIR=.venv"
set "PY_CMD="

echo Searching for Python 3.12 / 3.11...
echo.

rem ------------------------------------------------------------
rem Prefer fixed common Python install paths first.
rem ------------------------------------------------------------

call :CheckPython "C:\Program Files\Python312\python.exe"
if defined PY_CMD goto :PY_FOUND

call :CheckPython "C:\Program Files\Python311\python.exe"
if defined PY_CMD goto :PY_FOUND

call :CheckPython "%LocalAppData%\Programs\Python\Python312\python.exe"
if defined PY_CMD goto :PY_FOUND

call :CheckPython "%LocalAppData%\Programs\Python\Python311\python.exe"
if defined PY_CMD goto :PY_FOUND

rem ------------------------------------------------------------
rem Try py launcher if available.
rem ------------------------------------------------------------

where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PY_CMD=py -3.12"
        goto :PY_FOUND
    )

    py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PY_CMD=py -3.11"
        goto :PY_FOUND
    )
)

rem ------------------------------------------------------------
rem Try all python.exe entries in PATH.
rem ------------------------------------------------------------

for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY_CMD (
        call :CheckPython "%%P"
    )
)

if defined PY_CMD goto :PY_FOUND

echo ERROR: Python 3.12 or 3.11 was not found.
echo.
echo Install Python 3.12 x64 and enable:
echo - Add python.exe to PATH
echo - Install py launcher
echo.
echo Current PATH python entries:
where python
echo.
pause
exit /b 1


:PY_FOUND
echo Found Python:
%PY_CMD% --version
echo.

rem Reject Python 3.14 explicitly.
%PY_CMD% -c "import sys; raise SystemExit(1 if sys.version_info[:2] >= (3,14) else 0)" >nul 2>nul
if errorlevel 1 (
    echo ERROR: This build script detected Python 3.14 or newer.
    echo Please build public releases with Python 3.12 x64 or 3.11 x64.
    echo.
    pause
    exit /b 1
)

if not exist "%ENTRY%" (
    echo ERROR: Entry file not found:
    echo %ENTRY%
    echo.
    pause
    exit /b 1
)

rem ------------------------------------------------------------
rem Check existing venv. Delete it if it was created with wrong Python.
rem ------------------------------------------------------------

if exist "%VENV_DIR%\Scripts\python.exe" (
    echo Checking existing virtual environment...
    "%VENV_DIR%\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] in [(3,12),(3,11)] else 1)" >nul 2>nul
    if errorlevel 1 (
        echo Existing .venv was created with the wrong Python version.
        echo Removing old .venv...
        rmdir /s /q "%VENV_DIR%"
    )
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 goto :FAIL
)

set "VENV_PY=%CD%\%VENV_DIR%\Scripts\python.exe"

echo.
echo Using venv Python:
"%VENV_PY%" --version
echo.

echo Upgrading pip/setuptools/wheel...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :FAIL

if exist "requirements_overlay_builder.txt" (
    echo.
    echo Installing requirements_overlay_builder.txt...
    "%VENV_PY%" -m pip install -r "requirements_overlay_builder.txt"
    if errorlevel 1 goto :FAIL
) else if exist "requirements.txt" (
    echo.
    echo Installing requirements.txt...
    "%VENV_PY%" -m pip install -r "requirements.txt"
    if errorlevel 1 goto :FAIL
) else (
    echo.
    echo WARNING: No requirements file found.
    echo Installing PyInstaller only...
    "%VENV_PY%" -m pip install pyinstaller
    if errorlevel 1 goto :FAIL
)

rem ------------------------------------------------------------
rem Try to build native C helper if missing.
rem ------------------------------------------------------------

echo.
echo Checking native hash helper...

if not exist "tools\cd_hashlittle_native.exe" (
    echo Native helper not found. Trying to build it...

    if exist "BUILD_FAST_HASH_HELPER.bat" (
        call "BUILD_FAST_HASH_HELPER.bat"
    ) else if exist "BUILD_NATIVE_C_HELPER.bat" (
        call "BUILD_NATIVE_C_HELPER.bat"
    ) else (
        echo WARNING: No helper build script found.
    )
)

if exist "tools\cd_hashlittle_native.exe" (
    echo Native helper found:
    echo tools\cd_hashlittle_native.exe

    if exist "TEST_FAST_HASH_HELPER.bat" (
        echo.
        echo Testing native hash helper...
        call "TEST_FAST_HASH_HELPER.bat"
        if errorlevel 1 (
            echo WARNING: Hash helper test failed. Build will continue, but verify manually.
        )
    )
) else (
    echo.
    echo WARNING: Native hash helper was not built.
    echo The app can still run, but large texture builds may be much slower.
    echo For release builds, compile tools\cd_hashlittle_native.exe first.
)

rem ------------------------------------------------------------
rem Clean old build output.
rem ------------------------------------------------------------

echo.
echo Cleaning old build/dist folders...

if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

rem ------------------------------------------------------------
rem Build ONEDIR / windowed / no UPX.
rem ------------------------------------------------------------

echo.
echo Building with PyInstaller ONEDIR, windowed, no UPX...
echo.

if exist "tools\cd_hashlittle_native.exe" (
    "%VENV_PY%" -m PyInstaller ^
        --noconfirm ^
        --clean ^
        --onedir ^
        --windowed ^
        --noupx ^
        --name "%APP_NAME%" ^
        --add-data "tools\cd_hashlittle_native.exe;tools" ^
        "%ENTRY%"
) else (
    "%VENV_PY%" -m PyInstaller ^
        --noconfirm ^
        --clean ^
        --onedir ^
        --windowed ^
        --noupx ^
        --name "%APP_NAME%" ^
        "%ENTRY%"
)

if errorlevel 1 goto :FAIL

set "DIST_DIR=dist\%APP_NAME%"

if not exist "%DIST_DIR%\%APP_NAME%.exe" (
    echo ERROR: Build finished but EXE was not found:
    echo %DIST_DIR%\%APP_NAME%.exe
    echo.
    pause
    exit /b 1
)

rem ------------------------------------------------------------
rem Copy public documentation/license files into release folder.
rem ------------------------------------------------------------

echo.
echo Copying documentation files...

for %%F in (
    README.md
    README_BUILD.md
    README_SECURITY.md
    README_NEXUS_REVIEW.md
    README_DISTRIBUTION.txt
    README_RUNTIME_ERROR.txt
    README_ES.txt
    THIRD_PARTY_LICENSES.txt
    LICENSE
) do (
    if exist "%%F" copy /y "%%F" "%DIST_DIR%\" >nul
)

rem Copy helper next to exe too, for extra compatibility.
if exist "tools\cd_hashlittle_native.exe" (
    if not exist "%DIST_DIR%\tools" mkdir "%DIST_DIR%\tools"
    copy /y "tools\cd_hashlittle_native.exe" "%DIST_DIR%\tools\" >nul
)

rem ------------------------------------------------------------
rem Generate SHA256SUMS.txt.
rem ------------------------------------------------------------

echo.
echo Generating SHA256SUMS.txt...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$files = @('%DIST_DIR%\%APP_NAME%.exe');" ^
    "if (Test-Path '%DIST_DIR%\tools\cd_hashlittle_native.exe') { $files += '%DIST_DIR%\tools\cd_hashlittle_native.exe' }" ^
    "$out = foreach ($f in $files) { $h = Get-FileHash -Algorithm SHA256 $f; '{0}  {1}' -f $h.Hash.ToLower(), (Split-Path $f -Leaf) };" ^
    "$out | Set-Content -Encoding ASCII '%DIST_DIR%\SHA256SUMS.txt'"

echo.
echo ============================================================
echo BUILD COMPLETE
echo ============================================================
echo.
echo Final folder:
echo %CD%\%DIST_DIR%
echo.
echo Important:
echo Upload the FULL folder as a ZIP.
echo Do NOT upload only %APP_NAME%.exe
echo The _internal folder is required.
echo.
pause
exit /b 0


:CheckPython
set "CAND=%~1"

if not exist "%CAND%" exit /b 0

"%CAND%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] in [(3,12),(3,11)] else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD="%CAND%""
)
exit /b 0


:FAIL
echo.
echo ============================================================
echo BUILD FAILED
echo ============================================================
echo.
echo Review the error above.
echo.
pause
exit /b 1