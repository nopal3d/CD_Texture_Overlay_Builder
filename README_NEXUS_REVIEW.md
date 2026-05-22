# Nexus Review Notes

This repository contains the full source code for CD Texture Overlay Builder.

The released Windows application is built with PyInstaller in ONEDIR mode, without UPX compression. The final release package should contain the full `dist\CD_Texture_Overlay_Builder\` folder, not just the executable.

## Why antivirus false positives may happen

The tool is an offline modding utility that performs large local file operations on Crimson Desert game archives and meta files. It may be flagged because it:

- is an unsigned executable,
- is packaged with PyInstaller,
- scans and writes game archive files,
- creates PAZ/PAMT overlay folders,
- updates `meta/0.pathc` and `meta/0.papgt`,
- moves registered overlay folders during Smart Hold / Release Hold operations.

## Network / privacy behavior

The tool does not connect to the internet, collect user data, modify the Windows registry, install drivers, or inject DLLs.

See `README_SECURITY.md` for more details.
