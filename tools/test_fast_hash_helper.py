from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from cdumm.archive.hashlittle import hashlittle  # noqa

sizes = [0, 1, 2, 3, 11, 12, 13, 24, 25, 1024 * 1024 + 37]
seed = 0xC5EDE
helpers = [
    ROOT / 'tools' / 'cd_hashlittle_native.exe',
]
helpers = [h for h in helpers if h.exists()]
if not helpers:
    raise SystemExit('ERROR: native C helper exe not found')

for size in sizes:
    p = Path(tempfile.gettempdir()) / f'cd_hashlittle_test_{size}.bin'
    p.write_bytes(os.urandom(size))
    py_hash = hashlittle(p.read_bytes(), seed)
    print(f'Size {size} bytes - Python: {py_hash}')
    for exe in helpers:
        out = subprocess.check_output([str(exe), str(p), str(seed)], text=True)
        helper_hash = None
        for line in out.splitlines():
            if line.startswith('HASH '):
                helper_hash = int(line.split()[1])
        print(f'  {exe.name}: {helper_hash}')
        if py_hash != helper_hash:
            raise SystemExit(f'ERROR: hash mismatch for {exe.name} at size {size}')
print('OK: helper hashes match Python reference')
