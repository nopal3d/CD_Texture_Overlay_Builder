# CDHD Runtime (game 2.04+)

Crimson Desert 2.04 ("Charting the Unknown") signs `meta/0.papgt` and refuses to
start if it is modified, so HD overlays can no longer be added as archive folders.
`CDHDRuntime.asi` serves the HD textures at runtime instead. Nothing in the game's
archives or `meta` folder is written, and Steam's file check stays clean.

## How it works

1. An ASI loader (Ultimate ASI Loader saved as `bin64\winmm.dll`) loads
   `bin64\CDHDRuntime.asi` into the game.
2. The plugin reads `bin64\cdhd\manifest.tsv`. It refuses to serve anything if the
   manifest was written for another game build (checked against `meta\0.paver` and
   the exe size).
3. It finds `pa::BaseResourceLoadWorker`, `pa::ResourceLoadWorker_Package` and
   `pa::BasePackageResourceMap` through MSVC RTTI in the running (already unpacked)
   game image.
4. It patches slots 2, 3, 4, 5, 6, 10, 11, 12 and 13 of the Package worker's vtable.
   A request for a listed path is handed to the game's own loose-file worker code,
   running on a fake worker whose resource map resolves the game path to the file
   on disk. Every other request goes to the original pack reader.
   The forwarding stubs (`src/hooks.asm`) only choose a jump target, so all register
   and stack arguments reach the game's function unchanged.

A served DDS must use the **same compression format** as the game's original
(2.04 keeps drawing it in that format, since `0.pathc` cannot change). The
resolution can be larger. The builder checks this and leaves out mismatches.

## Manifest

UTF-8, TAB separated, `#` starts a comment:

```
cdhd-runtime	1
build	<lowercase hex of meta/0.paver>	<CrimsonDesert.exe size in bytes>
file	<game path, e.g. tree/texture/x.dds>	<absolute path of the DDS on disk>
```

Game paths are matched case-insensitively, with `\` treated as `/` and leading
slashes ignored.

## Diagnostics (bin64\cdhd\)

- `CDHDRuntime.log`: startup, classes found, number of files, `serving` or a `STOP:` reason.
- An empty file named `verbose` logs every served texture.
- An empty file named `trace` logs every path the pack reader is asked for
  (first 5000). Use it to check the path form the game requests.

## Build and test

- `build.bat` builds `bin\CDHDRuntime.asi` (MSVC x64). The tool embeds it at build time.
- `test\run_test.bat` builds a mock game that has classes with the same RTTI names
  and slot roles, loads the plugin and checks the routing end to end.
