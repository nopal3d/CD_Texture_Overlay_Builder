CD Texture Overlay Builder - Runtime Error Help

Error example:

Failed to load Python DLL '...\_internal\python314.dll'
LoadLibrary: The specified module could not be found.

Most common causes:

1. The user copied only CD_Texture_Overlay_Builder.exe
   The public build is ONEDIR, not ONEFILE.
   Users must extract and run the complete folder:

   CD_Texture_Overlay_Builder\
     CD_Texture_Overlay_Builder.exe
     _internal\

   Do not move the exe outside this folder.

2. Antivirus quarantined files from _internal
   Restore the quarantined files or re-extract the full ZIP.

3. Missing Microsoft Visual C++ Runtime
   Install the latest Microsoft Visual C++ Redistributable x64.

4. The app was built with Python 3.14
   For public releases, rebuild with Python 3.12 x64 or Python 3.11 x64.
   Python 3.14 builds may show python314.dll errors on some systems.

Recommended public release build:

- Python 3.12 x64
- PyInstaller ONEDIR
- Windowed
- No UPX
- Native C hash helper included
- Full dist folder zipped, not only the exe

Build commands:

BUILD_NATIVE_C_HELPER.bat
TEST_FAST_HASH_HELPER.bat
BUILD_WINDOWS_EXE.bat
PACKAGE_RELEASE_ZIP.bat
