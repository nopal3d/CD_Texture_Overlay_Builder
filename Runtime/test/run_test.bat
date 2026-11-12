@echo off
rem Builds the plugin + mock game and runs the end-to-end hook test.
setlocal
set ROOT=%~dp0..
call "%ROOT%\build.bat" test || exit /b 1
set T=%ROOT%\obj\testgame
if exist "%T%" rmdir /s /q "%T%"
mkdir "%T%\bin64\cdhd" "%T%\meta" "%T%\textures"
copy /y "%ROOT%\bin\mock_game.exe" "%T%\bin64\" >nul
copy /y "%ROOT%\bin\CDHDRuntime.asi" "%T%\bin64\" >nul
powershell -NoProfile -Command ^
  "$t='%T%';" ^
  "[IO.File]::WriteAllBytes(\"$t\meta\0.paver\", [byte[]](2,0,4,0,8,0,1,0));" ^
  "[IO.File]::WriteAllText(\"$t\textures\test_color.dds\", 'HELLO-HD-TEXTURE');" ^
  "$exe=(Get-Item \"$t\bin64\mock_game.exe\").Length;" ^
  "$m = \"# test`ncdhd-runtime`t1`nbuild`t0200040008000100`t$exe`nfile`ttree/texture/test_color.dds`t$t\textures\test_color.dds`nfile`tmissing/x.dds`t$t\textures\nope.dds`n\";" ^
  "[IO.File]::WriteAllText(\"$t\bin64\cdhd\manifest.tsv\", $m)"
type nul > "%T%\bin64\cdhd\verbose"
"%T%\bin64\mock_game.exe" "%T%\bin64\CDHDRuntime.asi"
set RC=%ERRORLEVEL%
echo ---- CDHDRuntime.log
type "%T%\bin64\cdhd\CDHDRuntime.log"
exit /b %RC%
