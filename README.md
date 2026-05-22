# CD Texture Overlay Builder

Version: 1.2.5
Mod name: KhainOneHDTexture

CD Texture Overlay Builder is a local offline tool for installing Crimson Desert texture overlays.

It creates PAZ/PAMT overlay folders such as `0037/0.paz` and `0037/0.pamt`, then updates the Crimson Desert meta files so the game can load the new textures.

The tool does not directly repack the original game PAZ archives.

## Main features

- Builds texture PAZ/PAMT overlays from DDS files.
- Applies overlays by updating `meta/0.pathc` and `meta/0.papgt`.
- Supports Smart Hold / Release Hold + Reapply for compatibility with other mod managers.
- Preserves external mods whenever possible.
- Keeps a master registry of overlays created by this tool.
- Uses a native C helper for fast PAZ hashing when available.
- Falls back to pure Python hashing if the native helper is missing.
- PyInstaller onedir/no-UPX build to reduce antivirus false positives.

## Recommended install flow

1. Install other mods first using your preferred mod manager.
2. Run `CD_Texture_Overlay_Builder.exe`.
3. Select or auto-detect your Crimson Desert game folder.
4. Select the DDS texture folder.
5. Select the correct source type, usually `0000 - Object textures`.
6. Click `Build / Apply Overlay`.
7. Launch the game and test.

## Using other mod managers later

Before installing more mods with another manager:

1. Open this tool.
2. Click `Smart Hold Overlays`.
3. Install more mods with your other manager.
4. Open this tool again.
5. Click `Release Hold + Reapply`.

This temporarily parks this texture pack outside the game folder, then reapplies it over the current modded meta files.

## Source and build

See `README_BUILD.md`.

## Security notes

See `README_SECURITY.md`.

## Third-party notices

See `THIRD_PARTY_LICENSES.txt`.


## Repository contents

```text
texture_overlay_builder.py          Main application / UI / overlay workflow
src/cdumm/archive/                  Adapted CDUMM archive helpers
src/cdumm/engine/                   Adapted path helpers
tools/cd_hashlittle_native.c        Optional native C PAZ hash helper
tools/test_fast_hash_helper.py      Hash helper validation test
BUILD_NATIVE_C_HELPER.bat           Builds the native C helper
BUILD_FAST_HASH_HELPER.bat          Compatibility alias for older instructions
TEST_FAST_HASH_HELPER.bat           Tests native helper against Python reference
BUILD_WINDOWS_EXE.bat               Builds the PyInstaller onedir app
PACKAGE_RELEASE_ZIP.bat             Packages the dist folder for release
README_BUILD.md                     Detailed build instructions
README_SECURITY.md                  Security/behavior notes
THIRD_PARTY_LICENSES.txt            CDUMM MIT notice and license text
docs/BUTTONS_AND_OPTIONS_GUIDE.txt  Detailed UI button guide
```

## Public release recommendation

For Nexus or other mod sites, distribute the portable ONEDIR package created by:

```bat
BUILD_NATIVE_C_HELPER.bat
TEST_FAST_HASH_HELPER.bat
BUILD_WINDOWS_EXE.bat
PACKAGE_RELEASE_ZIP.bat
```

Do not distribute only `CD_Texture_Overlay_Builder.exe`. The full folder generated under `dist\CD_Texture_Overlay_Builder\` is required.
