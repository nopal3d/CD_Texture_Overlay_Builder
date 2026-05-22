# Build Instructions

These instructions are for building CD Texture Overlay Builder from source on Windows.

## Requirements

- Windows 10 or Windows 11
- Python 3.11 or 3.12 recommended
- pip
- Visual Studio Community / Build Tools with `Desktop development with C++`
- PyInstaller, installed through the provided requirements file

## Setup from source

Open CMD or PowerShell in the project folder:

```bat
cd /d "PATH_TO_THIS_FOLDER"
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements_overlay_builder.txt
```

## Build native C hash helper

Open `x64 Native Tools Command Prompt for VS`, then run:

```bat
cd /d "PATH_TO_THIS_FOLDER"
BUILD_NATIVE_C_HELPER.bat
```

This should create:

```text
tools\cd_hashlittle_native.exe
```

The tool can still run without this helper, but large PAZ hashing will be slower because it will use the Python fallback.

## Test native helper

```bat
TEST_FAST_HASH_HELPER.bat
```

The test compares the native helper against the Python reference implementation.

## Run from source

```bat
RUN_OVERLAY_BUILDER.bat
```

## Build Windows executable

```bat
BUILD_WINDOWS_EXE.bat
```

Version 1.2.5 uses a Nexus-safe PyInstaller configuration:

- `onedir` build, not `onefile`
- `--windowed`
- `--noupx`
- native C helper only
- Python fallback if the native helper is missing

The final executable will be created here:

```text
dist\CD_Texture_Overlay_Builder\CD_Texture_Overlay_Builder.exe
```

A SHA256 checksum file is generated here:

```text
dist\CD_Texture_Overlay_Builder\SHA256SUMS.txt
```

## Why onedir instead of onefile?

PyInstaller onefile apps self-extract to a temporary AppData/Temp folder at runtime. That behavior often increases antivirus false positives and can make users worry when they see temporary paths.

The v1.2.5 build uses onedir to avoid that self-extraction behavior.


## w64devkit Auto Detection

BUILD_NATIVE_C_HELPER.bat also searches common w64devkit locations such as `C:\TempCDUMM\w64devkit` and `C:\w64devkit`.


## Compatibility Build Script

`BUILD_FAST_HASH_HELPER.bat` is included as a compatibility alias for older instructions. It calls `BUILD_NATIVE_C_HELPER.bat` and builds `tools\cd_hashlittle_native.exe`.


## Runtime DLL Error Notes

For public releases, build with Python 3.12 x64 when possible. The release should be packaged as a full ONEDIR folder, not as a single copied EXE. See README_RUNTIME_ERROR.txt and README_DISTRIBUTION.txt.
