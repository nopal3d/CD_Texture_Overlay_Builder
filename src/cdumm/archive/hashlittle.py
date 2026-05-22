"""hashlittle implementation for PAMT/PAPGT integrity chain.

This is the Bob Jenkins hashlittle hash used by Crimson Desert for:
- PAMT hash: hashlittle(pamt[12:], 0xC5EDE)
- PAPGT hash: hashlittle(papgt[12:], 0xC5EDE)

Uses Rust cdumm_native.compute_hashlittle when available (260x faster),
falls back to pure Python.
"""
import struct
import os
import subprocess
import sys
from pathlib import Path

try:
    from cdumm_native import compute_hashlittle as _native_hashlittle
except ImportError:
    _native_hashlittle = None


def hashlittle(data: bytes, initval: int = 0) -> int:
    """Bob Jenkins hashlittle hash function."""
    if _native_hashlittle is not None:
        return _native_hashlittle(data, initval)
    length = len(data)
    a = b = c = (0xDEADBEEF + length + initval) & 0xFFFFFFFF

    offset = 0
    while length > 12:
        a = (a + struct.unpack_from("<I", data, offset)[0]) & 0xFFFFFFFF
        b = (b + struct.unpack_from("<I", data, offset + 4)[0]) & 0xFFFFFFFF
        c = (c + struct.unpack_from("<I", data, offset + 8)[0]) & 0xFFFFFFFF

        a = (a - c) & 0xFFFFFFFF; a ^= ((c << 4) | (c >> 28)) & 0xFFFFFFFF; c = (c + b) & 0xFFFFFFFF
        b = (b - a) & 0xFFFFFFFF; b ^= ((a << 6) | (a >> 26)) & 0xFFFFFFFF; a = (a + c) & 0xFFFFFFFF
        c = (c - b) & 0xFFFFFFFF; c ^= ((b << 8) | (b >> 24)) & 0xFFFFFFFF; b = (b + a) & 0xFFFFFFFF
        a = (a - c) & 0xFFFFFFFF; a ^= ((c << 16) | (c >> 16)) & 0xFFFFFFFF; c = (c + b) & 0xFFFFFFFF
        b = (b - a) & 0xFFFFFFFF; b ^= ((a << 19) | (a >> 13)) & 0xFFFFFFFF; a = (a + c) & 0xFFFFFFFF
        c = (c - b) & 0xFFFFFFFF; c ^= ((b << 4) | (b >> 28)) & 0xFFFFFFFF; b = (b + a) & 0xFFFFFFFF

        offset += 12
        length -= 12

    # Handle remaining bytes
    remaining = data[offset:]
    if length > 0:
        # Pad remaining bytes into a, b, c
        padded = remaining + b"\x00" * (12 - len(remaining))
        if length >= 1: a = (a + padded[0]) & 0xFFFFFFFF
        if length >= 2: a = (a + (padded[1] << 8)) & 0xFFFFFFFF
        if length >= 3: a = (a + (padded[2] << 16)) & 0xFFFFFFFF
        if length >= 4: a = (a + (padded[3] << 24)) & 0xFFFFFFFF
        if length >= 5: b = (b + padded[4]) & 0xFFFFFFFF
        if length >= 6: b = (b + (padded[5] << 8)) & 0xFFFFFFFF
        if length >= 7: b = (b + (padded[6] << 16)) & 0xFFFFFFFF
        if length >= 8: b = (b + (padded[7] << 24)) & 0xFFFFFFFF
        if length >= 9: c = (c + padded[8]) & 0xFFFFFFFF
        if length >= 10: c = (c + (padded[9] << 8)) & 0xFFFFFFFF
        if length >= 11: c = (c + (padded[10] << 16)) & 0xFFFFFFFF
        if length >= 12: c = (c + (padded[11] << 24)) & 0xFFFFFFFF

        # Final mixing
        c ^= b; c = (c - ((b << 14) | (b >> 18))) & 0xFFFFFFFF
        a ^= c; a = (a - ((c << 11) | (c >> 21))) & 0xFFFFFFFF
        b ^= a; b = (b - ((a << 25) | (a >> 7))) & 0xFFFFFFFF
        c ^= b; c = (c - ((b << 16) | (b >> 16))) & 0xFFFFFFFF
        a ^= c; a = (a - ((c << 4) | (c >> 28))) & 0xFFFFFFFF
        b ^= a; b = (b - ((a << 14) | (a >> 18))) & 0xFFFFFFFF
        c ^= b; c = (c - ((b << 24) | (b >> 8))) & 0xFFFFFFFF

    return c



def external_hash_helper_path():
    """Return the fastest available hash helper executable.

    Preference order for v1.2:
      1) cd_hashlittle_native.exe  (native C helper)
      2) None                      (Python fallback)

    The old C# helper is intentionally not used in v1.2 Nexus-safe builds.

    The helpers are optional. If missing or failing, the Python fallback still
    produces correct hashes, only slower.
    """
    env = os.environ.get("CDOB_FAST_HASH_EXE", "").strip('"')
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))

    helper_names = ["cd_hashlittle_native.exe"]

    roots: list[Path] = []
    try:
        # Source layout: <root>/src/cdumm/archive/hashlittle.py -> root is parents[3]
        roots.append(Path(__file__).resolve().parents[3])
    except Exception:
        pass
    try:
        roots.append(Path(getattr(sys, "_MEIPASS", Path.cwd())))
    except Exception:
        pass
    roots.append(Path.cwd())
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)

    for root in roots:
        for helper_name in helper_names:
            candidates.append(root / "tools" / helper_name)

    # Deduplicate while preserving order.
    seen: set[str] = set()
    for c in candidates:
        try:
            key = str(c.resolve())
        except Exception:
            key = str(c)
        if key in seen:
            continue
        seen.add(key)
        try:
            if c.exists() and c.is_file():
                return c
        except OSError:
            continue
    return None


def _hashlittle_file_external(path, initval: int = 0, progress_cb=None):
    helper = external_hash_helper_path()
    if helper is None:
        return None
    path = Path(path)
    try:
        kwargs = {}
        if os.name == "nt":
            # Prevent the brief CMD window flash when the GUI app spawns the
            # native hash helper from a PyInstaller --windowed build.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.Popen(
            [str(helper), str(path), str(initval)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        result = None
        if proc.stdout is not None:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 3 and parts[0].upper() == "PROGRESS":
                    try:
                        done = int(parts[1]); total = int(parts[2])
                        if progress_cb:
                            progress_cb(done, total)
                    except Exception:
                        pass
                elif len(parts) >= 2 and parts[0].upper() == "HASH":
                    result = int(parts[1]) & 0xFFFFFFFF
        stderr = ""
        if proc.stderr is not None:
            stderr = proc.stderr.read()
        rc = proc.wait()
        if rc == 0 and result is not None:
            return result
        raise RuntimeError(f"Fast hash helper failed rc={rc}: {stderr.strip()}")
    except Exception:
        # Silent fallback: the caller still gets a correct Python hash.
        return None

def hashlittle_file(path, initval: int = 0, chunk_size: int = 16 * 1024 * 1024, progress_cb=None) -> int:
    """Compute Bob Jenkins hashlittle over a file without loading it all into RAM.

    The result matches hashlittle(path.read_bytes(), initval). This is used for
    multi-GB PAZ overlays where converting the whole file to bytes can spike RAM.
    """
    fast = _hashlittle_file_external(path, initval, progress_cb=progress_cb)
    if fast is not None:
        return fast

    path = Path(path)
    total_len = path.stat().st_size
    a = b = c = (0xDEADBEEF + total_len + initval) & 0xFFFFFFFF

    processed = 0
    last_report = 0
    buf = b""

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            buf += chunk
            # IMPORTANT: the reference implementation processes 12-byte blocks
            # only while length_left > 12, so the final 1..12 bytes are handled
            # by the final-mix branch. Leave exactly that tail in buf.
            offset = 0
            limit = len(buf)
            while (limit - offset) >= 12 and (processed + 12) < total_len:
                a = (a + struct.unpack_from("<I", buf, offset)[0]) & 0xFFFFFFFF
                b = (b + struct.unpack_from("<I", buf, offset + 4)[0]) & 0xFFFFFFFF
                c = (c + struct.unpack_from("<I", buf, offset + 8)[0]) & 0xFFFFFFFF

                a = (a - c) & 0xFFFFFFFF; a ^= ((c << 4) | (c >> 28)) & 0xFFFFFFFF; c = (c + b) & 0xFFFFFFFF
                b = (b - a) & 0xFFFFFFFF; b ^= ((a << 6) | (a >> 26)) & 0xFFFFFFFF; a = (a + c) & 0xFFFFFFFF
                c = (c - b) & 0xFFFFFFFF; c ^= ((b << 8) | (b >> 24)) & 0xFFFFFFFF; b = (b + a) & 0xFFFFFFFF
                a = (a - c) & 0xFFFFFFFF; a ^= ((c << 16) | (c >> 16)) & 0xFFFFFFFF; c = (c + b) & 0xFFFFFFFF
                b = (b - a) & 0xFFFFFFFF; b ^= ((a << 19) | (a >> 13)) & 0xFFFFFFFF; a = (a + c) & 0xFFFFFFFF
                c = (c - b) & 0xFFFFFFFF; c ^= ((b << 4) | (b >> 28)) & 0xFFFFFFFF; b = (b + a) & 0xFFFFFFFF

                offset += 12
                processed += 12

            if offset:
                buf = buf[offset:]
            if progress_cb and (processed - last_report >= 256 * 1024 * 1024 or processed == total_len):
                last_report = processed
                try:
                    progress_cb(processed, total_len)
                except Exception:
                    pass

    remaining = buf
    rem_len = len(remaining)
    if rem_len > 0:
        padded = remaining + b"\x00" * (12 - rem_len)
        if rem_len >= 1: a = (a + padded[0]) & 0xFFFFFFFF
        if rem_len >= 2: a = (a + (padded[1] << 8)) & 0xFFFFFFFF
        if rem_len >= 3: a = (a + (padded[2] << 16)) & 0xFFFFFFFF
        if rem_len >= 4: a = (a + (padded[3] << 24)) & 0xFFFFFFFF
        if rem_len >= 5: b = (b + padded[4]) & 0xFFFFFFFF
        if rem_len >= 6: b = (b + (padded[5] << 8)) & 0xFFFFFFFF
        if rem_len >= 7: b = (b + (padded[6] << 16)) & 0xFFFFFFFF
        if rem_len >= 8: b = (b + (padded[7] << 24)) & 0xFFFFFFFF
        if rem_len >= 9: c = (c + padded[8]) & 0xFFFFFFFF
        if rem_len >= 10: c = (c + (padded[9] << 8)) & 0xFFFFFFFF
        if rem_len >= 11: c = (c + (padded[10] << 16)) & 0xFFFFFFFF
        if rem_len >= 12: c = (c + (padded[11] << 24)) & 0xFFFFFFFF

        c ^= b; c = (c - ((b << 14) | (b >> 18))) & 0xFFFFFFFF
        a ^= c; a = (a - ((c << 11) | (c >> 21))) & 0xFFFFFFFF
        b ^= a; b = (b - ((a << 25) | (a >> 7))) & 0xFFFFFFFF
        c ^= b; c = (c - ((b << 16) | (b >> 16))) & 0xFFFFFFFF
        a ^= c; a = (a - ((c << 4) | (c >> 28))) & 0xFFFFFFFF
        b ^= a; b = (b - ((a << 14) | (a >> 18))) & 0xFFFFFFFF
        c ^= b; c = (c - ((b << 24) | (b >> 8))) & 0xFFFFFFFF

    if progress_cb:
        try:
            progress_cb(total_len, total_len)
        except Exception:
            pass
    return c


INTEGRITY_SEED = 0xC5EDE


def compute_pamt_hash(pamt_data: bytes) -> int:
    """Compute PAMT integrity hash: hashlittle(pamt[12:], 0xC5EDE)."""
    return hashlittle(pamt_data[12:], INTEGRITY_SEED)


def compute_papgt_hash(papgt_data: bytes) -> int:
    """Compute PAPGT integrity hash: hashlittle(papgt[12:], 0xC5EDE)."""
    return hashlittle(papgt_data[12:], INTEGRITY_SEED)
