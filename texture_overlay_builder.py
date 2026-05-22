from __future__ import annotations

import bisect
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Any

# Make bundled CDUMM archive helpers importable when running from source or PyInstaller.
APP_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
LOCAL_SRC = Path(__file__).resolve().parent / "src"
if LOCAL_SRC.exists():
    sys.path.insert(0, str(LOCAL_SRC))
if (APP_ROOT / "src").exists():
    sys.path.insert(0, str(APP_ROOT / "src"))

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from cdumm.archive.overlay_builder import OverlayEntry, build_overlay_to_file
from cdumm.archive.papgt_manager import PapgtManager
from cdumm.archive.pathc_handler import get_path_hash, read_pathc, serialize_pathc, update_entry, PathcMapEntry
from cdumm.archive.paz_parse import PazEntry, parse_pamt

APP_NAME = "Crimson Desert Texture Overlay Builder"
APP_VERSION = "1.2.5"
SUPPORTED_EXTS = {".dds"}
U32_MAX = 0xFFFFFFFF
DEFAULT_SPLIT_GB = 3.75
MAX_META_BACKUPS = 10
DEFAULT_MOD_NAME = "KhainOneHDTexture"

# Simple UI presets for the most common Crimson Desert texture packages.
# The tool still builds one target filter per run; use two passes for 0000 and 0009.
FILTER_PRESETS: dict[str, tuple[str, str]] = {
    "Todos / sin filtro": ("", ""),
    "0000 - Object textures": ("0000", "object/texture"),
    "0000 - Object sublayer textures": ("0000", "object/texture/sublayer"),
    "0009 - Character textures": ("0009", "character/texture"),
    "Solo PAMT 0000 (sin ruta)": ("0000", ""),
    "Solo PAMT 0009 (sin ruta)": ("0009", ""),
}


@dataclass(slots=True)
class IndexCandidate:
    pamt_dir: str
    entry_path: str          # Flattened PAMT path, e.g. object/texture.dds or object/file.dds
    full_path: str           # Full virtual path if recoverable, e.g. object/texture/file.dds
    filename: str
    compression_type: int
    encrypted: bool
    crypto_filename: str
    flags: int
    comp_size: int
    orig_size: int


@dataclass(slots=True)
class MatchedFile:
    source_path: str
    rel_path: str
    size: int
    pamt_dir: str
    entry_path: str
    full_path: str
    filename: str
    compression_type: int
    encrypted: bool
    crypto_filename: str

    def metadata(self) -> dict:
        return {
            "entry_path": self.entry_path,
            "pamt_dir": self.pamt_dir,
            "compression_type": self.compression_type,
            "encrypted": self.encrypted,
            "crypto_filename": self.crypto_filename,
            "source_path": self.source_path,
            "full_path": self.full_path,
            "delta_hash": self._delta_hash(),
        }

    def _delta_hash(self) -> str:
        try:
            st = Path(self.source_path).stat()
            return f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            return ""


@dataclass(slots=True)
class BuildOptions:
    game_dir: Path
    texture_dir: Path
    output_dir: Path
    mod_name: str
    apply_to_game: bool
    allow_unique_filename: bool
    dry_run: bool
    split_gb: float
    backup_meta: bool
    scan_existing_mod_dirs: bool
    target_pamt_dir: str = ""
    target_full_prefix: str = ""


@dataclass(slots=True)
class BuildResult:
    matched_count: int
    skipped_count: int
    ambiguous_count: int
    overlay_dirs: list[str]
    output_dir: str
    manifest_path: str
    report_path: str
    applied: bool


class UiLogger:
    def __init__(self, cb: Callable[[str], None]) -> None:
        self.cb = cb

    def __call__(self, message: str) -> None:
        self.cb(message)




def native_hash_available() -> bool:
    try:
        import cdumm_native  # type: ignore
        return hasattr(cdumm_native, "compute_hashlittle")
    except Exception:
        return False

class PamtIndex:
    def __init__(self) -> None:
        self.by_full: dict[str, list[IndexCandidate]] = {}
        self.by_flat: dict[str, list[IndexCandidate]] = {}
        self.by_name: dict[str, list[IndexCandidate]] = {}
        # Fast suffix lookup for cases where the source keeps only part of the internal path.
        # v0.3.3 did this with a full scan over ~281k paths per file, which made matching
        # 10k+ loose DDS files take several minutes. v0.3.4 builds these suffix keys once.
        self.by_suffix: dict[str, list[IndexCandidate]] = {}
        self.candidates: list[IndexCandidate] = []
        self.existing_mod_targets: dict[str, list[str]] = {}
        self.existing_mod_names: dict[str, list[str]] = {}

    def add(self, cand: IndexCandidate, is_mod_dir: bool = False) -> None:
        self.candidates.append(cand)
        flat = norm_virtual(cand.entry_path)
        full = norm_virtual(cand.full_path)
        name = cand.filename.lower()
        self.by_flat.setdefault(flat, []).append(cand)
        self.by_full.setdefault(full, []).append(cand)
        self.by_name.setdefault(name, []).append(cand)
        # Build suffix keys from the full virtual path, e.g.
        # object/texture/foo.dds -> texture/foo.dds and foo.dds.
        # This gives the same behavior as the old endswith scan, but O(1).
        parts = [x for x in full.split("/") if x]
        for i in range(1, len(parts)):
            suffix = "/".join(parts[i:])
            self.by_suffix.setdefault(suffix, []).append(cand)
        if is_mod_dir:
            self.existing_mod_targets.setdefault(flat, []).append(cand.pamt_dir)
            self.existing_mod_names.setdefault(name, []).append(cand.pamt_dir)


def norm_virtual(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("/").lower()


def rel_to_virtual(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path.name
    return str(rel).replace("\\", "/").strip("/")


def is_game_dir(path: Path) -> bool:
    return (path / "meta" / "0.papgt").exists() and any(path.glob("[0-9][0-9][0-9][0-9]/0.pamt"))


def is_cloud_synced_path(path: Path) -> bool:
    """Best-effort warning for folders that are commonly slowed by sync clients."""
    p = str(path).lower().replace("\\", "/")
    markers = ["/onedrive/", "onedrive", "/dropbox/", "/google drive/", "/iclouddrive/"]
    return any(m in p for m in markers)


def fmt_eta(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}min"
    return f"{minutes / 60.0:.1f}h"


def discover_texture_files(texture_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in texture_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            files.append(p)
    return sorted(files, key=lambda x: str(x).lower())


def _parse_full_path_map_from_pamt(pamt_path: Path) -> dict[str, str]:
    """Return flattened_entry_path -> full virtual path for a PAMT.

    CDUMM's parse_pamt intentionally returns a flattened path. For textures we
    also need the full folder path so PATHC can be updated accurately.
    """
    import struct

    try:
        data = pamt_path.read_bytes()
    except OSError:
        return {}
    if len(data) < 32:
        return {}

    try:
        off = 16
        paz_count = struct.unpack_from("<I", data, 4)[0]
        for i in range(paz_count):
            off += 8
            if i < paz_count - 1:
                off += 4

        folder_len = struct.unpack_from("<I", data, off)[0]
        off += 4
        folder_start = off
        folders: dict[int, tuple[int, str]] = {}
        while off < folder_start + folder_len:
            rel = off - folder_start
            parent = struct.unpack_from("<I", data, off)[0]
            slen = data[off + 4]
            name = data[off + 5:off + 5 + slen].decode("utf-8", errors="replace")
            folders[rel] = (parent, name)
            off += 5 + slen

        def folder_path(ref: int) -> str:
            parts: list[str] = []
            cur = ref
            while cur != 0xFFFFFFFF and len(parts) < 64:
                if cur not in folders:
                    break
                parent, name = folders[cur]
                parts.append(name)
                cur = parent
            return "".join(reversed(parts)).strip("/")

        root = ""
        for parent, name in folders.values():
            if parent == 0xFFFFFFFF:
                root = name.strip("/")
                break

        node_len = struct.unpack_from("<I", data, off)[0]
        off += 4
        node_start = off
        nodes: dict[int, tuple[int, str]] = {}
        while off < node_start + node_len:
            rel = off - node_start
            parent = struct.unpack_from("<I", data, off)[0]
            slen = data[off + 4]
            name = data[off + 5:off + 5 + slen].decode("utf-8", errors="replace")
            nodes[rel] = (parent, name)
            off += 5 + slen

        def node_path(ref: int) -> str:
            parts: list[str] = []
            cur = ref
            while cur != 0xFFFFFFFF and len(parts) < 64:
                if cur not in nodes:
                    break
                parent, name = nodes[cur]
                parts.append(name)
                cur = parent
            return "".join(reversed(parts)).strip("/")

        folder_count = struct.unpack_from("<I", data, off)[0]
        off += 4
        folder_records: list[tuple[str, int, int]] = []
        for _ in range(folder_count):
            _path_hash, folder_ref, file_index, file_count = struct.unpack_from("<IIII", data, off)
            folder_records.append((folder_path(folder_ref), file_index, file_count))
            off += 16

        file_to_folder: dict[int, str] = {}
        for fp, first, count in folder_records:
            for i in range(first, first + count):
                file_to_folder[i] = fp

        file_count = struct.unpack_from("<I", data, off)[0]
        off += 4
        result: dict[str, str] = {}
        for i in range(file_count):
            node_ref = struct.unpack_from("<I", data, off)[0]
            off += 20
            filename = node_path(node_ref)
            flat = f"{root}/{filename}" if root else filename
            folder = file_to_folder.get(i, root)
            full = f"{folder.strip('/')}/{filename}" if folder else filename
            result[norm_virtual(flat)] = full.strip("/")
        return result
    except Exception:
        return {}


def _build_pamt_index_uncached(game_dir: Path, include_existing_mod_dirs: bool, log: Callable[[str], None]) -> PamtIndex:
    idx = PamtIndex()
    pamt_paths = sorted(game_dir.glob("[0-9][0-9][0-9][0-9]/0.pamt"))
    if not pamt_paths:
        raise FileNotFoundError("No encontré carpetas 0000/0.pamt, 0001/0.pamt, etc. ¿La ruta del juego es correcta?")

    log(f"Escaneando {len(pamt_paths)} PAMT del juego...")
    for n, pamt_path in enumerate(pamt_paths, start=1):
        pamt_dir = pamt_path.parent.name
        is_mod_dir = pamt_dir.isdigit() and int(pamt_dir) >= 36
        if is_mod_dir and not include_existing_mod_dirs:
            # We still skip these as targets, but we optionally report conflicts
            # against existing mod dirs below if requested.
            pass
        target_index = not is_mod_dir
        conflict_index = is_mod_dir
        if not target_index and not conflict_index:
            continue

        full_map = _parse_full_path_map_from_pamt(pamt_path)
        try:
            entries = parse_pamt(str(pamt_path), paz_dir=str(pamt_path.parent))
        except Exception as e:
            log(f"WARN: no pude leer {pamt_path}: {e}")
            continue

        for entry in entries:
            if Path(entry.path).suffix.lower() not in SUPPORTED_EXTS:
                continue
            filename = entry.path.rsplit("/", 1)[-1]
            full_path = full_map.get(norm_virtual(entry.path), entry.path)
            cand = IndexCandidate(
                pamt_dir=pamt_dir,
                entry_path=entry.path.replace("\\", "/"),
                full_path=full_path.replace("\\", "/"),
                filename=filename,
                compression_type=entry.compression_type,
                encrypted=entry.encrypted,
                crypto_filename=filename,
                flags=entry.flags,
                comp_size=entry.comp_size,
                orig_size=entry.orig_size,
            )
            if target_index:
                idx.add(cand, is_mod_dir=False)
            elif include_existing_mod_dirs:
                # Existing 0036+ overlay dirs are used only for conflict reporting.
                # Do NOT add them to the main match index, otherwise a texture
                # could accidentally target another mod instead of vanilla.
                flat = norm_virtual(cand.entry_path)
                idx.existing_mod_targets.setdefault(flat, []).append(cand.pamt_dir)
                idx.existing_mod_names.setdefault(cand.filename.lower(), []).append(cand.pamt_dir)
        if n % 5 == 0:
            log(f"  PAMT {n}/{len(pamt_paths)}...")
    log(f"Índice listo: {len(idx.candidates)} texturas encontradas.")
    return idx



PAMT_INDEX_CACHE_SCHEMA = 3


def _pamt_cache_path(game_dir: Path) -> Path:
    return game_dir / "CDTextureOverlayBuilder" / "cache" / "pamt_index_v3.json"


def _pamt_fingerprint(pamt_paths: list[Path], game_dir: Path) -> list[dict]:
    fp: list[dict] = []
    for p in pamt_paths:
        try:
            st = p.stat()
            rel = str(p.relative_to(game_dir)).replace("\\", "/")
            fp.append({"rel": rel, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
        except OSError:
            fp.append({"rel": str(p), "size": -1, "mtime_ns": -1})
    return fp


def _index_to_cache(idx: PamtIndex) -> dict:
    return {
        "candidates": [asdict(c) for c in idx.candidates],
        "existing_mod_targets": idx.existing_mod_targets,
        "existing_mod_names": idx.existing_mod_names,
    }


def _index_from_cache(data: dict) -> PamtIndex:
    idx = PamtIndex()
    for raw in data.get("candidates", []):
        cand = IndexCandidate(**raw)
        idx.add(cand, is_mod_dir=False)
    idx.existing_mod_targets = {str(k): list(v) for k, v in data.get("existing_mod_targets", {}).items()}
    idx.existing_mod_names = {str(k): list(v) for k, v in data.get("existing_mod_names", {}).items()}
    return idx


def build_pamt_index(game_dir: Path, include_existing_mod_dirs: bool, log: Callable[[str], None]) -> PamtIndex:
    """Build or load a cached PAMT texture index.

    v0.3 stores the expensive 281k+ texture index in
    CDTextureOverlayBuilder/cache/pamt_index_v3.json and reuses it as long as
    every 0.pamt file has the same size and mtime. If DMM/another manager
    changes overlays/meta and PAMTs appear/disappear, the fingerprint changes
    and the cache is rebuilt automatically.
    """
    pamt_paths = sorted(game_dir.glob("[0-9][0-9][0-9][0-9]/0.pamt"))
    if not pamt_paths:
        raise FileNotFoundError("No encontré carpetas 0000/0.pamt, 0001/0.pamt, etc. ¿La ruta del juego es correcta?")
    fp = _pamt_fingerprint(pamt_paths, game_dir)
    cache_path = _pamt_cache_path(game_dir)
    try:
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if (
                cached.get("schema") == PAMT_INDEX_CACHE_SCHEMA
                and cached.get("game_dir") == str(game_dir)
                and cached.get("include_existing_mod_dirs") == bool(include_existing_mod_dirs)
                and cached.get("fingerprint") == fp
            ):
                idx = _index_from_cache(cached.get("index", {}))
                log(f"Índice PAMT cacheado cargado: {len(idx.candidates)} texturas.")
                return idx
    except Exception as e:
        log(f"WARN: no pude usar cache PAMT; se reconstruye. Detalle: {e}")

    idx = _build_pamt_index_uncached(game_dir, include_existing_mod_dirs, log)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": PAMT_INDEX_CACHE_SCHEMA,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "game_dir": str(game_dir),
            "include_existing_mod_dirs": bool(include_existing_mod_dirs),
            "fingerprint": fp,
            "index": _index_to_cache(idx),
        }
        safe_write(cache_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        log(f"Cache PAMT guardado: {cache_path}")
    except Exception as e:
        log(f"WARN: no pude guardar cache PAMT: {e}")
    return idx


def _normalize_target_pamt_dir(value: str) -> str:
    value = (value or "").strip()
    if not value or value.lower() in {"all", "todos", "todo", "*"}:
        return ""
    if value.isdigit():
        return f"{int(value):04d}"
    return value


def _normalize_target_prefix(value: str) -> str:
    value = norm_virtual(value or "")
    if value in {"all", "todos", "todo", "*"}:
        return ""
    # Accept user input like 0000/object/texture in the path-prefix field too.
    parts = [x for x in value.split("/") if x]
    if parts and parts[0].isdigit() and len(parts[0]) == 4:
        value = "/".join(parts[1:])
    return value.strip("/")


def _candidate_equiv_key(c: IndexCandidate) -> tuple:
    """Key used to collapse duplicate PAMT candidates that point to the same real target.

    v0.3.6 only deduped candidates when every packing flag also matched. In real
    Crimson Desert PAMTs one vanilla texture can be exposed twice with the exact
    same virtual target path but slightly different parsed flags/metadata. For an
    overlay replacement this is still a single target: the PATHC target is the
    same PAMT + full internal path.

    v0.3.7 therefore dedupes by target identity first. This fixes false
    ambiguities like two identical candidates for:
        0000:object/texture/sublayer/foo.dds
    """
    full = norm_virtual(c.full_path)
    flat = norm_virtual(c.entry_path)
    return (c.pamt_dir, full or flat, c.filename.lower())

def _dedupe_equivalent_candidates(candidates: list[IndexCandidate]) -> list[IndexCandidate]:
    if len(candidates) < 2:
        return candidates
    seen: set[tuple] = set()
    out: list[IndexCandidate] = []
    for c in candidates:
        key = _candidate_equiv_key(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _filter_candidates(candidates: list[IndexCandidate], target_pamt_dir: str = "", target_full_prefix: str = "") -> list[IndexCandidate]:
    """Restrict candidates to an original PAZ/PAMT dir and/or full internal path prefix.

    This resolves many loose-filename ambiguities safely. Example:
      target_pamt_dir="0000" and target_full_prefix="object/texture"
    lets foo.dds match only 0000:object/texture/foo.dds, instead of searching
    every same-named texture in the whole game.
    """
    pamt = _normalize_target_pamt_dir(target_pamt_dir)
    prefix = _normalize_target_prefix(target_full_prefix)
    if not pamt and not prefix:
        return _dedupe_equivalent_candidates(candidates)
    out: list[IndexCandidate] = []
    prefix_slash = prefix + "/" if prefix else ""
    for c in candidates:
        if pamt and c.pamt_dir != pamt:
            continue
        if prefix:
            full = norm_virtual(c.full_path)
            flat = norm_virtual(c.entry_path)
            if not (full == prefix or full.startswith(prefix_slash) or flat == prefix or flat.startswith(prefix_slash)):
                continue
        out.append(c)
    return _dedupe_equivalent_candidates(out)


def choose_candidate(rel_virtual: str, source_name: str, index: PamtIndex, allow_unique_filename: bool, target_pamt_dir: str = "", target_full_prefix: str = "") -> tuple[IndexCandidate | None, str, list[IndexCandidate]]:
    rel_norm = norm_virtual(rel_virtual)
    source_name_lower = source_name.lower()

    # Super-fast common case: user has all DDS loose in one folder.
    # With only a filename there is no useful path suffix to scan; use by_name directly.
    # Safe behavior is unchanged: if the filename appears more than once, it stays ambiguous.
    if "/" not in rel_norm:
        if allow_unique_filename:
            raw_candidates = index.by_name.get(source_name_lower, [])
            candidates = _filter_candidates(raw_candidates, target_pamt_dir, target_full_prefix)
            if len(candidates) == 1:
                method = "unique_filename_filtered" if len(raw_candidates) != len(candidates) else "unique_filename_fast"
                return candidates[0], method, candidates
            if len(candidates) > 1:
                method = "ambiguous_filename_filtered" if len(raw_candidates) != len(candidates) else "ambiguous_filename"
                return None, method, candidates
            if raw_candidates:
                return None, "filtered_out_filename", raw_candidates
        return None, "not_found", []

    # Exact full internal path: object/texture/file.dds
    raw_candidates = index.by_full.get(rel_norm, [])
    candidates = _filter_candidates(raw_candidates, target_pamt_dir, target_full_prefix)
    if len(candidates) == 1:
        method = "exact_full_filtered" if len(raw_candidates) != len(candidates) else "exact_full"
        return candidates[0], method, candidates
    if len(candidates) > 1:
        method = "ambiguous_exact_full_filtered" if len(raw_candidates) != len(candidates) else "ambiguous_exact_full"
        return None, method, candidates

    # Exact flattened PAMT path: object/file.dds
    raw_candidates = index.by_flat.get(rel_norm, [])
    candidates = _filter_candidates(raw_candidates, target_pamt_dir, target_full_prefix)
    if len(candidates) == 1:
        method = "exact_flat_filtered" if len(raw_candidates) != len(candidates) else "exact_flat"
        return candidates[0], method, candidates
    if len(candidates) > 1:
        method = "ambiguous_exact_flat_filtered" if len(raw_candidates) != len(candidates) else "ambiguous_exact_flat"
        return None, method, candidates

    # Fast suffix match. v0.3.3 used: for every file, scan all by_full keys with endswith().
    # On 281k indexed textures x 13k source files, that can take several minutes.
    raw_candidates = index.by_suffix.get(rel_norm, [])
    candidates = _filter_candidates(raw_candidates, target_pamt_dir, target_full_prefix)
    if len(candidates) == 1:
        method = "suffix_full_filtered" if len(raw_candidates) != len(candidates) else "suffix_full_fast"
        return candidates[0], method, candidates
    if len(candidates) > 1:
        method = "ambiguous_suffix_full_filtered" if len(raw_candidates) != len(candidates) else "ambiguous_suffix_full"
        return None, method, candidates

    if allow_unique_filename:
        raw_candidates = index.by_name.get(source_name_lower, [])
        candidates = _filter_candidates(raw_candidates, target_pamt_dir, target_full_prefix)
        if len(candidates) == 1:
            method = "unique_filename_filtered" if len(raw_candidates) != len(candidates) else "unique_filename"
            return candidates[0], method, candidates
        if len(candidates) > 1:
            method = "ambiguous_filename_filtered" if len(raw_candidates) != len(candidates) else "ambiguous_filename"
            return None, method, candidates
        if raw_candidates:
            return None, "filtered_out_filename", raw_candidates

    return None, "not_found", []


def match_textures(game_dir: Path, texture_dir: Path, allow_unique_filename: bool, scan_existing_mod_dirs: bool, log: Callable[[str], None], target_pamt_dir: str = "", target_full_prefix: str = "") -> tuple[list[MatchedFile], list[str], list[str], dict]:
    index = build_pamt_index(game_dir, include_existing_mod_dirs=scan_existing_mod_dirs, log=log)
    source_files = discover_texture_files(texture_dir)
    if not source_files:
        raise FileNotFoundError("No encontré archivos .dds en la carpeta de texturas/mod.")

    matched: list[MatchedFile] = []
    skipped: list[str] = []
    ambiguous: list[str] = []
    stats: dict[str, int] = {}

    log(f"Detectadas {len(source_files)} texturas .dds en el mod.")
    pamt_filter = _normalize_target_pamt_dir(target_pamt_dir)
    prefix_filter = _normalize_target_prefix(target_full_prefix)
    if pamt_filter or prefix_filter:
        log(f"Filtro de origen activo: PAMT={pamt_filter or 'TODOS'}, ruta={prefix_filter or 'TODAS'}. Esto ayuda a resolver ambiguas por filename.")
    for i, src in enumerate(source_files, start=1):
        rel = rel_to_virtual(src, texture_dir)
        cand, method, candidates = choose_candidate(rel, src.name, index, allow_unique_filename, pamt_filter, prefix_filter)
        stats[method] = stats.get(method, 0) + 1
        if cand is None:
            if method.startswith("ambiguous"):
                preview = ", ".join(f"{c.pamt_dir}:{c.full_path}" for c in candidates[:8])
                ambiguous.append(f"{rel} -> {method}: {len(candidates)} candidatos: {preview}")
            elif method.startswith("filtered_out"):
                skipped.append(f"{rel} -> existe en PAMT, pero quedó fuera por filtro de origen ({len(candidates)} candidatos sin filtrar)")
            else:
                skipped.append(f"{rel} -> no encontrado en PAMT vanilla")
            continue
        try:
            size = src.stat().st_size
        except OSError:
            skipped.append(f"{rel} -> no pude leer tamaño")
            continue
        matched.append(MatchedFile(
            source_path=str(src),
            rel_path=rel,
            size=size,
            pamt_dir=cand.pamt_dir,
            entry_path=cand.entry_path,
            full_path=cand.full_path,
            filename=cand.filename,
            compression_type=cand.compression_type,
            encrypted=cand.encrypted,
            crypto_filename=cand.crypto_filename,
        ))
        if i % 500 == 0:
            log(f"  Match {i}/{len(source_files)}...")

    return matched, skipped, ambiguous, {"match_methods": stats, "source_count": len(source_files)}


def split_matches(matches: list[MatchedFile], split_gb: float) -> list[list[MatchedFile]]:
    target = max(0.5, split_gb) * 1024 * 1024 * 1024
    chunks: list[list[MatchedFile]] = []
    cur: list[MatchedFile] = []
    cur_size = 0
    for m in matches:
        est = m.size + 65536
        rem = est % 16
        if rem:
            est += 16 - rem
        if cur and cur_size + est > target:
            chunks.append(cur)
            cur = []
            cur_size = 0
        cur.append(m)
        cur_size += est
    if cur:
        chunks.append(cur)
    return chunks


def allocate_overlay_dirs(game_dir: Path, count: int, output_dir: Path | None = None) -> list[str]:
    max_num = 36
    for base in [game_dir, output_dir]:
        if not base or not base.exists():
            continue
        for d in base.iterdir():
            if d.is_dir() and d.name.isdigit() and len(d.name) == 4:
                max_num = max(max_num, int(d.name))
    return [f"{n:04d}" for n in range(max_num + 1, max_num + 1 + count)]


def safe_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)




def safe_write_large(path: Path, data: bytes, log: Callable[[str], None], label: str = "archivo") -> None:
    """Write a large bytes object with visible progress.

    v0.1 used safe_write() for multi-GB PAZ files, which looked frozen while
    Windows was writing/flushing several GB. This version logs progress so the
    user knows the app is still working.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    total = len(data)
    written = 0
    chunk = 64 * 1024 * 1024
    last_log = 0
    log(f"Escribiendo {label}: {path} ({total / 1024 / 1024:.1f} MB)...")
    with open(tmp, "wb") as f:
        mv = memoryview(data)
        for off in range(0, total, chunk):
            part = mv[off:off + chunk]
            f.write(part)
            written += len(part)
            pct = (written / total * 100.0) if total else 100.0
            if written - last_log >= 256 * 1024 * 1024 or written == total:
                last_log = written
                log(f"  write {label}: {written / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB ({pct:.1f}%)")
        f.flush()
        # For huge overlay files, fsync can take a very long time on HDD/OneDrive/USB
        # and does not add much value because the file is replace-written. Flush is enough
        # here; meta files still use safe_write() with fsync.
    os.replace(tmp, path)

def prune_meta_backups(game_dir: Path, log: Callable[[str], None] | None = None, max_keep: int = MAX_META_BACKUPS) -> None:
    """Keep only the newest meta backups to avoid filling the tool folder.

    We delete the oldest backup folders after creating a new one. This behaves like
    overwriting the oldest slot, but is safer because a newly-created backup is
    never replaced mid-copy.
    """
    root = game_dir / "CDTextureOverlayBuilder" / "backups"
    if not root.exists():
        return
    backups = [p for p in root.iterdir() if p.is_dir() and ((p / "meta" / "0.papgt").exists() or (p / "meta" / "0.pathc").exists())]
    if len(backups) <= max_keep:
        return
    backups.sort(key=lambda x: (x.stat().st_mtime if x.exists() else 0, x.name))
    for old in backups[:max(0, len(backups) - max_keep)]:
        try:
            shutil.rmtree(old)
            if log:
                log(f"Backup viejo eliminado: {old.name}")
        except Exception as e:
            if log:
                log(f"WARN: no pude eliminar backup viejo {old}: {e}")


def copy_current_meta_backup(game_dir: Path, log: Callable[[str], None]) -> Path:
    backup_root = game_dir / "CDTextureOverlayBuilder" / "backups"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_root / stamp
    # In rare cases two operations can start in the same second. Keep names unique.
    if backup_dir.exists():
        suffix = 1
        while (backup_root / f"{stamp}_{suffix:02d}").exists():
            suffix += 1
        backup_dir = backup_root / f"{stamp}_{suffix:02d}"
    for rel in ["meta/0.papgt", "meta/0.pathc"]:
        src = game_dir / rel
        if src.exists():
            dst = backup_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    log(f"Backup meta creado: {backup_dir}")
    prune_meta_backups(game_dir, log, MAX_META_BACKUPS)
    return backup_dir


def build_conflict_lines(matches: list[MatchedFile], game_dir: Path) -> list[str]:
    lines: list[str] = []
    # Lightweight conflict scan: parse existing 0036+ PAMTs by flattened path and filename.
    existing_flat: dict[str, list[str]] = {}
    existing_name: dict[str, list[str]] = {}
    for pamt in sorted(game_dir.glob("[0-9][0-9][0-9][0-9]/0.pamt")):
        d = pamt.parent.name
        if not (d.isdigit() and int(d) >= 36):
            continue
        try:
            for e in parse_pamt(str(pamt), paz_dir=str(pamt.parent)):
                if Path(e.path).suffix.lower() not in SUPPORTED_EXTS:
                    continue
                flat = norm_virtual(e.path)
                name = e.path.rsplit("/", 1)[-1].lower()
                existing_flat.setdefault(flat, []).append(d)
                existing_name.setdefault(name, []).append(d)
        except Exception:
            continue
    for m in matches:
        flat = norm_virtual(m.entry_path)
        if flat in existing_flat:
            lines.append(f"CONFLICTO exacto: {m.entry_path} ya aparece en overlays {existing_flat[flat]}")
        elif m.filename.lower() in existing_name:
            lines.append(f"Posible conflicto por nombre: {m.filename} aparece en overlays {existing_name[m.filename.lower()]}")
    return lines


def update_pathc_for_matches(game_dir: Path, matches: list[MatchedFile], overlay_entries: list[OverlayEntry], log: Callable[[str], None], header_override_by_entry: dict[str, bytes] | None = None) -> bytes | None:
    if not matches:
        return None
    pathc_path = game_dir / "meta" / "0.pathc"
    if not pathc_path.exists():
        log("WARN: meta/0.pathc no existe. Se omite registro PATHC de texturas DDS.")
        return None

    dds_matches = [m for m in matches if m.entry_path.lower().endswith(".dds")]
    if not dds_matches:
        return None

    packed_by_entry: dict[str, OverlayEntry] = {}
    packed_by_name: dict[str, list[OverlayEntry]] = {}
    for oe in overlay_entries:
        if getattr(oe, "entry_path", ""):
            packed_by_entry[norm_virtual(oe.entry_path)] = oe
        packed_by_name.setdefault(oe.filename.lower(), []).append(oe)

    pathc = read_pathc(pathc_path)
    updated = 0
    added = 0
    skipped = 0
    preserved = 0

    for m in dds_matches:
        oe = packed_by_entry.get(norm_virtual(m.entry_path))
        if oe is None:
            cands = packed_by_name.get(m.filename.lower(), [])
            if len(cands) == 1:
                oe = cands[0]
        if oe is None or oe.dds_m_values is None:
            skipped += 1
            log(f"WARN: no pude obtener m-values PATHC para {m.entry_path}")
            continue
        header = b""
        if header_override_by_entry:
            header = header_override_by_entry.get(norm_virtual(m.entry_path), b"") or header_override_by_entry.get(norm_virtual(m.full_path), b"")
        if not header:
            try:
                with open(m.source_path, "rb") as f:
                    header = f.read(148)
            except OSError as e:
                skipped += 1
                log(f"WARN: no pude leer header DDS {m.source_path}: {e}")
                continue
        if len(header) < 128 or header[:4] != b"DDS ":
            skipped += 1
            log(f"WARN: no parece DDS válido: {m.rel_path}")
            continue

        if oe.dir_path:
            vpath = "/" + oe.dir_path.strip("/") + "/" + m.filename
        else:
            vpath = "/" + m.full_path.strip("/")
        h = get_path_hash(vpath)
        pos = bisect.bisect_left(pathc.key_hashes, h)
        exists = pos < len(pathc.key_hashes) and pathc.key_hashes[pos] == h
        if exists and pathc.map_entries[pos].m1 == oe.dds_m_values[0]:
            preserved += 1
            continue

        record_size = pathc.header.dds_record_size
        fourcc = header[84:88] if len(header) >= 88 else b""
        head_size = 148 if (fourcc == b"DX10" and len(header) >= 148) else 128
        rec = bytearray(record_size)
        rec[:min(len(header), head_size, record_size)] = header[:min(len(header), head_size, record_size)]
        rec_bytes = bytes(rec)
        try:
            dds_idx = pathc.dds_records.index(rec_bytes)
        except ValueError:
            pathc.dds_records.append(rec_bytes)
            dds_idx = len(pathc.dds_records) - 1

        update_entry(pathc, vpath, dds_idx, oe.dds_m_values)
        if exists:
            updated += 1
        else:
            added += 1

    pathc.header.dds_record_count = len(pathc.dds_records)
    pathc.header.hash_count = len(pathc.key_hashes)
    out = serialize_pathc(pathc)
    log(f"PATHC: {updated} actualizadas, {added} agregadas, {preserved} preservadas, {skipped} omitidas.")
    return out


def build_pathc_header_cache(matches: list[MatchedFile], log: Callable[[str], None] | None = None) -> dict[str, str]:
    """Store DDS headers needed for future Reapply without rebuilding or requiring source DDS files."""
    headers: dict[str, str] = {}
    for m in matches:
        try:
            with open(m.source_path, "rb") as f:
                header = f.read(148)
            if len(header) >= 128 and header[:4] == b"DDS ":
                headers[norm_virtual(m.entry_path)] = header.hex()
        except OSError as e:
            if log:
                log(f"WARN: no pude guardar header PATHC para registry {m.rel_path}: {e}")
    return headers


def decode_pathc_header_cache(raw: dict[str, str] | None) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            out[norm_virtual(k)] = bytes.fromhex(str(v))
        except ValueError:
            continue
    return out


def write_report(report_path: Path, options: BuildOptions, matches: list[MatchedFile], skipped: list[str], ambiguous: list[str], conflicts: list[str], overlay_dirs: list[str], stats: dict) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    total_size = sum(m.size for m in matches)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"{APP_NAME} v{APP_VERSION}\n")
        f.write(f"Fecha: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"Juego: {options.game_dir}\n")
        f.write(f"Texturas: {options.texture_dir}\n")
        f.write(f"Filtro PAMT origen: {_normalize_target_pamt_dir(options.target_pamt_dir) or 'TODOS'}\n")
        f.write(f"Filtro ruta interna: {_normalize_target_prefix(options.target_full_prefix) or 'TODAS'}\n")
        f.write(f"Modo: {'APLICADO AL JUEGO' if options.apply_to_game else 'BUILD ONLY'}\n")
        f.write(f"Overlay dirs: {overlay_dirs}\n")
        f.write(f"Texturas matched: {len(matches)} ({total_size / 1024 / 1024:.1f} MB fuente)\n")
        f.write(f"Skipped: {len(skipped)}\n")
        f.write(f"Ambiguas: {len(ambiguous)}\n")
        f.write(f"Stats: {json.dumps(stats, ensure_ascii=False)}\n\n")

        f.write("=== MATCHED ===\n")
        for m in matches:
            f.write(f"{m.rel_path} -> {m.pamt_dir}:{m.entry_path} | full={m.full_path} | {m.size} bytes\n")
        f.write("\n=== CONFLICTOS / POSIBLES CONFLICTOS ===\n")
        if conflicts:
            for line in conflicts:
                f.write(line + "\n")
        else:
            f.write("Sin conflictos detectados contra overlays existentes 0036+.\n")
        f.write("\n=== AMBIGUAS ===\n")
        for line in ambiguous:
            f.write(line + "\n")
        f.write("\n=== SKIPPED ===\n")
        for line in skipped:
            f.write(line + "\n")


def build_or_apply(options: BuildOptions, log: Callable[[str], None]) -> BuildResult:
    total_start = time.perf_counter()
    if not is_game_dir(options.game_dir):
        raise FileNotFoundError("La carpeta del juego no parece válida. Debe contener meta/0.papgt y carpetas 0000/0.pamt.")
    if not options.texture_dir.exists():
        raise FileNotFoundError("La carpeta de texturas no existe.")
    if not options.mod_name.strip():
        raise ValueError("Ponle un nombre al mod.")

    if is_cloud_synced_path(options.texture_dir):
        log("AVISO: la carpeta de texturas parece estar dentro de OneDrive/Dropbox/Cloud. Para máxima velocidad usa una ruta local tipo C:/CD_MOD_WORK/.")
    if is_cloud_synced_path(options.output_dir):
        log("AVISO: la carpeta de salida parece estar dentro de OneDrive/Dropbox/Cloud. El build del PAZ se hace en el juego si Apply está ON, pero reportes/manifests podrían tardar más.")
    log(f"Split PAZ interno: {options.split_gb:.2f} GiB.")

    log("== Scan / matching ==")
    matches, skipped, ambiguous, stats = match_textures(
        options.game_dir,
        options.texture_dir,
        options.allow_unique_filename,
        options.scan_existing_mod_dirs,
        log,
        options.target_pamt_dir,
        options.target_full_prefix,
    )
    if not matches:
        raise RuntimeError("No se pudo matchear ninguna textura. Usa rutas internas completas o activa match por filename único.")

    conflicts = build_conflict_lines(matches, options.game_dir)
    chunks = split_matches(matches, options.split_gb)
    log(f"Texturas matched: {len(matches)}. Chunks/overlays: {len(chunks)}. Skipped: {len(skipped)}. Ambiguas: {len(ambiguous)}.")
    if ambiguous:
        log(f"AVISO: {len(ambiguous)} texturas quedaron fuera por ambigüedad real. Los duplicados equivalentes de PAMT se deduplican automáticamente.")
    # Hash speed status: the log you shared shows the PAZ write is fast,
    # while the pure-Python PAZ CRC/hash dominates total build time.
    try:
        from cdumm.archive.hashlittle import external_hash_helper_path
        helper = external_hash_helper_path()
    except Exception:
        helper = None

    if helper:
        log(f"Fast Hash Helper detectado: {helper}")
        log("CRC PAZ usará helper externo rápido en vez del fallback Python lento.")
    elif not native_hash_available():
        log("AVISO: no detecté Fast Hash Helper ni cdumm_native. El CRC PAZ usará fallback Python y puede tardar mucho.")
        log("Para acelerar, ejecuta BUILD_NATIVE_C_HELPER.bat. Si usas w64devkit, colócalo junto al proyecto o en C:/TempCDUMM/w64devkit para auto-detección.")
    else:
        log("cdumm_native detectado. Se usará cuando esté disponible para hashing.")
    if conflicts:
        log(f"WARN: {len(conflicts)} posibles conflictos con otros overlays. Revisa el reporte.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_mod_name = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in options.mod_name).strip() or "TextureOverlay"
    build_root = options.output_dir / f"{safe_mod_name}_{stamp}"
    report_path = build_root / "overlay_report.txt"
    manifest_path = build_root / "manifest.json"

    overlay_dirs = allocate_overlay_dirs(options.game_dir, len(chunks), None if options.apply_to_game else build_root)
    if options.apply_to_game:
        log(f"Overlays nuevos planeados: {overlay_dirs}. No se deberían sobrescribir overlays existentes; se empieza después del mayor directorio 0000-9999 encontrado.")
    else:
        log(f"Build-only: overlays planeados en carpeta de salida: {overlay_dirs}.")
    if options.dry_run:
        write_report(report_path, options, matches, skipped, ambiguous, conflicts, overlay_dirs, stats)
        manifest = {
            "app": APP_NAME,
            "version": APP_VERSION,
            "dry_run": True,
            "mod_name": options.mod_name,
            "overlay_dirs_planned": overlay_dirs,
            "matched_count": len(matches),
            "skipped_count": len(skipped),
            "ambiguous_count": len(ambiguous),
            "report": str(report_path),
        }
        safe_write(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"))
        log("Dry run terminado. No se escribieron PAZ/PAMT ni meta del juego.")
        return BuildResult(len(matches), len(skipped), len(ambiguous), overlay_dirs, str(build_root), str(manifest_path), str(report_path), False)

    backup_dir = None
    if options.apply_to_game and options.backup_meta:
        backup_dir = copy_current_meta_backup(options.game_dir, log)

    all_overlay_entries: list[OverlayEntry] = []
    chunk_infos: list[dict] = []
    log("== Build overlay PAZ/PAMT ==")
    build_root.mkdir(parents=True, exist_ok=True)

    vanilla_pathc = options.game_dir / "meta" / "0.pathc"
    processed = 0
    for chunk_idx, chunk in enumerate(chunks, start=1):
        overlay_dir = overlay_dirs[chunk_idx - 1]
        log(f"Construyendo overlay {chunk_idx}/{len(chunks)} -> {overlay_dir} ({len(chunk)} texturas)...")

        target_base = options.game_dir if options.apply_to_game else build_root
        out_dir = target_base / overlay_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        entries_for_builder: list[tuple[str, dict]] = [(m.source_path, m.metadata()) for m in chunk]
        chunk_source_mb = sum(m.size for m in chunk) / 1024 / 1024
        log(f"  Streaming build: {len(chunk)} texturas ({chunk_source_mb:.1f} MB fuente). No se carga el PAZ completo a RAM.")

        last_progress = [0.0]
        def progress_cb(i: int, total: int, entry_name: str = "") -> None:
            now = time.time()
            if entry_name.startswith("[stage]"):
                log(f"  overlay {chunk_idx}/{len(chunks)}: {entry_name[7:].strip()}")
                return
            if now - last_progress[0] > 0.75 or i == total - 1:
                last_progress[0] = now
                log(f"  overlay {chunk_idx}/{len(chunks)}: {i + 1}/{total} {entry_name}")

        log(f"  Escribiendo directo a {overlay_dir}/0.paz.tmp y calculando CRC por streaming...")
        chunk_start = time.perf_counter()
        pamt_bytes, overlay_packed, paz_size = build_overlay_to_file(
            entries_for_builder,
            out_dir / "0.paz",
            game_dir=options.game_dir,
            progress_cb=progress_cb,
            vanilla_pathc_path=vanilla_pathc if vanilla_pathc.exists() else None,
        )
        chunk_elapsed = time.perf_counter() - chunk_start
        speed_mb_s = (paz_size / 1024 / 1024) / chunk_elapsed if chunk_elapsed > 0 else 0.0
        remaining_mb = sum(sum(m.size for m in c) for c in chunks[chunk_idx:]) / 1024 / 1024
        eta = (remaining_mb / speed_mb_s) if speed_mb_s > 0 else 0.0
        log(f"  overlay {chunk_idx}/{len(chunks)}: tiempo build+CRC {chunk_elapsed/60:.1f} min ({chunk_elapsed:.1f} s), velocidad efectiva {speed_mb_s:.1f} MB/s, ETA overlays restantes {fmt_eta(eta)}.")
        if paz_size > U32_MAX:
            raise RuntimeError(f"El chunk {chunk_idx} quedó arriba de 4 GiB. Baja el split size o hay un archivo individual demasiado grande.")

        safe_write(out_dir / "0.pamt", pamt_bytes)
        all_overlay_entries.extend(overlay_packed)
        processed += len(chunk)
        chunk_infos.append({
            "overlay_dir": overlay_dir,
            "entry_count": len(chunk),
            "paz_size": paz_size,
            "pamt_size": len(pamt_bytes),
        })
        log(f"  escrito {overlay_dir}/0.paz ({paz_size / 1024 / 1024:.1f} MB) + 0.pamt")
        del entries_for_builder, pamt_bytes, overlay_packed

    applied = False
    underlay_pathc_entries: list[dict[str, Any]] = []
    if options.apply_to_game:
        underlay_pathc_entries = snapshot_pathc_underlay(options.game_dir, matches, all_overlay_entries, log)
        log("== Actualizando meta/0.pathc ==")
        pathc_bytes = update_pathc_for_matches(options.game_dir, matches, all_overlay_entries, log)
        if pathc_bytes is not None:
            safe_write(options.game_dir / "meta" / "0.pathc", pathc_bytes)

        log("== Reconstruyendo meta/0.papgt ==")
        modified_pamts: dict[str, bytes] = {}
        for od in overlay_dirs:
            modified_pamts[od] = (options.game_dir / od / "0.pamt").read_bytes()
        papgt_bytes = PapgtManager(options.game_dir).rebuild(modified_pamts=modified_pamts)
        safe_write(options.game_dir / "meta" / "0.papgt", papgt_bytes)
        applied = True
        log("Aplicado al juego: overlay dirs + PATHC + PAPGT actualizados.")
    else:
        log("Build only: paquete creado en output. Para usarlo en el juego necesitas aplicar esos overlays y actualizar PAPGT/PATHC.")

    write_report(report_path, options, matches, skipped, ambiguous, conflicts, overlay_dirs, stats)
    manifest = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mod_name": options.mod_name,
        "game_dir": str(options.game_dir),
        "texture_dir": str(options.texture_dir),
        "target_pamt_dir": _normalize_target_pamt_dir(options.target_pamt_dir),
        "target_full_prefix": _normalize_target_prefix(options.target_full_prefix),
        "output_dir": str(build_root),
        "applied_to_game": applied,
        "backup_dir": str(backup_dir) if backup_dir else "",
        "overlay_dirs": overlay_dirs,
        "chunks": chunk_infos,
        "matched_count": len(matches),
        "skipped_count": len(skipped),
        "ambiguous_count": len(ambiguous),
        "report": str(report_path),
        "matches": [asdict(m) for m in matches],
        "overlay_entries": [asdict(e) for e in all_overlay_entries],
        "pathc_headers": build_pathc_header_cache(matches, log),
        "underlay_pathc_entries": underlay_pathc_entries,
        "pamt_index_cache": str(_pamt_cache_path(options.game_dir)),
    }
    safe_write(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"))
    if applied:
        try:
            register_applied_manifest(options.game_dir, manifest_path, manifest, log)
        except Exception as e:
            log(f"WARN: no pude actualizar registry maestro: {e}")
    log(f"Reporte: {report_path}")
    total_elapsed = time.perf_counter() - total_start
    log(f"Tiempo total: {total_elapsed/60:.1f} min ({total_elapsed:.1f} s).")
    log(f"Manifest: {manifest_path}")
    return BuildResult(len(matches), len(skipped), len(ambiguous), overlay_dirs, str(build_root), str(manifest_path), str(report_path), applied)



def _is_valid_crimson_desert_dir(path: Path) -> bool:
    try:
        return path.exists() and (path / "meta" / "0.papgt").exists() and any(path.glob("[0-9][0-9][0-9][0-9]/0.pamt"))
    except Exception:
        return False


def _steam_library_candidates_from_vdf(vdf_path: Path) -> list[Path]:
    """Parse Steam libraryfolders.vdf lightly without external deps."""
    out: list[Path] = []
    if not vdf_path.exists():
        return out
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out
    # Modern Steam VDF has lines like: "path"  "G:\\SteamLibrary"
    for m in re.finditer(r'"path"\s+"([^"]+)"', text, re.IGNORECASE):
        raw = m.group(1).replace('\\\\', '\\')
        if raw:
            out.append(Path(raw))
    # Older/simple format can have numeric keys directly pointing to paths.
    for m in re.finditer(r'"\d+"\s+"([A-Za-z]:\\[^"]+)"', text):
        raw = m.group(1).replace('\\\\', '\\')
        if raw:
            out.append(Path(raw))
    return out


def _steam_install_candidates() -> list[Path]:
    candidates: list[Path] = []
    # Environment/default locations.
    for env_name in ["PROGRAMFILES(X86)", "PROGRAMFILES"]:
        base = os.environ.get(env_name)
        if base:
            candidates.append(Path(base) / "Steam")
    candidates.extend([
        Path("C:/Program Files (x86)/Steam"),
        Path("C:/Program Files/Steam"),
    ])
    # Windows registry, if available.
    if os.name == "nt":
        try:
            import winreg  # type: ignore
            reg_paths = [
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
            ]
            for hive, key_path in reg_paths:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        val, _ = winreg.QueryValueEx(key, "SteamPath")
                        if val:
                            candidates.append(Path(str(val)))
                except Exception:
                    pass
        except Exception:
            pass
    # De-dup preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for c in candidates:
        try:
            k = str(c.expanduser()).lower()
        except Exception:
            k = str(c).lower()
        if k not in seen:
            seen.add(k)
            unique.append(c)
    return unique


def detect_crimson_desert_game_dir() -> Path | None:
    """Best-effort auto detection for lazy-friendly setup.

    Checks Steam registry/default install, Steam libraryfolders.vdf, a few common
    library roots on local drives, and validates meta/0.papgt + 0000/0.pamt.
    """
    libraries: list[Path] = []
    steam_roots = _steam_install_candidates()
    for steam in steam_roots:
        if steam.exists():
            libraries.append(steam)
            libraries.extend(_steam_library_candidates_from_vdf(steam / "steamapps" / "libraryfolders.vdf"))

    # Common library root guesses. Kept shallow to avoid scanning full drives.
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{drive}:/")
        libraries.extend([
            root / "SteamLibrary",
            root / "Steam",
            root / "Games" / "SteamLibrary",
            root / "SteamLibrarySSD",
        ])

    # Also check one level above the current tool, useful if users keep it near game folders.
    try:
        here = Path(__file__).resolve()
        libraries.extend([here.parent, here.parent.parent])
    except Exception:
        pass

    names = ["Crimson Desert", "CrimsonDesert"]
    seen: set[str] = set()
    for lib in libraries:
        try:
            lib = lib.expanduser()
        except Exception:
            pass
        for base in [lib / "steamapps" / "common", lib / "SteamApps" / "common", lib / "common", lib]:
            for name in names:
                cand = base / name
                key = str(cand).lower()
                if key in seen:
                    continue
                seen.add(key)
                if _is_valid_crimson_desert_dir(cand):
                    return cand
    return None


def find_latest_meta_backup(game_dir: Path) -> Path | None:
    root = game_dir / "CDTextureOverlayBuilder" / "backups"
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and (p / "meta" / "0.papgt").exists()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.name)[-1]


def restore_meta_from_backup(game_dir: Path, backup_dir: Path, log: Callable[[str], None]) -> None:
    if not backup_dir.exists():
        raise FileNotFoundError(f"No existe backup: {backup_dir}")
    restored = 0
    for rel in ["meta/0.papgt", "meta/0.pathc"]:
        src = backup_dir / rel
        dst = game_dir / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1
            log(f"Restaurado {rel} desde {backup_dir}")
    if restored == 0:
        raise FileNotFoundError("El backup no tiene meta/0.papgt ni meta/0.pathc.")



def _pathc_vpath_for_match(m: MatchedFile, oe: OverlayEntry | None = None) -> str:
    """Return the virtual path used by meta/0.pathc for a matched texture."""
    if oe is not None and getattr(oe, "dir_path", ""):
        return "/" + str(oe.dir_path).strip("/") + "/" + m.filename
    return "/" + str(m.full_path).strip("/")


def _match_to_overlay_map(matches: list[MatchedFile], overlay_entries: list[OverlayEntry]) -> dict[str, OverlayEntry]:
    packed_by_entry: dict[str, OverlayEntry] = {}
    packed_by_name: dict[str, list[OverlayEntry]] = {}
    for oe in overlay_entries:
        if getattr(oe, "entry_path", ""):
            packed_by_entry[norm_virtual(oe.entry_path)] = oe
        packed_by_name.setdefault(oe.filename.lower(), []).append(oe)
    out: dict[str, OverlayEntry] = {}
    for m in matches:
        oe = packed_by_entry.get(norm_virtual(m.entry_path))
        if oe is None:
            cands = packed_by_name.get(m.filename.lower(), [])
            if len(cands) == 1:
                oe = cands[0]
        if oe is not None:
            out[norm_virtual(m.entry_path)] = oe
    return out


def pathc_vpaths_for_matches(matches: list[MatchedFile], overlay_entries: list[OverlayEntry]) -> list[str]:
    mapping = _match_to_overlay_map(matches, overlay_entries)
    vpaths: list[str] = []
    seen: set[str] = set()
    for m in matches:
        if not m.entry_path.lower().endswith(".dds"):
            continue
        oe = mapping.get(norm_virtual(m.entry_path))
        vpath = _pathc_vpath_for_match(m, oe)
        key = vpath.lower()
        if key not in seen:
            seen.add(key)
            vpaths.append(vpath)
    return vpaths


def snapshot_pathc_underlay_from_file(pathc_path: Path, matches: list[MatchedFile], overlay_entries: list[OverlayEntry], log: Callable[[str], None] | None = None, label: str = "PATHC underlay") -> list[dict[str, Any]]:
    vpaths = pathc_vpaths_for_matches(matches, overlay_entries)
    if not vpaths or not pathc_path.exists():
        return []
    try:
        pathc = read_pathc(pathc_path)
    except Exception as e:
        if log:
            log(f"WARN: no pude snapshot {label}: {e}")
        return []
    snap: list[dict[str, Any]] = []
    for vpath in vpaths:
        h = get_path_hash(vpath)
        pos = bisect.bisect_left(pathc.key_hashes, h)
        if pos < len(pathc.key_hashes) and pathc.key_hashes[pos] == h:
            me = pathc.map_entries[pos]
            dds_idx = me.selector & 0xFFFF
            rec_hex = ""
            if 0 <= dds_idx < len(pathc.dds_records):
                rec_hex = pathc.dds_records[dds_idx].hex()
            snap.append({
                "vpath": vpath,
                "exists": True,
                "selector": int(me.selector) & 0xFFFFFFFF,
                "record": rec_hex,
                "m": [me.m1, me.m2, me.m3, me.m4],
            })
        else:
            snap.append({"vpath": vpath, "exists": False})
    if log:
        log(f"{label} snapshot guardado: {sum(1 for x in snap if x.get('exists'))} existentes, {sum(1 for x in snap if not x.get('exists'))} ausentes.")
    return snap


def snapshot_pathc_underlay(game_dir: Path, matches: list[MatchedFile], overlay_entries: list[OverlayEntry], log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Capture the current PATHC entries that will be overwritten by this mod.

    This enables dynamic Hold: later we can remove only this texture overlay and
    restore whatever PATHC entries were underneath at the last Build/Reapply,
    without restoring an old full meta backup that could delete newer mods.
    """
    return snapshot_pathc_underlay_from_file(game_dir / "meta" / "0.pathc", matches, overlay_entries, log, "PATHC underlay")


def _remove_pathc_hash(pathc: Any, vpath: str) -> bool:
    h = get_path_hash(vpath)
    pos = bisect.bisect_left(pathc.key_hashes, h)
    if pos < len(pathc.key_hashes) and pathc.key_hashes[pos] == h:
        pathc.key_hashes.pop(pos)
        pathc.map_entries.pop(pos)
        return True
    return False


def _restore_pathc_snapshot_item(pathc: Any, item: dict[str, Any]) -> tuple[int, int]:
    """Restore one snapshot item. Returns (restored, removed)."""
    vpath = str(item.get("vpath", "") or "")
    if not vpath:
        return 0, 0
    exists = bool(item.get("exists"))
    if not exists:
        return (0, 1) if _remove_pathc_hash(pathc, vpath) else (0, 0)

    rec_hex = str(item.get("record", "") or "")
    m_raw = item.get("m", [0, 0, 0, 0])
    try:
        m = tuple(int(x) & 0xFFFFFFFF for x in list(m_raw)[:4])
    except Exception:
        m = (0, 0, 0, 0)
    while len(m) < 4:  # type: ignore[arg-type]
        m = tuple(list(m) + [0])  # type: ignore[assignment]
    try:
        rec = bytes.fromhex(rec_hex) if rec_hex else b""
    except ValueError:
        rec = b""
    selector_raw = item.get("selector", None)
    try:
        original_selector = int(selector_raw) & 0xFFFFFFFF if selector_raw is not None else None
    except Exception:
        original_selector = None

    # Important: some vanilla/external PATHC selectors use 0xFFFF as the low
    # 16-bit value. That is a sentinel and does not point to a DDS template
    # record. Older removal code converted an empty snapshot record into DDS
    # index 0, changing e.g. 0x0402FFFF -> 0x04020000. That tiny selector
    # change can still break/crash the game even when PAZ/PAPGT are restored.
    # If the snapshot has no DDS record but includes the full selector, restore
    # that selector exactly.
    if not rec and original_selector is not None:
        selector = original_selector
    else:
        if rec:
            record_size = pathc.header.dds_record_size
            if len(rec) != record_size:
                rec = rec[:record_size].ljust(record_size, b"\x00")
            try:
                dds_idx = pathc.dds_records.index(rec)
            except ValueError:
                pathc.dds_records.append(rec)
                dds_idx = len(pathc.dds_records) - 1
        else:
            dds_idx = 0

        # Preserve the original selector upper bits. Older builds forced 0xFFFFxxxx,
        # which can corrupt vanilla/external PATHC entries when removing a texture build.
        # New manifests store the full selector; when absent we keep the old fallback.
        if original_selector is not None:
            selector = (original_selector & 0xFFFF0000) | (dds_idx & 0xFFFF)
        else:
            selector = 0xFFFF0000 | (dds_idx & 0xFFFF)
    h = get_path_hash(vpath)
    pos = bisect.bisect_left(pathc.key_hashes, h)
    if pos < len(pathc.key_hashes) and pathc.key_hashes[pos] == h:
        pathc.map_entries[pos].selector = selector
        pathc.map_entries[pos].m1 = m[0]
        pathc.map_entries[pos].m2 = m[1]
        pathc.map_entries[pos].m3 = m[2]
        pathc.map_entries[pos].m4 = m[3]
    else:
        pathc.key_hashes.insert(pos, h)
        pathc.map_entries.insert(pos, PathcMapEntry(selector, m[0], m[1], m[2], m[3]))
    return 1, 0


def compact_pathc_dds_records(pathc: Any, log: Callable[[str], None] | None = None) -> int:
    """Remove unreferenced DDS records and remap selectors safely.

    Removing a texture build can leave orphan DDS templates appended by the
    overlay install. Some game code appears sensitive to the DDS record table,
    even when the hash table no longer references those records. This compacts
    the table while preserving selector upper bits and collision entries.
    Returns the number of records removed.
    """
    old_count = len(pathc.dds_records)
    if old_count <= 0:
        return 0

    used: set[int] = set()
    for entry in pathc.map_entries:
        idx = int(entry.selector) & 0xFFFF
        if 0 <= idx < old_count:
            used.add(idx)
    for entry in getattr(pathc, "collision_entries", []) or []:
        idx = int(getattr(entry, "dds_index", 0))
        if 0 <= idx < old_count:
            used.add(idx)

    if not used:
        return 0
    if len(used) == old_count:
        return 0

    ordered = sorted(used)
    remap = {old: new for new, old in enumerate(ordered)}
    pathc.dds_records = [pathc.dds_records[i] for i in ordered]

    for entry in pathc.map_entries:
        idx = int(entry.selector) & 0xFFFF
        if idx in remap:
            entry.selector = (int(entry.selector) & 0xFFFF0000) | (remap[idx] & 0xFFFF)
    for entry in getattr(pathc, "collision_entries", []) or []:
        idx = int(getattr(entry, "dds_index", 0))
        if idx in remap:
            entry.dds_index = remap[idx]

    removed = old_count - len(pathc.dds_records)
    if log and removed:
        log(f"PATHC compact: removed {removed} orphan DDS template records.")
    return removed


def restore_pathc_underlay_for_mods(game_dir: Path, mods: list[dict[str, Any]], log: Callable[[str], None]) -> None:
    """Dynamic Hold: remove only this tool's texture entries from current PATHC.

    It restores the per-path underlay captured before the latest Build/Reapply,
    so external mods added later remain in meta/0.pathc. If an old manifest lacks
    underlay data, it safely removes this tool's paths as fallback.
    """
    pathc_path = game_dir / "meta" / "0.pathc"
    if not pathc_path.exists():
        log("WARN: meta/0.pathc no existe. No hay PATHC que limpiar durante Hold.")
        return
    pathc = read_pathc(pathc_path)
    restored = 0
    removed = 0
    fallback_removed = 0
    missing_snapshot = 0
    seen_snapshot_vpaths: set[str] = set()
    for mod in mods:
        try:
            _man_path, manifest = _load_registry_manifest(mod)
        except Exception as e:
            log(f"WARN: no pude leer manifest para Hold dinámico {mod.get('mod_name')}: {e}")
            continue
        snap = manifest.get("underlay_pathc_entries")
        try:
            matches = [_matched_from_manifest(x) for x in manifest.get("matches", [])]
            entries = [_overlay_entry_from_manifest(x) for x in manifest.get("overlay_entries", [])]
        except Exception:
            matches = []
            entries = []

        # Manifests from older builds may have underlay entries without the full
        # selector. If available, rebuild the snapshot from the original backup
        # PATHC so removal can restore vanilla/external entries exactly.
        if isinstance(snap, list) and snap and any(isinstance(x, dict) and x.get("exists") and "selector" not in x for x in snap):
            try:
                backup_pathc = Path(str(manifest.get("backup_dir", ""))) / "meta" / "0.pathc"
                backup_snap = snapshot_pathc_underlay_from_file(backup_pathc, matches, entries, log, "PATHC backup exact fallback") if backup_pathc.exists() else []
                if backup_snap:
                    snap = backup_snap
            except Exception as e:
                log(f"WARN: no pude generar fallback exacto desde backup PATHC: {e}")

        if isinstance(snap, list) and snap:
            for item in snap:
                if isinstance(item, dict):
                    key = str(item.get("vpath", "")).lower()
                    if key and key in seen_snapshot_vpaths:
                        continue
                    if key:
                        seen_snapshot_vpaths.add(key)
                    r, d = _restore_pathc_snapshot_item(pathc, item)
                    restored += r
                    removed += d
        else:
            missing_snapshot += 1
            # Fallback for manifests created before underlay snapshots. Prefer the build's
            # backup PATHC if available; otherwise remove our hashes only.
            try:
                backup_pathc = Path(str(manifest.get("backup_dir", ""))) / "meta" / "0.pathc"
                backup_snap = snapshot_pathc_underlay_from_file(backup_pathc, matches, entries, log, "PATHC backup fallback") if backup_pathc.exists() else []
                if backup_snap:
                    for item in backup_snap:
                        key = str(item.get("vpath", "")).lower()
                        if key and key in seen_snapshot_vpaths:
                            continue
                        if key:
                            seen_snapshot_vpaths.add(key)
                        r, d = _restore_pathc_snapshot_item(pathc, item)
                        restored += r
                        removed += d
                else:
                    for vpath in pathc_vpaths_for_matches(matches, entries):
                        key = vpath.lower()
                        if key in seen_snapshot_vpaths:
                            continue
                        seen_snapshot_vpaths.add(key)
                        if _remove_pathc_hash(pathc, vpath):
                            fallback_removed += 1
            except Exception as e:
                log(f"WARN: fallback PATHC cleanup falló para {mod.get('mod_name')}: {e}")
    compact_pathc_dds_records(pathc, log)
    pathc.header.dds_record_count = len(pathc.dds_records)
    pathc.header.hash_count = len(pathc.key_hashes)
    safe_write(pathc_path, serialize_pathc(pathc))
    log(f"Hold dinámico PATHC: {restored} entradas restauradas bajo overlay, {removed} removidas por ausencia previa, {fallback_removed} removidas por fallback.")
    if missing_snapshot:
        log(f"WARN: {missing_snapshot} manifest(s) no tenían underlay snapshot; se usó limpieza fallback. Reapply una vez con v1.0.0 para mejorar futuros Holds.")



def _parse_manifest_time(manifest: dict[str, Any]) -> str:
    return str(manifest.get("created_at", "") or "")


def find_clean_meta_backup_for_active_mods(game_dir: Path, active_mods: list[dict[str, Any]], log: Callable[[str], None] | None = None) -> Path | None:
    """Pick the best clean meta backup before texture overlays were applied.

    When multiple texture passes exist, the latest backup can still contain an
    earlier texture pass. For Hold we want the baseline before the first active
    texture overlay, so other managers see a clean game/meta state.
    """
    candidates: list[tuple[str, Path, str]] = []
    for mod in active_mods:
        try:
            man_path, manifest = _load_registry_manifest(mod)
            backup_raw = str(manifest.get("backup_dir", "") or "")
            if not backup_raw:
                continue
            b = Path(backup_raw)
            if b.exists() and ((b / "meta" / "0.papgt").exists() or (b / "meta" / "0.pathc").exists()):
                candidates.append((_parse_manifest_time(manifest), b, str(man_path)))
        except Exception as e:
            if log:
                log(f"WARN: no pude leer backup base del registry para {mod.get('mod_name')}: {e}")
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], str(x[1])))
    chosen = candidates[0][1]
    if log:
        log(f"Hold: backup base seleccionado para limpiar meta: {chosen}")
        if len(candidates) > 1:
            log("Hold: se usó el backup del primer texture overlay activo, no el último, para evitar dejar entradas de overlays anteriores.")
    return chosen


def uninstall_from_manifest(manifest_path: Path, delete_overlays: bool, log: Callable[[str], None]) -> None:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    game_dir = Path(manifest.get("game_dir", ""))
    if not game_dir.exists():
        raise FileNotFoundError("El game_dir del manifest no existe. No puedo desinstalar automáticamente.")
    backup_dir = Path(manifest.get("backup_dir", "")) if manifest.get("backup_dir") else None
    if backup_dir and backup_dir.exists():
        restore_meta_from_backup(game_dir, backup_dir, log)
    else:
        log("WARN: el manifest no tiene backup válido. No se restauró meta; solo se borrarán overlays si lo pediste.")

    if delete_overlays:
        for od in manifest.get("overlay_dirs", []):
            d = game_dir / str(od)
            if d.exists() and d.is_dir() and d.name.isdigit() and len(d.name) == 4:
                shutil.rmtree(d)
                log(f"Overlay eliminado: {d}")
    log("Uninstall/restore terminado. Recomendado: probar el juego antes de instalar otro mod.")


REGISTRY_SCHEMA = 1


def registry_root(game_dir: Path) -> Path:
    return game_dir / "CDTextureOverlayBuilder"


def registry_path(game_dir: Path) -> Path:
    return registry_root(game_dir) / "texture_overlay_registry.json"


def registry_manifest_dir(game_dir: Path) -> Path:
    return registry_root(game_dir) / "manifests"


def registry_hold_root(game_dir: Path) -> Path:
    # Outside the game folder so other managers do not see the extra 0037/0038/etc.
    safe_game_name = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in game_dir.name).strip() or "Crimson Desert"
    return game_dir.parent / "CDTextureOverlayBuilder_HOLD" / safe_game_name


def _new_registry(game_dir: Path) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema": REGISTRY_SCHEMA,
        "app": APP_NAME,
        "created_at": now,
        "updated_at": now,
        "game_dir": str(game_dir),
        "mods": [],
    }


def load_master_registry(game_dir: Path) -> dict[str, Any]:
    rp = registry_path(game_dir)
    if not rp.exists():
        return _new_registry(game_dir)
    try:
        with open(rp, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA:
            return _new_registry(game_dir)
        data.setdefault("mods", [])
        return data
    except Exception:
        return _new_registry(game_dir)


def save_master_registry(game_dir: Path, registry: dict[str, Any]) -> None:
    registry["schema"] = REGISTRY_SCHEMA
    registry["app"] = APP_NAME
    registry["game_dir"] = str(game_dir)
    registry["updated_at"] = datetime.now().isoformat(timespec="seconds")
    safe_write(registry_path(game_dir), json.dumps(registry, indent=2, ensure_ascii=False).encode("utf-8"))


def _safe_id_part(value: str) -> str:
    value = "".join(c if c.isalnum() or c in "-_" else "_" for c in (value or "")).strip("_")
    return value[:48] or "TextureOverlay"


def _mod_id_from_manifest(manifest: dict[str, Any]) -> str:
    raw = "|".join([
        str(manifest.get("mod_name", "TextureOverlay")),
        str(manifest.get("created_at", "")),
        str(manifest.get("target_pamt_dir", "")),
        str(manifest.get("target_full_prefix", "")),
        ",".join(str(x) for x in manifest.get("overlay_dirs", [])),
    ])
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{_safe_id_part(str(manifest.get('mod_name', 'TextureOverlay')))}_{digest}"




def _friendly_manifest_filename(manifest: dict[str, Any], mod_id: str) -> str:
    """Human-readable manifest copy name stored in CDTextureOverlayBuilder/manifests.

    Older versions used only <mod>_<hash>.json. This one includes timestamp and
    target filter so users can identify what each registry manifest belongs to.
    """
    raw_name = str(manifest.get("mod_name", "TextureOverlay") or "TextureOverlay")
    name = _safe_id_part(raw_name)
    created = str(manifest.get("created_at", "") or datetime.now().isoformat(timespec="seconds"))
    digits = "".join(c for c in created if c.isdigit())
    stamp = digits[:14] if len(digits) >= 14 else datetime.now().strftime("%Y%m%d%H%M%S")
    pamt = _safe_id_part(str(manifest.get("target_pamt_dir", "") or "all"))
    prefix = _safe_id_part(str(manifest.get("target_full_prefix", "") or "all"))
    digest = mod_id.rsplit("_", 1)[-1]
    return f"{name}_{stamp}_{pamt}_{prefix}_{digest}.json"


def _manifest_overlay_state(game_dir: Path, manifest: dict[str, Any]) -> tuple[str | None, list[dict[str, str]]]:
    """Return ('active'|'held'|None, held_overlays) for a manifest overlay set."""
    overlay_dirs = [str(x) for x in manifest.get("overlay_dirs", [])]
    if not overlay_dirs:
        return None, []
    active_ok = all((game_dir / od / "0.paz").exists() and (game_dir / od / "0.pamt").exists() for od in overlay_dirs)
    if active_ok:
        return "active", []
    hold_root = registry_hold_root(game_dir)
    held_overlays: list[dict[str, str]] = []
    held_ok = True
    for od in overlay_dirs:
        hp = hold_root / od
        # v0.3.8+ parks overlays under HOLD/<Game>/<mod_id>/<0037>.
        # Older/repair flows may use HOLD/<Game>/<0037>. Support both.
        if not (hp.exists() and (hp / "0.paz").exists() and (hp / "0.pamt").exists()):
            nested = []
            try:
                nested = [p for p in hold_root.glob(f"*/{od}") if (p / "0.paz").exists() and (p / "0.pamt").exists()]
            except Exception:
                nested = []
            hp = nested[0] if nested else hp
        if hp.exists() and (hp / "0.paz").exists() and (hp / "0.pamt").exists():
            held_overlays.append({"overlay_dir": od, "held_path": str(hp)})
        else:
            held_ok = False
            break
    if held_ok and held_overlays:
        return "held", held_overlays
    return None, []


def _registry_entry_from_manifest(game_dir: Path, manifest_path: Path, manifest: dict[str, Any], status: str = "active", held_overlays: list[dict[str, str]] | None = None) -> dict[str, Any]:
    mod_id = _mod_id_from_manifest(manifest)
    return {
        "mod_id": mod_id,
        "mod_name": manifest.get("mod_name", "TextureOverlay"),
        "status": status,
        "created_at": manifest.get("created_at", datetime.now().isoformat(timespec="seconds")),
        "registered_at": datetime.now().isoformat(timespec="seconds"),
        "manifest_copy": str(manifest_path),
        "original_manifest": str(manifest.get("original_manifest", "")),
        "overlay_dirs": [str(x) for x in manifest.get("overlay_dirs", [])],
        "held_overlays": held_overlays or [],
        "target_pamt_dir": manifest.get("target_pamt_dir", ""),
        "target_full_prefix": manifest.get("target_full_prefix", ""),
        "matched_count": int(manifest.get("matched_count", 0) or 0),
    }


def auto_repair_registry_from_local_manifests(game_dir: Path, log: Callable[[str], None] | None = None) -> int:
    """Auto-import registry entries from CDTextureOverlayBuilder/manifests/*.json.

    This makes Hold/Reapply work after reopening the tool without asking the user
    to manually pick a manifest. It only imports manifests whose overlays are
    physically present in the game folder or parked in the HOLD folder.
    """
    man_dir = registry_manifest_dir(game_dir)
    if not man_dir.exists():
        return 0
    registry = load_master_registry(game_dir)
    mods = list(registry.get("mods", []))
    existing_ids = {str(m.get("mod_id")) for m in mods}
    imported = 0
    for mp in sorted(man_dir.glob("*.json")):
        try:
            with open(mp, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            if not isinstance(manifest, dict) or not manifest.get("applied_to_game"):
                continue
            # If the manifest belongs to another game folder, skip it.
            mgame = Path(str(manifest.get("game_dir", ""))).expanduser()
            try:
                if mgame and mgame.exists() and mgame.resolve() != game_dir.resolve():
                    continue
            except Exception:
                pass
            mod_id = _mod_id_from_manifest(manifest)
            if mod_id in existing_ids:
                continue
            state, held_overlays = _manifest_overlay_state(game_dir, manifest)
            if not state:
                continue
            entry = _registry_entry_from_manifest(game_dir, mp, manifest, state, held_overlays)
            mods.append(entry)
            existing_ids.add(mod_id)
            imported += 1
        except Exception as e:
            if log:
                log(f"WARN: no pude auto-importar manifest {mp.name}: {e}")
    if imported:
        registry["mods"] = mods
        save_master_registry(game_dir, registry)
        if log:
            log(f"Registry auto-repair: importados {imported} manifest(s) desde {man_dir}")
    return imported

def register_applied_manifest(game_dir: Path, manifest_path: Path, manifest: dict[str, Any], log: Callable[[str], None]) -> None:
    """Register an applied build in a master registry stored inside the game folder.

    A copy of the full manifest is stored in CDTextureOverlayBuilder/manifests so
    the user can move the report/output folder later and still use Hold/Reapply.
    """
    if not manifest.get("applied_to_game"):
        return
    registry = load_master_registry(game_dir)
    mod_id = _mod_id_from_manifest(manifest)
    man_dir = registry_manifest_dir(game_dir)
    man_dir.mkdir(parents=True, exist_ok=True)
    manifest = dict(manifest)
    manifest["original_manifest"] = str(manifest_path)
    man_copy = man_dir / _friendly_manifest_filename(manifest, mod_id)
    safe_write(man_copy, json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"))

    entry = _registry_entry_from_manifest(game_dir, man_copy, manifest, "active", [])
    entry["original_manifest"] = str(manifest_path)
    mods = [m for m in registry.get("mods", []) if m.get("mod_id") != mod_id]
    mods.append(entry)
    registry["mods"] = mods
    save_master_registry(game_dir, registry)
    log(f"Registry maestro actualizado: {registry_path(game_dir)}")
    log(f"Mod registrado: {entry['mod_name']} ({mod_id}), overlays={entry['overlay_dirs']}")
    # Verify immediately so failures are visible in the build log.
    check = load_master_registry(game_dir)
    if not any(m.get("mod_id") == mod_id and m.get("status") == "active" for m in check.get("mods", [])):
        raise RuntimeError(f"Registry guardado pero no pude verificar el mod activo: {registry_path(game_dir)}")




def register_manifest_file_to_registry(manifest_path: Path, log: Callable[[str], None]) -> None:
    """Repair/import the master registry from a manifest.json created by this tool.

    This is useful if an older build applied overlays correctly but the registry was
    not saved, or if the user moved/closed the app before using Hold/Reapply.
    """
    if not manifest_path.exists():
        raise FileNotFoundError(f"No existe manifest: {manifest_path}")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    game_dir = Path(str(manifest.get("game_dir", ""))).expanduser()
    if not game_dir.exists():
        raise FileNotFoundError(f"El game_dir del manifest no existe: {game_dir}")
    if not manifest.get("applied_to_game"):
        raise RuntimeError("Ese manifest no está marcado como aplicado al juego. No lo registro para Hold/Reapply.")
    overlay_dirs = [str(x) for x in manifest.get("overlay_dirs", [])]
    if not overlay_dirs:
        raise RuntimeError("El manifest no contiene overlay_dirs.")
    missing = [od for od in overlay_dirs if not (game_dir / od / "0.paz").exists() or not (game_dir / od / "0.pamt").exists()]
    if missing:
        raise RuntimeError("El manifest apunta a overlays que no existen en el juego: " + ", ".join(missing))
    register_applied_manifest(game_dir, manifest_path, manifest, log)
    registry = load_master_registry(game_dir)
    active = [m for m in registry.get("mods", []) if m.get("status") == "active"]
    if not active:
        raise RuntimeError(f"Intenté registrar el manifest, pero el registry sigue sin mods activos: {registry_path(game_dir)}")
    log(f"Repair registry OK. Mods activos registrados: {len(active)}")


def scan_registry_status(game_dir: Path) -> dict[str, Any]:
    auto_repair_registry_from_local_manifests(game_dir, None)
    registry = load_master_registry(game_dir)
    mods = registry.get("mods", [])
    active = [m for m in mods if m.get("status") == "active"]
    held = [m for m in mods if m.get("status") == "held"]
    return {
        "registry_path": str(registry_path(game_dir)),
        "mods": len(mods),
        "active": len(active),
        "held": len(held),
        "active_overlays": [od for m in active for od in m.get("overlay_dirs", [])],
        "held_mods": [m.get("mod_name") or m.get("mod_id") for m in held],
    }

def _load_registry_manifest(mod: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    candidates = [mod.get("manifest_copy"), mod.get("original_manifest")]
    for raw in candidates:
        if not raw:
            continue
        p = Path(str(raw))
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return p, json.load(f)
    raise FileNotFoundError(f"No encontré manifest para el mod registrado {mod.get('mod_name') or mod.get('mod_id')}")


def _save_registry_manifest(path: Path, manifest: dict[str, Any]) -> None:
    safe_write(path, json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"))


def _matched_from_manifest(raw: dict[str, Any]) -> MatchedFile:
    allowed = {f.name for f in MatchedFile.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    clean = {k: raw.get(k) for k in allowed if k in raw}
    return MatchedFile(**clean)  # type: ignore[arg-type]


def _overlay_entry_from_manifest(raw: dict[str, Any]) -> OverlayEntry:
    allowed = {"dir_path", "filename", "paz_offset", "comp_size", "decomp_size", "flags", "dds_m_values", "dds_last4", "entry_path"}
    clean = {k: raw.get(k) for k in allowed if k in raw}
    if isinstance(clean.get("dds_m_values"), list):
        clean["dds_m_values"] = tuple(clean["dds_m_values"])
    return OverlayEntry(**clean)  # type: ignore[arg-type]


def _next_free_overlay_dir(game_dir: Path, reserved: set[str] | None = None) -> str:
    reserved = reserved or set()
    for n in range(36, 10000):
        name = f"{n:04d}"
        if name in reserved:
            continue
        if not (game_dir / name).exists():
            return name
    raise RuntimeError("No encontré un número libre 0036-9999 para restaurar overlays.")


def _rewrite_manifest_overlay_dirs(manifest: dict[str, Any], rename_map: dict[str, str]) -> dict[str, Any]:
    if not rename_map:
        return manifest
    manifest = dict(manifest)
    manifest["overlay_dirs"] = [rename_map.get(str(x), str(x)) for x in manifest.get("overlay_dirs", [])]
    chunks = []
    for ch in manifest.get("chunks", []):
        if isinstance(ch, dict):
            ch = dict(ch)
            ch["overlay_dir"] = rename_map.get(str(ch.get("overlay_dir", "")), str(ch.get("overlay_dir", "")))
        chunks.append(ch)
    manifest["chunks"] = chunks
    manifest["renamed_overlay_dirs"] = {**manifest.get("renamed_overlay_dirs", {}), **rename_map}
    manifest["last_renamed_at"] = datetime.now().isoformat(timespec="seconds")
    return manifest



def _registered_texture_mods(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all registry entries belonging to the fixed public texture mod."""
    return [m for m in registry.get("mods", []) if str(m.get("mod_name", "")) == DEFAULT_MOD_NAME]


def _find_held_overlay_path(game_dir: Path, mod: dict[str, Any], overlay_dir: str) -> Path | None:
    for item in mod.get("held_overlays", []) or []:
        original = str(item.get("original_dir", "") or item.get("overlay_dir", ""))
        if original and original != overlay_dir:
            continue
        raw = item.get("held_path") or item.get("path")
        if raw:
            p = Path(str(raw))
            if p.exists():
                return p
    root = registry_hold_root(game_dir)
    candidates = [root / str(mod.get("mod_id", "")) / overlay_dir, root / overlay_dir]
    try:
        candidates.extend(root.glob(f"*/{overlay_dir}"))
    except Exception:
        pass
    for p in candidates:
        if p.exists():
            return p
    return None


def _move_or_delete_overlay_dir(src: Path, dst_root: Path, delete_overlays: bool, log: Callable[[str], None]) -> bool:
    if not src.exists():
        log(f"WARN: overlay not found, skipped: {src}")
        return False
    if delete_overlays:
        shutil.rmtree(src)
        log(f"Deleted overlay: {src}")
        return True
    dst_root.mkdir(parents=True, exist_ok=True)
    dst = dst_root / src.name
    if dst.exists():
        suffix = datetime.now().strftime("_%H%M%S")
        dst = dst_root / f"{src.name}{suffix}"
    shutil.move(str(src), str(dst))
    log(f"Moved overlay to removed backup: {src} -> {dst}")
    return True


def remove_current_texture_build(game_dir: Path, log: Callable[[str], None], *, delete_overlays: bool = False) -> None:
    """Fully remove the current registered KhainOneHDTexture build while preserving external mods.

    This is different from Smart Hold. Hold is temporary parking. This removes the
    registered build from PATHC/PAPGT, moves/deletes its PAZ/PAMT folders, clears
    registry entries and registry manifest copies, then leaves the tool ready for
    a fresh Build / Apply Overlay.
    """
    auto_repair_registry_from_local_manifests(game_dir, log)
    registry = load_master_registry(game_dir)
    target_mods = _registered_texture_mods(registry)
    if not target_mods:
        raise RuntimeError("No registered KhainOneHDTexture build was found. Nothing to remove.")

    copy_current_meta_backup(game_dir, log)
    active = [m for m in target_mods if m.get("status") == "active"]
    held = [m for m in target_mods if m.get("status") == "held"]

    if active:
        log("Removing current build: cleaning only KhainOneHDTexture entries from current PATHC...")
        restore_pathc_underlay_for_mods(game_dir, active, log)
    else:
        log("Removing current build: no active PATHC entries found. Registered overlays may already be on Hold.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    removed_root = game_dir.parent / "CDTextureOverlayBuilder_REMOVED" / game_dir.name / stamp
    moved_or_deleted = 0
    manifest_copies: list[Path] = []

    for mod in target_mods:
        mod_id = str(mod.get("mod_id", "unknown_mod"))
        mod_removed_root = removed_root / mod_id
        for raw_od in mod.get("overlay_dirs", []) or []:
            od = str(raw_od)
            src = game_dir / od
            if not src.exists() and mod.get("status") == "held":
                held_src = _find_held_overlay_path(game_dir, mod, od)
                if held_src:
                    src = held_src
            if src.exists():
                if _move_or_delete_overlay_dir(src, mod_removed_root, delete_overlays, log):
                    moved_or_deleted += 1
            else:
                log(f"WARN: registered overlay missing, skipped: {od}")
        for raw in [mod.get("manifest_copy")]:
            if raw:
                mp = Path(str(raw))
                if mp.exists():
                    manifest_copies.append(mp)

    # Rebuild PAPGT after moving/removing our overlays. Other external overlay folders
    # that are still in the game folder remain visible to PapgtManager.
    log("Removing current build: rebuilding meta/0.papgt without KhainOneHDTexture overlays...")
    papgt_bytes = PapgtManager(game_dir).rebuild(modified_pamts={})
    safe_write(game_dir / "meta" / "0.papgt", papgt_bytes)

    # Clean registry entries and registry manifest copies for this texture build.
    target_ids = {str(m.get("mod_id")) for m in target_mods}
    registry["mods"] = [m for m in registry.get("mods", []) if str(m.get("mod_id")) not in target_ids]
    save_master_registry(game_dir, registry)
    deleted_manifests = 0
    man_root = registry_manifest_dir(game_dir)
    for mp in manifest_copies:
        try:
            # Only delete registry-owned manifest copies, not the user's original output report folder.
            if man_root in mp.parents or mp.parent == man_root:
                mp.unlink()
                deleted_manifests += 1
                log(f"Deleted registry manifest: {mp.name}")
        except Exception as e:
            log(f"WARN: could not delete registry manifest {mp}: {e}")

    # If the manifests folder is now empty, keep the folder but do not fail.
    log(f"Current build removed. Overlays moved/deleted: {moved_or_deleted}. Registry manifests deleted: {deleted_manifests}.")
    if not delete_overlays:
        log(f"Removed overlay backup folder: {removed_root}")
    log("Ready to apply a new build.")


def reapply_registered_mods(game_dir: Path, log: Callable[[str], None], *, backup_meta: bool = True) -> None:
    auto_repair_registry_from_local_manifests(game_dir, log)
    registry = load_master_registry(game_dir)
    active = [m for m in registry.get("mods", []) if m.get("status") == "active"]
    if not active:
        raise RuntimeError("No hay mods activos registrados para reaplicar.")

    if backup_meta:
        copy_current_meta_backup(game_dir, log)

    all_matches: list[MatchedFile] = []
    all_entries: list[OverlayEntry] = []
    all_headers: dict[str, bytes] = {}
    all_dirs: list[str] = []
    per_mod_loaded: list[tuple[dict[str, Any], Path, dict[str, Any], list[MatchedFile], list[OverlayEntry]]] = []
    for mod in active:
        man_path, manifest = _load_registry_manifest(mod)
        log(f"Reapply registry: {mod.get('mod_name')} ({mod.get('mod_id')}) desde {man_path}")
        mod_matches: list[MatchedFile] = []
        mod_entries: list[OverlayEntry] = []
        for raw in manifest.get("matches", []):
            try:
                mf = _matched_from_manifest(raw)
                mod_matches.append(mf)
                all_matches.append(mf)
            except Exception as e:
                log(f"WARN: match inválido omitido en {man_path}: {e}")
        all_headers.update(decode_pathc_header_cache(manifest.get("pathc_headers")))
        for raw in manifest.get("overlay_entries", []):
            try:
                oe = _overlay_entry_from_manifest(raw)
                mod_entries.append(oe)
                all_entries.append(oe)
            except Exception as e:
                log(f"WARN: overlay entry inválido omitido en {man_path}: {e}")
        per_mod_loaded.append((mod, man_path, manifest, mod_matches, mod_entries))
        for od in mod.get("overlay_dirs") or manifest.get("overlay_dirs", []):
            if str(od) not in all_dirs:
                all_dirs.append(str(od))

    if not all_matches or not all_entries:
        raise RuntimeError("Registry cargó, pero no encontré matches/overlay_entries para reaplicar PATHC.")

    # Capture the current PATHC underlay before this reapply overwrites it.
    # Next Hold will restore these entries instead of rolling the whole meta back.
    for _mod, man_path, manifest, mod_matches, mod_entries in per_mod_loaded:
        try:
            manifest["underlay_pathc_entries"] = snapshot_pathc_underlay(game_dir, mod_matches, mod_entries, log)
            manifest["underlay_captured_at"] = datetime.now().isoformat(timespec="seconds")
            _save_registry_manifest(man_path, manifest)
        except Exception as e:
            log(f"WARN: no pude actualizar underlay snapshot en {man_path}: {e}")

    log("== Reapply: actualizando meta/0.pathc preservando otros mods ==")
    pathc_bytes = update_pathc_for_matches(game_dir, all_matches, all_entries, log, header_override_by_entry=all_headers)
    if pathc_bytes is not None:
        safe_write(game_dir / "meta" / "0.pathc", pathc_bytes)

    log("== Reapply: reconstruyendo meta/0.papgt con overlays registrados + otros mods existentes ==")
    modified_pamts: dict[str, bytes] = {}
    missing = []
    for od in all_dirs:
        p = game_dir / od / "0.pamt"
        if p.exists():
            modified_pamts[od] = p.read_bytes()
        else:
            missing.append(od)
    if missing:
        log(f"WARN: overlays registrados no presentes en la carpeta del juego: {missing}")
    papgt_bytes = PapgtManager(game_dir).rebuild(modified_pamts=modified_pamts)
    safe_write(game_dir / "meta" / "0.papgt", papgt_bytes)
    log(f"Reapply terminado. Mods activos: {len(active)}, overlays presentes: {len(modified_pamts)}.")


def hold_registered_overlays(game_dir: Path, log: Callable[[str], None]) -> None:
    auto_repair_registry_from_local_manifests(game_dir, log)
    registry = load_master_registry(game_dir)
    active = [m for m in registry.get("mods", []) if m.get("status") == "active"]
    if not active:
        raise RuntimeError(
            "No hay overlays activos registrados para poner en Hold.\n"
            f"Registry esperado: {registry_path(game_dir)}\n\n"
            "Si el mod ya fue aplicado y funciona en el juego, usa primero el botón:\n"
            "Repair registry from manifest\n"
            "y selecciona el manifest.json del último build. Después vuelve a usar Hold."
        )

    # Dynamic smart Hold: make a safety backup, remove only this tool's PATHC
    # entries by restoring the stored underlay snapshot, then move the PAZ/PAMT
    # folders out of the game so other managers do not see them. We do NOT
    # restore an old full meta backup, because that could delete mods installed
    # after the texture pack was first applied.
    copy_current_meta_backup(game_dir, log)
    log("== Hold inteligente: limpiando PATHC solo de Texture Overlay Builder ==")
    restore_pathc_underlay_for_mods(game_dir, active, log)

    root = registry_hold_root(game_dir)
    root.mkdir(parents=True, exist_ok=True)
    moved = 0
    for mod in active:
        mod_id = str(mod.get("mod_id"))
        mod_hold_dir = root / mod_id
        mod_hold_dir.mkdir(parents=True, exist_ok=True)
        held: list[dict[str, str]] = []
        for od in [str(x) for x in mod.get("overlay_dirs", [])]:
            src = game_dir / od
            dst = mod_hold_dir / od
            if src.exists() and src.is_dir():
                if dst.exists():
                    suffix = datetime.now().strftime("_%Y%m%d_%H%M%S")
                    dst = mod_hold_dir / f"{od}{suffix}"
                shutil.move(str(src), str(dst))
                moved += 1
                log(f"Hold: {src} -> {dst}")
                held.append({"original_dir": od, "held_path": str(dst)})
            else:
                log(f"WARN: overlay registrado no existe en juego y no se pudo mover: {src}")
                held.append({"original_dir": od, "held_path": str(dst), "missing_at_hold": "1"})
        mod["status"] = "held"
        mod["held_at"] = datetime.now().isoformat(timespec="seconds")
        mod["held_overlays"] = held

    save_master_registry(game_dir, registry)
    log("== Hold inteligente: reconstruyendo PAPGT sin tus overlays, preservando otros overlays existentes ==")
    papgt_bytes = PapgtManager(game_dir).rebuild(modified_pamts={})
    safe_write(game_dir / "meta" / "0.papgt", papgt_bytes)
    log(f"Hold terminado. Carpetas movidas fuera del juego: {moved}. Registro: {registry_path(game_dir)}")
    log("Otros managers deberían ver el meta actual sin tu texture overlay, pero conservando sus propios mods.")
    log("Después de instalar otros mods, usa 'Release Hold + Reapply' antes de jugar.")

def release_hold_and_reapply(game_dir: Path, log: Callable[[str], None]) -> None:
    auto_repair_registry_from_local_manifests(game_dir, log)
    registry = load_master_registry(game_dir)
    held_mods = [m for m in registry.get("mods", []) if m.get("status") == "held"]
    if not held_mods:
        raise RuntimeError("No hay mods en Hold para restaurar.")

    copy_current_meta_backup(game_dir, log)
    reserved: set[str] = set()
    for mod in held_mods:
        rename_map: dict[str, str] = {}
        new_dirs: list[str] = []
        for item in mod.get("held_overlays", []):
            old = str(item.get("original_dir", ""))
            held_path = Path(str(item.get("held_path", "")))
            if not old:
                continue
            if not held_path.exists():
                log(f"WARN: held overlay no existe y se omitirá: {held_path}")
                continue
            target_name = old
            target = game_dir / target_name
            if target.exists() or target_name in reserved:
                target_name = _next_free_overlay_dir(game_dir, reserved)
                target = game_dir / target_name
                rename_map[old] = target_name
                log(f"Release: {old} ya está ocupado. Se restaurará como {target_name}.")
            reserved.add(target_name)
            shutil.move(str(held_path), str(target))
            new_dirs.append(target_name)
            log(f"Release: {held_path} -> {target}")

        mod["status"] = "active"
        mod["released_at"] = datetime.now().isoformat(timespec="seconds")
        mod["overlay_dirs"] = new_dirs
        mod["held_overlays"] = []

        # Keep the registry manifest copy aligned if folder names changed.
        try:
            man_path, manifest = _load_registry_manifest(mod)
            manifest = _rewrite_manifest_overlay_dirs(manifest, rename_map)
            manifest["overlay_dirs"] = new_dirs
            manifest["last_released_at"] = datetime.now().isoformat(timespec="seconds")
            _save_registry_manifest(man_path, manifest)
        except Exception as e:
            log(f"WARN: no pude actualizar manifest de registry para {mod.get('mod_name')}: {e}")

    save_master_registry(game_dir, registry)
    log("Release terminado. Ahora se reinyecta PATHC/PAPGT sobre el meta actual de otros managers...")
    reapply_registered_mods(game_dir, log, backup_meta=False)



UI_TEXT: dict[str, dict[str, str]] = {
    "game_folder": {"es": "Carpeta del juego", "en": "Game folder"},
    "browse": {"es": "Buscar", "en": "Browse"},
    "auto_detect": {"es": "Auto detectar", "en": "Auto detect"},
    "textures_folder": {"es": "Carpeta de texturas .dds", "en": "DDS textures folder"},
    "mod_name": {"es": "Mod", "en": "Mod"},
    "source_filter": {"es": "Source PAMT / tipo", "en": "Source PAMT / type"},
    "apply_game": {"es": "Aplicar al juego", "en": "Apply to game"},
    "unique_match": {"es": "Permitir match por filename único", "en": "Allow unique filename match"},
    "dry_run": {"es": "Dry run / solo reporte", "en": "Dry run / report only"},
    "backup_meta": {"es": "Crear backup de meta antes de aplicar", "en": "Back up meta before applying"},
    "scan_conflicts": {"es": "Escanear conflictos con overlays existentes", "en": "Scan conflicts with existing overlays"},
    "build_apply": {"es": "Build / Apply Overlay", "en": "Build / Apply Overlay"},
    "restore_backup": {"es": "Restaurar último backup meta", "en": "Restore latest meta backup"},
    "uninstall_manifest": {"es": "Desinstalar manifest", "en": "Uninstall manifest"},
    "hold": {"es": "Smart Hold overlays", "en": "Smart Hold overlays"},
    "release": {"es": "Release Hold + Reapply", "en": "Release Hold + Reapply"},
    "reapply": {"es": "Reaplicar registry", "en": "Reapply registry"},
    "repair": {"es": "Reparar registry desde manifest", "en": "Repair registry from manifest"},
    "status": {"es": "Estado registry", "en": "Registry status"},
    "clear_log": {"es": "Limpiar log", "en": "Clear log"},
    "lang_toggle": {"es": "English", "en": "Español"},
}


UI_TEXT.update({
    "project_config": {"es": "Configuración del proyecto", "en": "Project configuration"},
    "process_options": {"es": "Opciones del proceso", "en": "Process options"},
    "control_progress": {"es": "Control y progreso", "en": "Control & progress"},
    "activity_log": {"es": "Registro de actividad", "en": "Activity log"},
    "game_folder_short": {"es": "Carpeta del juego", "en": "Game folder"},
    "textures_folder_short": {"es": "Carpeta de texturas DDS", "en": "DDS textures folder"},
    "browse_short": {"es": "...", "en": "..."},
    "search_label": {"es": "Buscar", "en": "Browse"},
    "mod_label": {"es": "Mod", "en": "Mod"},
    "source_label": {"es": "Fuente PAMT / tipo", "en": "Source PAMT / type"},
    "progress_label": {"es": "Progreso", "en": "Progress"},
    "build_apply_pro": {"es": "Construir / aplicar overlay", "en": "Build / apply overlay"},
    "hold_pro": {"es": "Smart Hold overlays", "en": "Smart Hold overlays"},
    "release_pro": {"es": "Release Hold + Reapply", "en": "Release Hold + Reapply"},
    "status_pro": {"es": "Estado del registry", "en": "Registry status"},
    "repair_pro": {"es": "Reparar registry", "en": "Repair registry"},
    "uninstall_pro": {"es": "Desinstalar manifest", "en": "Uninstall manifest"},
    "restore_pro": {"es": "Restaurar backup meta", "en": "Restore meta backup"},
    "reapply_pro": {"es": "Reaplicar registry", "en": "Reapply registry"},
    "clear_log_pro": {"es": "Limpiar registro", "en": "Clear log"},
    "remove_build_pro": {"es": "Quitar build actual", "en": "Remove current build"},
    "language_es": {"es": "🇪🇸  Español", "en": "🇺🇸  English"},
})



# Runtime log translation helper. The core engine uses many technical log strings;
# this keeps the public UI readable when the user switches to English.
def translate_runtime_log(message: str, lang: str) -> str:
    if lang != "en":
        return message
    replacements = [
        ("AVISO", "NOTICE"),
        ("Índice PAMT cacheado cargado", "Cached PAMT index loaded"),
        ("Índice listo", "Index ready"),
        ("texturas encontradas", "textures found"),
        ("Detectadas", "Detected"),
        ("texturas .dds en el mod", ".dds textures in the mod"),
        ("Filtro de origen activo", "Source filter active"),
        ("Esto ayuda a resolver ambiguas por filename", "This helps resolve filename ambiguities"),
        ("Texturas matched", "Matched textures"),
        ("Ambiguas", "Ambiguous"),
        ("texturas quedaron fuera por ambigüedad real", "textures were left out due to real ambiguity"),
        ("Los duplicados equivalentes de PAMT se deduplican automáticamente", "Equivalent PAMT duplicates are deduplicated automatically"),
        ("Fast Hash Helper detectado", "Fast Hash Helper detected"),
        ("CRC PAZ usará helper externo rápido en vez del fallback Python lento", "PAZ CRC will use the fast external helper instead of the slow Python fallback"),
        ("Overlays nuevos planeados", "New overlays planned"),
        ("No se deberían sobrescribir overlays existentes", "Existing overlays should not be overwritten"),
        ("se empieza después del mayor directorio 0000-9999 encontrado", "the first free number after the highest 0000-9999 folder will be used"),
        ("Backup meta creado", "Meta backup created"),
        ("Construyendo overlay", "Building overlay"),
        ("texturas", "textures"),
        ("fuente", "source"),
        ("No se carga el PAZ completo a RAM", "The full PAZ is not loaded into RAM"),
        ("Escribiendo directo a", "Writing directly to"),
        ("calculando CRC por streaming", "calculating CRC by streaming"),
        ("Calculando CRC/hash PAZ por streaming", "Calculating PAZ CRC/hash by streaming"),
        ("CRC PAZ listo", "PAZ CRC done"),
        ("Recalculando hash PAMT", "Recalculating PAMT hash"),
        ("tiempo build+CRC", "build+CRC time"),
        ("velocidad efectiva", "effective speed"),
        ("ETA overlays restantes", "remaining overlay ETA"),
        ("escrito", "written"),
        ("Actualizando meta", "Updating meta"),
        ("Reconstruyendo meta", "Rebuilding meta"),
        ("Aplicado al juego", "Applied to game"),
        ("overlay dirs + PATHC + PAPGT actualizados", "overlay dirs + PATHC + PAPGT updated"),
        ("Reporte", "Report"),
        ("Tiempo total", "Total time"),
        ("Registry maestro actualizado", "Master registry updated"),
        ("Mod registrado", "Registered mod"),
        ("Hold inteligente", "Smart Hold"),
        ("limpiando PATHC solo de Texture Overlay Builder", "cleaning only Texture Overlay Builder PATHC entries"),
        ("entradas restauradas bajo overlay", "entries restored under overlay"),
        ("removidas por ausencia previa", "removed because they did not exist before"),
        ("removidas por fallback", "removed by fallback"),
        ("reconstruyendo PAPGT sin tus overlays", "rebuilding PAPGT without your overlays"),
        ("preservando otros overlays existentes", "preserving other existing overlays"),
        ("Hold terminado", "Hold complete"),
        ("Carpetas movidas fuera del juego", "Folders moved outside the game"),
        ("Otros managers deberían ver el meta actual sin tu texture overlay, pero conservando sus propios mods", "Other managers should see the current meta without your texture overlay, while keeping their own mods"),
        ("Después de instalar otros mods, usa", "After installing other mods, use"),
        ("Release terminado", "Release complete"),
        ("se reinyectará el registry sobre el meta actual", "the registry will be reinjected over the current meta"),
        ("Reapply terminado", "Reapply complete"),
        ("Restaurado", "Restored"),
        ("Eliminado", "Deleted"),
        ("Movido", "Moved"),
        ("Removiendo build actual", "Removing current build"),
        ("Build actual removida", "Current build removed"),
        ("Listo para aplicar una build nueva", "Ready to apply a new build"),
        ("TODOS", "ALL"),
        ("TODAS", "ALL"),
        ("Sí", "Yes"),
        ("No", "No"),
    ]
    out = message
    for a, b in replacements:
        out = out.replace(a, b)
    return out

UI_MESSAGES = {
    "busy": {"es": "Ya hay un proceso corriendo.", "en": "A process is already running."},
    "select_valid_game": {"es": "Selecciona primero la carpeta válida del juego.", "en": "Select a valid game folder first."},
    "game_folder_missing": {"es": "La carpeta del juego no existe.", "en": "The game folder does not exist."},
    "texture_folder_missing": {"es": "La carpeta de texturas no existe.", "en": "The texture folder does not exist."},
    "no_backups": {"es": "No encontré backups en CDTextureOverlayBuilder/backups.", "en": "No backups found in CDTextureOverlayBuilder/backups."},
    "valid_game_path": {"es": "Ruta del juego ya válida:\n{path}", "en": "Game path is already valid:\n{path}"},
    "game_detected": {"es": "Crimson Desert detectado:\n{path}", "en": "Crimson Desert detected:\n{path}"},
    "detect_failed": {"es": "No pude auto-detectar Crimson Desert. Selecciona la carpeta manualmente.", "en": "Could not auto-detect Crimson Desert. Select the folder manually."},
    "restore_confirm": {"es": "Voy a restaurar meta/0.papgt y meta/0.pathc desde el backup más reciente:\n\n{path}\n\n¿Continuar?", "en": "This will restore meta/0.papgt and meta/0.pathc from the latest backup:\n\n{path}\n\nContinue?"},
    "uninstall_delete_question": {"es": "¿Quieres borrar también las carpetas overlay creadas por ese manifest?\n\nSí = restaura meta y borra 0037/0038/etc del manifest.\nNo = solo restaura meta desde el backup del manifest.", "en": "Do you also want to delete the overlay folders created by that manifest?\n\nYes = restore meta and delete 0037/0038/etc from the manifest.\nNo = only restore meta from the manifest backup."},
    "uninstall_confirm": {"es": "Voy a desinstalar/restaurar usando:\n\n{path}\n\n¿Continuar?", "en": "This will uninstall/restore using:\n\n{path}\n\nContinue?"},
    "hold_confirm": {"es": "Smart Hold moverá fuera de la carpeta del juego todos los overlays registrados por esta herramienta.\n\nTambién quitará SOLO las entradas PATHC de este texture overlay, preservando el meta actual de otros managers.\n\nÚsalo antes de abrir DMM/CDUMM/otro manager para que no vea ni borre tus 0037/0038/etc.\n\nDespués de instalar otros mods, usa 'Release Hold + Reapply'.\n\n¿Continuar?", "en": "Smart Hold will move all overlays registered by this tool out of the game folder.\n\nIt will also remove ONLY this texture overlay's PATHC entries, preserving the current meta from other managers.\n\nUse this before opening DMM/CDUMM/another manager so it does not see or remove your 0037/0038/etc folders.\n\nAfter installing other mods, use 'Release Hold + Reapply'.\n\nContinue?"},
    "release_confirm": {"es": "Release moverá de regreso los overlays que estaban en Hold.\n\nSi otro manager ocupó los números anteriores, se usarán números nuevos libres.\nDespués rehará meta/0.pathc y meta/0.papgt sobre el estado actual, preservando otros mods.\n\n¿Continuar?", "en": "Release will move back the overlays that were on Hold.\n\nIf another manager used the previous numbers, new free numbers will be used.\nThen meta/0.pathc and meta/0.papgt will be rebuilt over the current state, preserving other mods.\n\nContinue?"},
    "reapply_confirm": {"es": "Reapply no reconstruye PAZ. Solo vuelve a inyectar en meta/0.pathc y meta/0.papgt los overlays activos del registry.\n\nÚsalo si otro manager modificó meta después de aplicar tu texture pack.\n\n¿Continuar?", "en": "Reapply does not rebuild PAZ files. It only reinjects the active registry overlays into meta/0.pathc and meta/0.papgt.\n\nUse this if another manager modified meta after applying your texture pack.\n\nContinue?"},
    "repair_confirm": {"es": "Voy a registrar este manifest en el registry maestro para poder usar Hold/Reapply:\n\n{path}\n\nNo reconstruye PAZ y no modifica texturas. Solo guarda el registro maestro.\n\n¿Continuar?", "en": "This will register this manifest in the master registry so Hold/Reapply can be used:\n\n{path}\n\nIt does not rebuild PAZ and does not modify textures. It only saves the master registry.\n\nContinue?"},
    "remove_confirm": {"es": "Esto quitará totalmente la build registrada de KhainOneHDTexture.\n\nHará lo siguiente:\n- Quitar tus entradas del meta actual.\n- Preservar mods externos.\n- Mover o borrar tus overlays registrados.\n- Limpiar registry/manifests viejos.\n- Dejar listo para aplicar una build nueva.\n\n¿Continuar?", "en": "This will completely remove the registered KhainOneHDTexture build.\n\nIt will:\n- Remove your entries from the current meta.\n- Preserve external mods.\n- Move or delete your registered overlays.\n- Clean old registry/manifests.\n- Leave the tool ready for a new build.\n\nContinue?"},
    "remove_delete_question": {"es": "¿Borrar permanentemente los overlays registrados?\n\nSí = borrar carpetas 0037/0038/etc.\nNo = moverlas a una carpeta REMOVED de respaldo.", "en": "Permanently delete the registered overlays?\n\nYes = delete 0037/0038/etc folders.\nNo = move them to a REMOVED backup folder."},
    "remove_done": {"es": "Build actual removida. Listo para aplicar una build nueva.", "en": "Current build removed. Ready to apply a new build."},
    "restore_done": {"es": "Restore terminado. Recomendado: abre el juego y verifica el estado.", "en": "Restore complete. Recommended: launch the game and verify the state."},
    "uninstall_done": {"es": "Uninstall/restore terminado.", "en": "Uninstall/restore complete."},
    "hold_done": {"es": "Hold terminado. Ya puedes usar otros managers. Antes de jugar usa Release Hold + Reapply.", "en": "Hold complete. You can now use other managers. Before playing, use Release Hold + Reapply."},
    "release_done": {"es": "Release + Reapply terminado. Ya puedes probar el juego.", "en": "Release + Reapply complete. You can now test the game."},
    "reapply_done": {"es": "Reapply registry terminado.", "en": "Reapply registry complete."},
    "repair_done": {"es": "Registry reparado. Ahora puedes usar Hold registered overlays.", "en": "Registry repaired. You can now use Smart Hold overlays."},
    "done": {"es": "Terminado. Matched: {count}\nReporte:\n{report}", "en": "Done. Matched: {count}\nReport:\n{report}"},
}

class OverlayBuilderUi(tk.Tk):
    # Dark blue professional palette inspired by modern game mod managers.
    C_BG = "#061426"
    C_BG_2 = "#0a1d35"
    C_CARD = "#0d2745"
    C_CARD_2 = "#0a213b"
    C_BORDER = "#1d456c"
    C_BORDER_SOFT = "#173553"
    C_TEXT = "#d7e7fb"
    C_MUTED = "#89a4c4"
    C_ACCENT = "#39a3ff"
    C_ACCENT_2 = "#1f6fdf"
    C_ACCENT_DARK = "#0b4fb3"
    C_GREEN = "#13c283"
    C_WARNING = "#f3b23a"
    C_DANGER = "#ff5a66"
    C_INPUT = "#071a30"

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1260x760")
        self.minsize(1120, 680)
        self.configure(bg=self.C_BG)
        self.msg_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self._build_ui()
        self.after(100, self._poll_queue)

    def _init_theme(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Pro.Horizontal.TProgressbar", troughcolor=self.C_INPUT, background=self.C_ACCENT, bordercolor=self.C_BORDER, lightcolor=self.C_ACCENT, darkcolor=self.C_ACCENT_DARK)
        style.configure("Pro.TCombobox", fieldbackground=self.C_INPUT, background=self.C_CARD_2, foreground=self.C_TEXT, arrowcolor=self.C_ACCENT, bordercolor=self.C_BORDER, insertcolor=self.C_TEXT)
        style.map("Pro.TCombobox", fieldbackground=[("readonly", self.C_INPUT)], foreground=[("readonly", self.C_TEXT)])

    def _tr(self, key: str) -> str:
        return UI_TEXT.get(key, {}).get(getattr(self, "lang", "es"), key)

    def _msg(self, key: str, **kwargs) -> str:
        text = UI_MESSAGES.get(key, {}).get(getattr(self, "lang", "es"), key)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    def _register_text(self, widget: object, key: str) -> object:
        self._ui_text_widgets.append((widget, key))
        return widget

    def _section_title(self, parent: tk.Widget, key: str, icon: str = "") -> tk.Label:
        txt = (icon + "  " if icon else "") + self._tr(key)
        w = tk.Label(parent, text=txt.upper(), bg=self.C_CARD, fg=self.C_TEXT, font=("Segoe UI", 11, "bold"), anchor="w")
        self._register_text(w, key)
        return w

    def _label(self, parent: tk.Widget, key: str, **kw) -> tk.Label:
        w = tk.Label(parent, text=self._tr(key), bg=kw.pop("bg", self.C_CARD), fg=kw.pop("fg", self.C_MUTED), font=kw.pop("font", ("Segoe UI", 10)), anchor="w", **kw)
        self._register_text(w, key)
        return w

    def _button(self, parent: tk.Widget, key: str, command, kind: str = "normal", **kw) -> tk.Button:
        palettes = {
            "primary": (self.C_ACCENT_2, "#ffffff", self.C_ACCENT),
            "success": ("#0f8f68", "#ffffff", self.C_GREEN),
            "warning": ("#72520f", "#ffe9a8", self.C_WARNING),
            "danger": ("#69313b", "#ffd7dc", self.C_DANGER),
            "ghost": (self.C_CARD_2, self.C_TEXT, self.C_BORDER),
            "normal": ("#12375f", self.C_TEXT, self.C_BORDER),
        }
        bg, fg, active = palettes.get(kind, palettes["normal"])
        w = tk.Button(
            parent,
            text=self._tr(key),
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.C_BORDER,
            padx=12,
            pady=8,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
            **kw,
        )
        self._register_text(w, key)
        return w

    def _entry(self, parent: tk.Widget, variable: tk.StringVar, readonly: bool = False) -> tk.Entry:
        e = tk.Entry(
            parent,
            textvariable=variable,
            bg=self.C_INPUT,
            fg=self.C_TEXT,
            insertbackground=self.C_ACCENT,
            readonlybackground=self.C_INPUT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.C_BORDER_SOFT,
            highlightcolor=self.C_ACCENT,
            font=("Segoe UI", 10),
        )
        if readonly:
            e.configure(state="readonly")
        return e

    def _check(self, parent: tk.Widget, key: str, variable: tk.BooleanVar) -> tk.Checkbutton:
        w = tk.Checkbutton(
            parent,
            text=self._tr(key),
            variable=variable,
            bg=self.C_CARD,
            fg=self.C_TEXT,
            activebackground=self.C_CARD,
            activeforeground="#ffffff",
            selectcolor=self.C_INPUT,
            relief="flat",
            font=("Segoe UI", 10),
            anchor="w",
            cursor="hand2",
        )
        self._register_text(w, key)
        return w

    def _card(self, parent: tk.Widget) -> tk.Frame:
        outer = tk.Frame(parent, bg=self.C_BORDER, bd=0)
        inner = tk.Frame(outer, bg=self.C_CARD, bd=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        inner._outer = outer  # type: ignore[attr-defined]
        return inner

    def _path_picker(self, parent: tk.Widget, row: int, label_key: str, var: tk.StringVar, command) -> None:
        self._label(parent, label_key).grid(row=row, column=0, columnspan=3, sticky="w", padx=0, pady=(10, 4))
        icon = tk.Label(parent, text="◆", bg=self.C_INPUT, fg=self.C_ACCENT, font=("Segoe UI", 13, "bold"), width=3)
        icon.grid(row=row + 1, column=0, sticky="nsw", pady=(0, 4))
        self._entry(parent, var).grid(row=row + 1, column=1, sticky="ew", padx=(0, 6), pady=(0, 4), ipady=7)
        self._button(parent, "browse_short", command, kind="ghost", width=4).grid(row=row + 1, column=2, sticky="e", pady=(0, 4))

    def _build_ui(self) -> None:
        self._init_theme()
        self.lang = getattr(self, "lang", "es")
        self._ui_text_widgets: list[tuple[object, str]] = []

        # Variables used by the existing engine/actions.
        self.game_var = tk.StringVar()
        self.tex_var = tk.StringVar()
        self.name_var = tk.StringVar(value=DEFAULT_MOD_NAME)
        self.filter_preset_var = tk.StringVar(value="0000 - Object textures")
        self.out_var = tk.StringVar(value="")
        self.split_var = tk.StringVar(value=str(DEFAULT_SPLIT_GB))
        self.target_pamt_var = tk.StringVar(value="")
        self.target_prefix_var = tk.StringVar(value="")
        self.apply_var = tk.BooleanVar(value=True)
        self.unique_var = tk.BooleanVar(value=True)
        self.dry_var = tk.BooleanVar(value=False)
        self.backup_var = tk.BooleanVar(value=True)
        self.scan_mods_var = tk.BooleanVar(value=True)

        # Full window background.
        shell = tk.Frame(self, bg=self.C_BG)
        shell.pack(fill="both", expand=True, padx=14, pady=14)
        shell.rowconfigure(1, weight=1)
        shell.columnconfigure(0, weight=1)

        # Header.
        header = tk.Frame(shell, bg=self.C_BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.columnconfigure(1, weight=1)
        logo = tk.Label(header, text="◢", bg=self.C_BG, fg="#8fc9ff", font=("Segoe UI", 34, "bold"))
        logo.grid(row=0, column=0, rowspan=2, sticky="w", padx=(2, 16))
        title = tk.Label(header, text="CRIMSON DESERT", bg=self.C_BG, fg=self.C_TEXT, font=("Segoe UI", 25, "bold"), anchor="w")
        title.grid(row=0, column=1, sticky="sw")
        subtitle = tk.Label(header, text="TEXTURE OVERLAY BUILDER", bg=self.C_BG, fg=self.C_MUTED, font=("Segoe UI", 11, "bold"), anchor="w")
        subtitle.grid(row=1, column=1, sticky="nw")
        version = tk.Label(header, text=f"v{APP_VERSION}", bg="#0b2545", fg="#8fb9e7", font=("Segoe UI", 9), padx=8, pady=3)
        version.grid(row=2, column=1, sticky="w", pady=(8, 0))
        self.lang_btn = self._button(header, "language_es", self._toggle_language, kind="ghost", width=14)
        self.lang_btn.grid(row=0, column=2, rowspan=2, sticky="e", padx=(10, 8), ipady=2)
        settings = tk.Label(header, text="⚙", bg=self.C_CARD_2, fg=self.C_TEXT, font=("Segoe UI", 18), width=3, height=1)
        settings.grid(row=0, column=3, rowspan=2, sticky="e")

        # Body columns.
        body = tk.Frame(shell, bg=self.C_BG)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=2, uniform="body")
        body.columnconfigure(1, weight=2, uniform="body")
        body.columnconfigure(2, weight=2, uniform="body")
        body.rowconfigure(0, weight=1)

        # Left card: project configuration.
        left = self._card(body)
        left._outer.grid(row=0, column=0, sticky="nsew", padx=(0, 10))  # type: ignore[attr-defined]
        left.columnconfigure(1, weight=1)
        self._section_title(left, "project_config", "▣").grid(row=0, column=0, columnspan=3, sticky="ew", padx=18, pady=(18, 10))
        tk.Frame(left, height=1, bg=self.C_BORDER_SOFT).grid(row=1, column=0, columnspan=3, sticky="ew", padx=18, pady=(0, 12))
        inner_left = tk.Frame(left, bg=self.C_CARD)
        inner_left.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=18, pady=(0, 18))
        inner_left.columnconfigure(1, weight=1)
        self._path_picker(inner_left, 0, "game_folder_short", self.game_var, self._browse_game)
        self._path_picker(inner_left, 2, "textures_folder_short", self.tex_var, self._browse_tex)
        self._label(inner_left, "search_label").grid(row=4, column=0, columnspan=3, sticky="w", pady=(18, 6))
        search_row = tk.Frame(inner_left, bg=self.C_CARD)
        search_row.grid(row=5, column=0, columnspan=3, sticky="ew")
        search_row.columnconfigure(0, weight=1)
        self._button(search_row, "browse", self._browse_tex, kind="primary").grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._button(search_row, "auto_detect", lambda: self._auto_detect_game_dir(False), kind="ghost").grid(row=0, column=1, sticky="ew")
        self._label(inner_left, "mod_label").grid(row=6, column=0, columnspan=3, sticky="w", pady=(20, 6))
        self._entry(inner_left, self.name_var, readonly=True).grid(row=7, column=0, columnspan=3, sticky="ew", ipady=8)
        self._label(inner_left, "source_label").grid(row=8, column=0, columnspan=3, sticky="w", pady=(20, 6))
        self.filter_preset_combo = ttk.Combobox(inner_left, textvariable=self.filter_preset_var, values=list(FILTER_PRESETS.keys()), state="readonly", style="Pro.TCombobox")
        self.filter_preset_combo.grid(row=9, column=0, columnspan=3, sticky="ew", ipady=5)
        self.filter_preset_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter_preset(log=True))

        # Center: options and control.
        center_wrap = tk.Frame(body, bg=self.C_BG)
        center_wrap.grid(row=0, column=1, sticky="nsew", padx=0)
        center_wrap.columnconfigure(0, weight=1)
        center_wrap.rowconfigure(1, weight=1)
        opt = self._card(center_wrap)
        opt._outer.grid(row=0, column=0, sticky="ew", pady=(0, 10))  # type: ignore[attr-defined]
        opt.columnconfigure(0, weight=1)
        self._section_title(opt, "process_options", "☰").grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 10))
        tk.Frame(opt, height=1, bg=self.C_BORDER_SOFT).grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        opts_inner = tk.Frame(opt, bg=self.C_CARD)
        opts_inner.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 18))
        for i, (key, var) in enumerate([
            ("apply_game", self.apply_var),
            ("unique_match", self.unique_var),
            ("dry_run", self.dry_var),
            ("backup_meta", self.backup_var),
            ("scan_conflicts", self.scan_mods_var),
        ]):
            self._check(opts_inner, key, var).grid(row=i, column=0, sticky="w", pady=4)

        ctrl = self._card(center_wrap)
        ctrl._outer.grid(row=1, column=0, sticky="nsew")  # type: ignore[attr-defined]
        ctrl.columnconfigure(0, weight=1)
        ctrl.columnconfigure(1, weight=1)
        self._section_title(ctrl, "control_progress", "▤").grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(18, 10))
        tk.Frame(ctrl, height=1, bg=self.C_BORDER_SOFT).grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 14))
        self.progress = ttk.Progressbar(ctrl, mode="indeterminate", style="Pro.Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 16))
        self.run_btn = self._button(ctrl, "build_apply_pro", self._start, kind="primary", height=2)
        self.run_btn.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 10))
        self._button(ctrl, "hold_pro", self._hold_registered, kind="ghost", height=2).grid(row=4, column=0, sticky="ew", padx=(18, 6), pady=6)
        self._button(ctrl, "release_pro", self._release_hold, kind="ghost", height=2).grid(row=4, column=1, sticky="ew", padx=(6, 18), pady=6)
        self._button(ctrl, "reapply_pro", self._reapply_registered, kind="ghost").grid(row=5, column=0, sticky="ew", padx=(18, 6), pady=6)
        self._button(ctrl, "status_pro", self._registry_status, kind="ghost").grid(row=5, column=1, sticky="ew", padx=(6, 18), pady=6)
        self._button(ctrl, "repair_pro", self._repair_registry_manifest, kind="ghost").grid(row=6, column=0, sticky="ew", padx=(18, 6), pady=6)
        self._button(ctrl, "uninstall_pro", self._uninstall_manifest, kind="danger").grid(row=6, column=1, sticky="ew", padx=(6, 18), pady=6)
        self._button(ctrl, "remove_build_pro", self._remove_current_build, kind="danger").grid(row=7, column=0, columnspan=2, sticky="ew", padx=18, pady=6)
        self._button(ctrl, "restore_pro", self._restore_latest_backup, kind="warning").grid(row=8, column=0, columnspan=2, sticky="ew", padx=18, pady=(6, 18))

        # Right: log.
        right = self._card(body)
        right._outer.grid(row=0, column=2, sticky="nsew", padx=(10, 0))  # type: ignore[attr-defined]
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        self._section_title(right, "activity_log", "▥").grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 10))
        tk.Frame(right, height=1, bg=self.C_BORDER_SOFT).grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
        log_wrap = tk.Frame(right, bg=self.C_INPUT, highlightbackground=self.C_BORDER_SOFT, highlightthickness=1)
        log_wrap.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 14))
        log_wrap.rowconfigure(0, weight=1)
        log_wrap.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_wrap, height=18, wrap="word", bg=self.C_INPUT, fg=self.C_TEXT, insertbackground=self.C_ACCENT, relief="flat", bd=0, font=("Consolas", 9), padx=10, pady=10)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview, bg=self.C_CARD_2, troughcolor=self.C_INPUT, activebackground=self.C_ACCENT)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self._button(right, "clear_log_pro", lambda: self.log_text.delete("1.0", "end"), kind="ghost").grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 18))

        self._apply_filter_preset(log=False)
        self.after(250, lambda: self._auto_detect_game_dir(True))

    def _toggle_language(self) -> None:
        self.lang = "en" if getattr(self, "lang", "es") == "es" else "es"
        for widget, key in getattr(self, "_ui_text_widgets", []):
            try:
                widget.configure(text=UI_TEXT.get(key, {}).get(self.lang, key))
            except Exception:
                pass
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self._log("Language changed to English." if self.lang == "en" else "Idioma cambiado a español.")

    def _auto_detect_game_dir(self, silent: bool = False) -> None:
        # Do not overwrite a manually selected valid path.
        current = Path(self.game_var.get()).expanduser() if self.game_var.get().strip() else None
        if current and _is_valid_crimson_desert_dir(current):
            if not silent:
                self._log(self._msg("valid_game_path", path=current).replace("\n", " "))
                messagebox.showinfo(APP_NAME, self._msg("valid_game_path", path=current))
            return
        found = detect_crimson_desert_game_dir()
        if found:
            self.game_var.set(str(found))
            if not silent:
                self._log(self._msg("game_detected", path=found).replace("\n", " "))
                messagebox.showinfo(APP_NAME, self._msg("game_detected", path=found))
        elif not silent:
            self._log(self._msg("detect_failed"))
            messagebox.showwarning(APP_NAME, self._msg("detect_failed"))

    def _apply_filter_preset(self, log: bool = True) -> None:
        name = self.filter_preset_var.get().strip()
        pamt, prefix = FILTER_PRESETS.get(name, ("", ""))
        self.target_pamt_var.set(pamt)
        self.target_prefix_var.set(prefix)
        if log:
            self._log(f"Preset aplicado: PAMT={pamt or 'TODOS'}, ruta={prefix or 'TODAS'}")

    def _browse_game(self) -> None:
        p = filedialog.askdirectory(title=("Select the main Crimson Desert folder" if self.lang == "en" else "Selecciona carpeta principal de Crimson Desert"))
        if p:
            self.game_var.set(p)

    def _browse_tex(self) -> None:
        p = filedialog.askdirectory(title=("Select the folder containing your .dds textures" if self.lang == "en" else "Selecciona carpeta con tus texturas .dds"))
        if p:
            self.tex_var.set(p)

    def _browse_out(self) -> None:
        p = filedialog.askdirectory(title=("Select output/reports folder" if self.lang == "en" else "Selecciona carpeta de salida/reportes"))
        if p:
            self.out_var.set(p)

    def _get_options(self) -> BuildOptions:
        game_dir = Path(self.game_var.get()).expanduser()
        # Reports/manifests are always stored inside the tool folder in the game
        # directory. This avoids asking end-users for another path.
        output_dir = game_dir / "CDTextureOverlayBuilder" / "reports"
        # Advanced split is fixed internally for distribution builds.
        split_gb = DEFAULT_SPLIT_GB
        self._apply_filter_preset(log=False)
        return BuildOptions(
            game_dir=game_dir,
            texture_dir=Path(self.tex_var.get()).expanduser(),
            output_dir=output_dir,
            mod_name=DEFAULT_MOD_NAME,
            apply_to_game=bool(self.apply_var.get()),
            allow_unique_filename=bool(self.unique_var.get()),
            dry_run=bool(self.dry_var.get()),
            split_gb=split_gb,
            backup_meta=bool(self.backup_var.get()),
            scan_existing_mod_dirs=bool(self.scan_mods_var.get()),
            target_pamt_dir=self.target_pamt_var.get().strip(),
            target_full_prefix=self.target_prefix_var.get().strip(),
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, self._msg("busy"))
            return
        options = self._get_options()
        if not options.game_dir.exists():
            messagebox.showerror(APP_NAME, self._msg("game_folder_missing"))
            return
        if not options.texture_dir.exists():
            messagebox.showerror(APP_NAME, self._msg("texture_folder_missing"))
            return
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("\n===== START =====")
        self.worker = threading.Thread(target=self._worker, args=(options,), daemon=True)
        self.worker.start()

    def _worker(self, options: BuildOptions) -> None:
        def log(msg: str) -> None:
            self.msg_queue.put(("log", msg))
        try:
            result = build_or_apply(options, log)
            self.msg_queue.put(("done", result))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"{e}\n\n{tb}"))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "done":
                    self.progress.stop()
                    self.run_btn.configure(state="normal")
                    r: BuildResult = payload  # type: ignore[assignment]
                    self._log("===== DONE =====")
                    self._log(f"Overlays: {r.overlay_dirs}")
                    self._log(f"Reporte: {r.report_path}")
                    messagebox.showinfo(APP_NAME, self._msg("done", count=r.matched_count, report=r.report_path))
                elif kind == "action_done":
                    self.progress.stop()
                    self.run_btn.configure(state="normal")
                    self._log("===== DONE =====")
                    self._log(str(payload))
                    messagebox.showinfo(APP_NAME, str(payload))
                elif kind == "error":
                    self.progress.stop()
                    self.run_btn.configure(state="normal")
                    self._log("===== ERROR =====")
                    self._log(str(payload))
                    messagebox.showerror(APP_NAME, str(payload).split("\n", 1)[0])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _restore_latest_backup(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, self._msg("busy"))
            return
        game_dir = Path(self.game_var.get()).expanduser()
        if not game_dir.exists():
            messagebox.showerror(APP_NAME, self._msg("select_valid_game"))
            return
        backup_dir = find_latest_meta_backup(game_dir)
        if backup_dir is None:
            messagebox.showerror(APP_NAME, self._msg("no_backups"))
            return
        ok = messagebox.askyesno(APP_NAME, self._msg("restore_confirm", path=backup_dir))
        if not ok:
            return
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("\n===== RESTORE LATEST META BACKUP =====")
        self.worker = threading.Thread(target=self._restore_worker, args=(game_dir, backup_dir), daemon=True)
        self.worker.start()

    def _restore_worker(self, game_dir: Path, backup_dir: Path) -> None:
        def log(msg: str) -> None:
            self.msg_queue.put(("log", msg))
        try:
            restore_meta_from_backup(game_dir, backup_dir, log)
            self.msg_queue.put(("action_done", self._msg("restore_done")))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"{e}\n\n{tb}"))

    def _uninstall_manifest(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, self._msg("busy"))
            return
        manifest = filedialog.askopenfilename(
            title=("Select the mod manifest.json to uninstall" if self.lang == "en" else "Selecciona manifest.json del mod a desinstalar"),
            filetypes=[("Manifest JSON", "manifest.json"), ("JSON", "*.json"), (("All" if self.lang == "en" else "Todos"), "*.*")],
        )
        if not manifest:
            return
        delete_overlays = messagebox.askyesno(APP_NAME, self._msg("uninstall_delete_question"))
        ok = messagebox.askyesno(APP_NAME, self._msg("uninstall_confirm", path=manifest))
        if not ok:
            return
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("\n===== UNINSTALL MANIFEST =====")
        self.worker = threading.Thread(target=self._uninstall_worker, args=(Path(manifest), delete_overlays), daemon=True)
        self.worker.start()

    def _uninstall_worker(self, manifest_path: Path, delete_overlays: bool) -> None:
        def log(msg: str) -> None:
            self.msg_queue.put(("log", msg))
        try:
            uninstall_from_manifest(manifest_path, delete_overlays, log)
            self.msg_queue.put(("action_done", self._msg("uninstall_done")))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"{e}\n\n{tb}"))



    def _remove_current_build(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, self._msg("busy"))
            return
        game_dir = Path(self.game_var.get()).expanduser()
        if not game_dir.exists():
            messagebox.showerror(APP_NAME, self._msg("select_valid_game"))
            return
        ok = messagebox.askyesno(APP_NAME, self._msg("remove_confirm"))
        if not ok:
            return
        delete_overlays = messagebox.askyesno(APP_NAME, self._msg("remove_delete_question"))
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("\n===== REMOVE CURRENT BUILD =====")
        self.worker = threading.Thread(target=self._remove_current_build_worker, args=(game_dir, delete_overlays), daemon=True)
        self.worker.start()

    def _remove_current_build_worker(self, game_dir: Path, delete_overlays: bool) -> None:
        def log(msg: str) -> None:
            self.msg_queue.put(("log", msg))
        try:
            remove_current_texture_build(game_dir, log, delete_overlays=delete_overlays)
            self.msg_queue.put(("action_done", self._msg("remove_done")))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"{e}\n\n{tb}"))

    def _hold_registered(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, self._msg("busy"))
            return
        game_dir = Path(self.game_var.get()).expanduser()
        if not game_dir.exists():
            messagebox.showerror(APP_NAME, self._msg("select_valid_game"))
            return
        ok = messagebox.askyesno(APP_NAME, self._msg("hold_confirm"))
        if not ok:
            return
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("\n===== HOLD REGISTERED OVERLAYS =====")
        self.worker = threading.Thread(target=self._hold_worker, args=(game_dir,), daemon=True)
        self.worker.start()

    def _hold_worker(self, game_dir: Path) -> None:
        def log(msg: str) -> None:
            self.msg_queue.put(("log", msg))
        try:
            hold_registered_overlays(game_dir, log)
            self.msg_queue.put(("action_done", self._msg("hold_done")))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"{e}\n\n{tb}"))

    def _release_hold(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, self._msg("busy"))
            return
        game_dir = Path(self.game_var.get()).expanduser()
        if not game_dir.exists():
            messagebox.showerror(APP_NAME, self._msg("select_valid_game"))
            return
        ok = messagebox.askyesno(APP_NAME, self._msg("release_confirm"))
        if not ok:
            return
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("\n===== RELEASE HOLD + REAPPLY =====")
        self.worker = threading.Thread(target=self._release_worker, args=(game_dir,), daemon=True)
        self.worker.start()

    def _release_worker(self, game_dir: Path) -> None:
        def log(msg: str) -> None:
            self.msg_queue.put(("log", msg))
        try:
            release_hold_and_reapply(game_dir, log)
            self.msg_queue.put(("action_done", self._msg("release_done")))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"{e}\n\n{tb}"))

    def _reapply_registered(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, self._msg("busy"))
            return
        game_dir = Path(self.game_var.get()).expanduser()
        if not game_dir.exists():
            messagebox.showerror(APP_NAME, self._msg("select_valid_game"))
            return
        ok = messagebox.askyesno(APP_NAME, self._msg("reapply_confirm"))
        if not ok:
            return
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("\n===== REAPPLY REGISTRY =====")
        self.worker = threading.Thread(target=self._reapply_worker, args=(game_dir,), daemon=True)
        self.worker.start()

    def _reapply_worker(self, game_dir: Path) -> None:
        def log(msg: str) -> None:
            self.msg_queue.put(("log", msg))
        try:
            reapply_registered_mods(game_dir, log, backup_meta=True)
            self.msg_queue.put(("action_done", self._msg("reapply_done")))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"{e}\n\n{tb}"))


    def _repair_registry_manifest(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, self._msg("busy"))
            return
        manifest = filedialog.askopenfilename(
            title=("Select the manifest.json from the applied build" if self.lang == "en" else "Selecciona manifest.json del build aplicado"),
            filetypes=[("Manifest JSON", "manifest.json"), ("JSON", "*.json"), (("All" if self.lang == "en" else "Todos"), "*.*")],
        )
        if not manifest:
            return
        ok = messagebox.askyesno(APP_NAME, self._msg("repair_confirm", path=manifest))
        if not ok:
            return
        self.run_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("\n===== REPAIR REGISTRY FROM MANIFEST =====")
        self.worker = threading.Thread(target=self._repair_registry_worker, args=(Path(manifest),), daemon=True)
        self.worker.start()

    def _repair_registry_worker(self, manifest_path: Path) -> None:
        def log(msg: str) -> None:
            self.msg_queue.put(("log", msg))
        try:
            register_manifest_file_to_registry(manifest_path, log)
            self.msg_queue.put(("action_done", self._msg("repair_done")))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"{e}\n\n{tb}"))

    def _registry_status(self) -> None:
        game_dir = Path(self.game_var.get()).expanduser()
        if not game_dir.exists():
            messagebox.showerror(APP_NAME, self._msg("select_valid_game"))
            return
        st = scan_registry_status(game_dir)
        if self.lang == "en":
            msg = (
                f"Registry: {st['registry_path']}\n"
                f"Registered mods: {st['mods']}\n"
                f"Active: {st['active']}\n"
                f"On Hold: {st['held']}\n"
                f"Active overlays: {', '.join(st['active_overlays']) or '(none)'}\n"
                f"Held mods: {', '.join(st['held_mods']) or '(none)'}"
            )
        else:
            msg = (
                f"Registry: {st['registry_path']}\n"
                f"Mods registrados: {st['mods']}\n"
                f"Activos: {st['active']}\n"
                f"En Hold: {st['held']}\n"
                f"Overlays activos: {', '.join(st['active_overlays']) or '(ninguno)'}\n"
                f"Held mods: {', '.join(st['held_mods']) or '(ninguno)'}"
            )
        self._log("\n===== REGISTRY STATUS =====")
        for line in msg.splitlines():
            self._log(line)
        messagebox.showinfo(APP_NAME, msg)

    def _log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"{stamp}  {translate_runtime_log(str(msg), getattr(self, 'lang', 'es'))}\n")
        self.log_text.see("end")
        self.update_idletasks()


def main() -> None:
    app = OverlayBuilderUi()
    app.mainloop()


if __name__ == "__main__":
    main()
