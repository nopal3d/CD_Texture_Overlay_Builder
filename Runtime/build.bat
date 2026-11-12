@echo off
rem Builds CDHDRuntime.asi (x64) with the MSVC toolchain.
rem Usage: build.bat [test]   - "test" also builds the mock game used by test\run_test.bat
setlocal
set ROOT=%~dp0
set OUT=%ROOT%bin
set OBJ=%ROOT%obj

if defined VSCMD_VER goto :tools_ready
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
if not defined VSDIR ( echo MSVC x64 tools not found. & exit /b 1 )
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
:tools_ready

if not exist "%OUT%" mkdir "%OUT%"
if not exist "%OBJ%" mkdir "%OBJ%"

ml64 /nologo /c /Fo"%OBJ%\hooks.obj" "%ROOT%src\hooks.asm" || exit /b 1
cl /nologo /O2 /MT /EHsc /W3 /std:c++17 /DUNICODE /D_UNICODE /c /Fo"%OBJ%\main.obj" "%ROOT%src\main.cpp" || exit /b 1
link /nologo /DLL /OUT:"%OUT%\CDHDRuntime.asi" "%OBJ%\main.obj" "%OBJ%\hooks.obj" kernel32.lib || exit /b 1
del /q "%OUT%\CDHDRuntime.exp" "%OUT%\CDHDRuntime.lib" 2>nul
echo Built %OUT%\CDHDRuntime.asi

if /i "%1"=="test" (
  cl /nologo /Od /MT /EHsc /GR /std:c++17 /Fo"%OBJ%\mock_game.obj" /Fe"%OUT%\mock_game.exe" "%ROOT%test\mock_game.cpp" /link /OPT:NOICF || exit /b 1
  echo Built %OUT%\mock_game.exe
)
endlocal
