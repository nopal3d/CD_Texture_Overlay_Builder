# Security Notes

CD Texture Overlay Builder is a local offline tool.

## The tool does not

- collect user data,
- connect to the internet,
- install drivers,
- inject DLLs,
- modify the Windows registry,
- modify files outside the selected Crimson Desert game folder and its own working folders.

## The tool does

- scan Crimson Desert PAMT files,
- scan the selected DDS texture folder,
- create PAZ/PAMT overlay folders,
- update `meta/0.pathc`,
- update `meta/0.papgt`,
- create backups of meta files,
- keep a local registry of overlays created by the tool,
- move registered texture overlays during Smart Hold,
- restore/reapply registered overlays during Release Hold + Reapply.

## Working folders created by the tool

Inside the Crimson Desert folder:

```text
CDTextureOverlayBuilder\
```

This stores cache, reports, manifests, registry, and backups.

Outside the game folder, next to the game folder:

```text
CDTextureOverlayBuilder_HOLD\
CDTextureOverlayBuilder_REMOVED\
```

These folders are used to temporarily park or remove texture overlay folders.

## Native helper

The tool may include:

```text
tools\cd_hashlittle_native.exe
```

This is a small native C command-line helper used only to calculate Crimson Desert PAZ/PAMT hashes faster.

The C source code is included:

```text
tools\cd_hashlittle_native.c
```

If the native helper is not present, the tool uses a pure Python fallback.

## Antivirus false positives

False positives may occur because the application:

- is a new unsigned executable,
- is built with PyInstaller,
- reads and writes large game archive files,
- creates PAZ/PAMT overlay folders,
- updates game meta files,
- moves folders during Smart Hold / Release Hold operations.

Version 1.2.5 is built with a safer packaging setup:

- PyInstaller onedir mode,
- no onefile self-extraction,
- no UPX compression,
- native C helper only,
- source code and build scripts included.
