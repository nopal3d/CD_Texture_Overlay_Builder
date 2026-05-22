from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "CD_Texture_Overlay_Builder"
INTERNAL = DIST / "_internal"

names = [
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "msvcp140.dll",
    "concrt140.dll",
]

candidates_dirs = []
for p in [Path(sys.executable).parent, Path(sys.base_prefix), Path(sys.prefix)]:
    if p not in candidates_dirs:
        candidates_dirs.append(p)

# Also check common Visual Studio redist paths if present.
for env in ["VCToolsRedistDir", "VCINSTALLDIR"]:
    val = os.environ.get(env)
    if val:
        candidates_dirs.append(Path(val))

copied = []
if not INTERNAL.exists():
    print(f"Internal folder not found: {INTERNAL}")
    raise SystemExit(0)

for name in names:
    found = None
    for d in candidates_dirs:
        for candidate in [d / name, *d.glob(f"**/{name}") if d.exists() else []]:
            if candidate.exists():
                found = candidate
                break
        if found:
            break
    if found:
        dst = INTERNAL / name
        try:
            shutil.copy2(found, dst)
            copied.append(str(dst))
        except Exception as e:
            print(f"Could not copy {found}: {e}")

if copied:
    print("Copied runtime DLLs:")
    for c in copied:
        print(" -", c)
else:
    print("No extra VC runtime DLLs copied. This may be OK if PyInstaller already bundled them.")
