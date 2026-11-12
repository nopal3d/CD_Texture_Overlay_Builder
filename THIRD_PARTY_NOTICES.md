# Third-party notices

## Noto Sans

HD Overlay Builder can optionally embed Noto Sans font files for a consistent UI.
Noto Sans is distributed by Google under the SIL Open Font License.

This public source package does not require embedded font binaries to build. If
font binaries are added to the repository or release package, keep the OFL license
text beside them, for example:

```text
Resources/Fonts/OFL_NOTO_SANS.txt
```

## DirectXTex texconv (MIT)

`Resources/ThirdParty/texconv.exe` is the unmodified `texconv.exe` from the
official DirectXTex release (github.com/microsoft/DirectXTex, release May2026).
It is embedded in the tool and used to re-encode textures whose block format
differs from the game's. Copyright (c) Microsoft Corporation. MIT License; full
text in `Resources/ThirdParty/DirectXTex_LICENSE.txt`.

## Ultimate ASI Loader (MIT)

`Resources/ThirdParty/UltimateASILoader_x64.dll` is the unmodified x64
`dinput8.dll` from Ultimate ASI Loader v10.0.2
(github.com/ThirteenAG/Ultimate-ASI-Loader). On game 2.04+ the tool installs it
as `bin64\winmm.dll` so the game loads `CDHDRuntime.asi`.
Copyright (c) 2023 ThirteenAG. MIT License; full text in
`Resources/ThirdParty/UltimateASILoader_LICENSE.txt`.
