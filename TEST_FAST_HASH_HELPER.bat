@echo off
setlocal
cd /d "%~dp0"
if not exist "tools\cd_hashlittle_native.exe" (
  call BUILD_NATIVE_C_HELPER.bat
)
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip setuptools wheel
python tools\test_fast_hash_helper.py
if not defined CI pause
