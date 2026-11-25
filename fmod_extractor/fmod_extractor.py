#!/usr/bin/env python3
r"""FFXV Standalone .fmod Extractor & Open Mod Package Converter.

Zero-dependency, single-file tool using Python's standard library only (zero
imports from ``tools/``). Supports all three on-disk Flagrum binary ``FMOD``
container generations (v2 single-mod legacy, v3 multi-mod legacy, v4
MemoryPack) plus historical zip-based ``.fmod`` archives:

  * De-processes ``FFG `` (FmodFragment) wrappers using the verified 128-byte
    fragment-relative data alignment, 64-bit glibc LCG 8-byte header XOR, and
    128 KiB chunked zlib pipeline so extracted members are native FFXV files.
  * Normalizes compiled EARC member extensions onto native toolchain names
    (``.ebex`` with ``XMB2``/``XMB\0`` magic -> ``.exml``; ``.gmdl`` ->
    ``.gmdl.gfxbin``; ``.gmtl`` -> ``.gmtl.gfxbin``) while recording every
    file's exact original EARC URI in ``extraction_manifest.json``.
  * Optionally converts ``.fmod`` archives into deterministic Open Mod Package
    Format v1 ``.zip`` files (``--to-zip``).
  * Runs as a CLI tool, a Windows drag-and-drop target, or a ``tkinter``
    desktop GUI (``--gui`` or launched with no arguments).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import sys
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path

TOOL_VERSION = "1.0.0"
GENERATED_BY = "standalone/fmod_extractor/fmod_extractor.py"

# --- Open Mod Package Format v1 constants ------------------------------------
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
EARC_SUFFIX = ".earc"
ENTRY_TYPE_EARC = "earc"
ENTRY_TYPE_LOOSE = "loose"
ZIP_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
ZIP_CREATE_SYSTEM = 3  # unix (deterministic across platforms)
_CHUNK = 1 << 20
_MOD_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]*\Z")

# --- Flagrum binary .fmod & FFG constants ------------------------------------
FMOD_MAGIC = b"FMOD"
FFG_MAGIC = b"FFG "
FMOD_MAX_VERSION = 4
FMOD_METADATA_CANDIDATES = (
    "flagrum.json",
    "mod.json",
    "project.json",
    "mod_meta.json",
)

_LEGACY_DATA_TYPES = (0, 1, 4)  # AddPacked, ReplacePacked, AddToTextureArray
_FILE_FLAG_COMPRESSED = 0x02
_FILE_FLAG_ENCRYPTED = 0x40
_FILE_FLAG_LZ4_MASK = 0x50000000

# Spec §2.5 EARC / FFG de-processing constants
_MASK64 = (1 << 64) - 1
_KEY_MULTIPLIER = 1103515245
_KEY_ADDITIVE = 12345
_ZLIB_CHUNK_SIZE = 131072  # 128 KiB decompressed bytes per chunk

_XMB_MAGICS = (b"XMB2", b"XMB\x00")
_SEDBBTEX_MAGIC = b"SEDBbtex"
_SEDBBTEX_SOURCE_EXTS = (".tif", ".tiff", ".tga", ".dds", ".png")

_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")
_U64 = struct.Struct("<Q")
_U32X2 = struct.Struct("<II")


class FmodExtractorError(Exception):
    """Raised when a .fmod file cannot be parsed, de-processed, or packaged."""


# Backwards-compatible alias matching tools/mod_package.py
FmodImportError = FmodExtractorError


# ---------------------------------------------------------------------------
# Path & extension normalization helpers
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    return "/".join(
        seg for seg in str(path).replace("\\", "/").split("/")
        if seg not in ("", ".")
    )


def _member_ext(game_path: str) -> str:
    name = game_path.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._\- ]+", "-", str(text).strip().lower())
    cleaned = re.sub(r"\s+", "-", cleaned).strip("-._")
    return cleaned[:64].rstrip("-._") or "extracted-fmod"


def _derive_mod_id(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", ".", str(text).strip().lower())
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("._-")
    if not cleaned:
        return "imported.mod"
    if not re.match(r"[a-z0-9]", cleaned):
        cleaned = "m." + cleaned
    cleaned = cleaned[:48].rstrip("._-")
    return cleaned or "imported.mod"


def normalize_extracted_filename(uri: str, raw_bytes: bytes) -> str:
    """Deterministic compiled-to-native extension normalizer.

    Rules:
      * ``.ebex`` (when *raw_bytes* starts with ``b"XMB2"`` or ``b"XMB\\x00"``)
        -> ``.exml``
      * ``.gmdl`` -> ``.gmdl.gfxbin`` (avoiding double ``.gfxbin.gfxbin``)
      * ``.gmtl`` -> ``.gmtl.gfxbin`` (avoiding double ``.gfxbin.gfxbin``)
      * ``.tif`` / ``.tiff`` / ``.tga`` / ``.dds`` / ``.png`` (when
        *raw_bytes* starts with ``b"SEDBbtex"``) -> ``.btex``
      * All other extensions are left untouched.
    """
    text = str(uri)
    tail = text.replace("\\", "/").rsplit("/", 1)[-1]
    low = tail.lower()
    if low.endswith(".gfxbin"):
        return text
    if low.endswith(".gmdl"):
        return f"{text[:-5]}.gmdl.gfxbin"
    if low.endswith(".gmtl"):
        return f"{text[:-5]}.gmtl.gfxbin"
    if low.endswith(".ebex") and bytes(raw_bytes[:4]) in _XMB_MAGICS:
        return f"{text[:-5]}.exml"
    if bytes(raw_bytes[:8]) == _SEDBBTEX_MAGIC:
        for ext in _SEDBBTEX_SOURCE_EXTS:
            if low.endswith(ext):
                return f"{text[:-len(ext)]}.btex"
    return text


def normalize_game_path(path: object) -> str:
    if not isinstance(path, str):
        raise FmodExtractorError(f"game_path must be a string, got {path!r}")
    p = path.replace("\\", "/").strip()
    if "\x00" in p:
        raise FmodExtractorError("game_path must not contain NUL")
    if p.startswith("/"):
        raise FmodExtractorError(f"game_path must be relative, got {path!r}")
    if len(p) >= 2 and p[1] == ":":
        raise FmodExtractorError(
            f"game_path must not contain a drive letter, got {path!r}"
        )
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        raise FmodExtractorError(f"game_path must not contain '..', got {path!r}")
    if not parts:
        raise FmodExtractorError(f"game_path is empty, got {path!r}")
    norm = "/".join(parts)
    if norm.lower() == MANIFEST_NAME:
        raise FmodExtractorError(
            f"game_path {path!r} is reserved (manifest.json)"
        )
    return norm


def entry_type_for(game_path: str) -> str:
    return (
        ENTRY_TYPE_EARC
        if game_path.lower().endswith(EARC_SUFFIX)
        else ENTRY_TYPE_LOOSE
    )


def compression_for(arcname: str) -> int:
    if arcname.lower().endswith(EARC_SUFFIX):
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Spec §2.5 FFG / EARC member de-processing (embedded, stdlib-only)
# ---------------------------------------------------------------------------

def _apply_key_xor(data: bytearray, key: int) -> None:
    """Spec §2.5 step 1: XOR first 8 bytes with 64-bit glibc LCG schedule."""
    partial_key = (key * _KEY_MULTIPLIER + _KEY_ADDITIVE) & _MASK64
    final_key = (partial_key * _KEY_MULTIPLIER + _KEY_ADDITIVE) & _MASK64
    first, second = _U32X2.unpack_from(data, 0)
    first ^= (final_key >> 32) & 0xFFFFFFFF
    second ^= final_key & 0xFFFFFFFF
    _U32X2.pack_into(data, 0, first, second)


def _decompress_chunks(data: bytes, size: int) -> bytes:
    """Spec §2.5 step 2: 128 KiB chunked zlib inflate (4-byte aligned)."""
    chunks = -(-size // _ZLIB_CHUNK_SIZE)
    out = bytearray()
    offset = 0
    for i in range(chunks):
        if offset + 8 > len(data):
            raise FmodExtractorError(
                f"chunk[{i}] header at 0x{offset:X} out of bounds "
                f"(fragment data {len(data)} bytes)"
            )
        csize = _U32.unpack_from(data, offset)[0]
        dsize = _U32.unpack_from(data, offset + 4)[0]
        offset += 8
        if offset + csize > len(data):
            raise FmodExtractorError(
                f"chunk[{i}] payload {csize} bytes at 0x{offset:X} out of "
                f"bounds (fragment data {len(data)} bytes)"
            )
        payload = data[offset:offset + csize]
        obj = zlib.decompressobj()
        result = obj.decompress(payload)
        if not obj.eof:
            raise FmodExtractorError(
                f"chunk[{i}]: zlib stream did not terminate within {csize} bytes"
            )
        if obj.unused_data:
            raise FmodExtractorError(
                f"chunk[{i}]: {len(obj.unused_data)} trailing bytes after zlib end"
            )
        if len(result) != dsize:
            raise FmodExtractorError(
                f"chunk[{i}]: decompressed {len(result)} bytes != declared {dsize}"
            )
        out += result
        offset += csize
        offset = (offset + 3) & ~3
    return bytes(out)


# ---------------------------------------------------------------------------
# Binary .fmod container parser (v1/v2, v3, v4 MemoryPack)
# ---------------------------------------------------------------------------

class _Bin:
    """Bounds-checked little-endian cursor over a bytes buffer."""

    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.o = offset

    def _need(self, n: int) -> None:
        if self.o + n > len(self.data):
            raise FmodExtractorError(
                f"unexpected end of .fmod at 0x{self.o:X} (need {n} more "
                f"bytes, buffer is {len(self.data)})"
            )

    def u8(self) -> int:
        self._need(1)
        v = self.data[self.o]
        self.o += 1
        return v

    def u32(self) -> int:
        self._need(4)
        v = _U32.unpack_from(self.data, self.o)[0]
        self.o += 4
        return v

    def i32(self) -> int:
        self._need(4)
        v = _I32.unpack_from(self.data, self.o)[0]
        self.o += 4
        return v

    def u64(self) -> int:
        self._need(8)
        v = _U64.unpack_from(self.data, self.o)[0]
        self.o += 8
        return v

    def i64(self) -> int:
        self._need(8)
        v = struct.unpack_from("<q", self.data, self.o)[0]
        self.o += 8
        return v

    def raw(self, n: int) -> bytes:
        self._need(n)
        v = self.data[self.o:self.o + n]
        self.o += n
        return v

    def align(self, n: int) -> None:
        self.o = (self.o + n - 1) & ~(n - 1)

    def cstring(self) -> str:
        end = self.data.find(b"\x00", self.o)
        if end < 0:
            raise FmodExtractorError(
                f"unterminated string at 0x{self.o:X} in .fmod"
            )
        v = self.data[self.o:end]
        self.o = end + 1
        return v.decode("utf-8", "replace")

    def mp_string(self) -> str | None:
        """MemoryPack string: [i32 ~utf8ByteCount][i32 charCount][utf8]."""
        h = self.i32()
        if h == -1:
            return None
        if h < 0:
            n = ~h
            self.i32()
            return self.raw(n).decode("utf-8", "replace")
        return self.raw(h).decode("utf-8", "replace")


def _data_slice(data: bytes, offset: int, size: int, what: str) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise FmodExtractorError(
            f"{what} at 0x{offset:X} (+{size} bytes) is out of bounds "
            f"(.fmod is {len(data)} bytes)"
        )
    return data[offset:offset + size]


def _uri_to_game_path(uri: str) -> str:
    rel = uri.split("://", 1)[1] if "://" in uri else uri
    rel = _norm(rel).lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel if rel.lower().startswith("datas/") else f"datas/{rel}"


def _archive_to_game_path(rel: str) -> str:
    rel_n = _norm(rel).lstrip("/")
    return rel_n if rel_n.lower().startswith("datas/") else f"datas/{rel_n}"


def _ffg_info(data: bytes, offset: int, size: int) -> dict | None:
    """Parse FmodFragment ('FFG ') header at *offset*; None if absent."""
    if size < 24 or data[offset:offset + 4] != FFG_MAGIC:
        return None
    r = _Bin(data, offset)
    r.raw(4)  # "FFG "
    r.u32()   # fragment version (1)
    original_size = r.u32()
    processed_size = r.u32()
    flags = r.u32()
    key = struct.unpack_from("<H", data, r.o)[0]
    r.align(16)
    relative_path = r.cstring()
    # Payload data is 128-aligned RELATIVE TO FRAGMENT START (fragments sit at
    # 16-aligned offsets inside the .fmod).
    data_offset = offset + ((r.o - offset + 127) & ~127)
    return {
        "version": 1,
        "original_size": original_size,
        "processed_size": processed_size,
        "flags": flags,
        "key": key,
        "relative_path": relative_path,
        "data_offset": data_offset,
    }


def _ffg_original_bytes(data: bytes, ffg: dict, what: str) -> bytes:
    """De-process one FmodFragment payload (spec §2.5 pipeline order)."""
    raw = _data_slice(data, ffg["data_offset"], ffg["processed_size"], what)
    if (ffg["flags"] & _FILE_FLAG_LZ4_MASK) == _FILE_FLAG_LZ4_MASK:
        raise FmodExtractorError(
            f"{what}: LZ4-compressed FFG fragment (flags=0x{ffg['flags']:X}) "
            "not supported"
        )
    if (
        ffg["flags"] & _FILE_FLAG_ENCRYPTED
        and not (ffg["flags"] & _FILE_FLAG_COMPRESSED)
    ):
        raise FmodExtractorError(
            f"{what}: encrypted FFG fragment (flags=0x{ffg['flags']:X}) "
            "not supported"
        )
    buf = bytearray(raw)
    if ffg["key"] > 0:
        if len(buf) < 8:
            raise FmodExtractorError(
                f"{what}: key={ffg['key']} but fragment data is "
                f"{len(buf)} bytes (< 8)"
            )
        _apply_key_xor(buf, ffg["key"])
    if ffg["flags"] & _FILE_FLAG_COMPRESSED:
        try:
            out = _decompress_chunks(bytes(buf), ffg["original_size"])
        except Exception as exc:  # noqa: BLE001
            raise FmodExtractorError(
                f"{what}: chunked zlib failed: {exc}"
            ) from exc
    else:
        out = bytes(buf)
    if len(out) > ffg["original_size"]:
        out = out[:ffg["original_size"]]
    return out


def _parse_legacy_mod(data: bytes, base: int, version: int,
                      mod_index: int) -> dict:
    r = _Bin(data, base)
    if r.raw(4) != FMOD_MAGIC:
        raise FmodExtractorError(
            f"mod[{mod_index}] at 0x{base:X}: bad magic (expected FMOD)"
        )
    if r.u32() != version:
        raise FmodExtractorError(
            f"mod[{mod_index}] at 0x{base:X}: version mismatch"
        )
    guid = None
    flags = 0
    if version >= 2:
        guid = r.raw(16).hex()
        flags = r.u32()
    category = r.i32()
    earc_count = r.u32()
    loose_count = r.u32()
    relbuf = base + r.u64()
    uribuf = base + r.u64()
    filebuf = base + r.u64()
    earcs_off = base + r.u64()
    files_off = base + r.u64()
    data_off = base + r.u64()
    thumb_off = base + r.u64()
    thumb_size = r.u32()
    strbuf = base + r.u64()
    name_off, author_off, desc_off, readme_off = (r.u32() for _ in range(4))

    def s_at(buf: int, delta: int) -> str:
        return _Bin(data, buf + delta).cstring()

    r.o = earcs_off
    archives = []
    for _ in range(earc_count):
        a_type = r.i32()
        a_flags = r.u32()
        rp_off = r.u32()
        fcount = r.u32()
        files = []
        for _ in range(fcount):
            f_type = r.i32()
            f_flags = r.u32()
            uri_off = r.u32()
            f_doff = data_off + r.u64()
            f_size = r.u32()
            files.append({
                "type": f_type,
                "flags": f_flags,
                "uri": s_at(uribuf, uri_off),
                "offset": f_doff,
                "size": f_size,
            })
        archives.append({
            "type": a_type,
            "flags": a_flags,
            "relative_path": s_at(relbuf, rp_off),
            "files": files,
        })
    r.o = files_off
    loose = []
    for _ in range(loose_count):
        l_type = r.i32()
        fn_off = r.u32()
        rp_off = r.u32()
        l_doff = data_off + r.u64()
        l_size = r.u32()
        loose.append({
            "type": l_type,
            "relative_path": s_at(relbuf, rp_off),
            "file_name": s_at(filebuf, fn_off),
            "offset": l_doff,
            "size": l_size,
        })
    return {
        "mod_index": mod_index,
        "guid": guid,
        "flags": flags,
        "category": category,
        "name": s_at(strbuf, name_off),
        "author": s_at(strbuf, author_off),
        "description": s_at(strbuf, desc_off),
        "readme": s_at(strbuf, readme_off),
        "archives": archives,
        "loose": loose,
        "thumbnail_offset": thumb_off,
        "thumbnail_size": thumb_size,
    }


def _parse_v4_mod(data: bytes, o: int, mod_index: int) -> tuple[dict, int]:
    r = _Bin(data, o)
    meta_size = r.i32()
    if meta_size <= 0 or o + 4 + meta_size > len(data):
        raise FmodExtractorError(
            f"mod[{mod_index}] at 0x{o:X}: invalid metadata size {meta_size}"
        )
    blob = _data_slice(data, o + 4, meta_size, f"mod[{mod_index}] metadata")
    r.o = o + 4 + meta_size
    r.align(16)

    m = _Bin(blob)
    member_count = m.u8()
    if member_count != 8:
        raise FmodExtractorError(
            f"mod[{mod_index}]: unexpected metadata member count "
            f"{member_count} (expected 8)"
        )
    guid = m.raw(16).hex()
    flags = m.i32()
    archives = []
    for _ in range(m.i32()):
        a_mc = m.u8()
        if a_mc != 4:
            raise FmodExtractorError(
                f"mod[{mod_index}]: archive object member count {a_mc} "
                "(expected 4)"
            )
        a_type = m.i32()
        rel = m.mp_string()
        a_flags = m.u32()
        files = []
        for _ in range(m.i32()):
            tag = m.u8()
            imc = m.u8()
            uri = m.mp_string()
            f_flags = m.u32()
            if imc == 4:
                m.mp_string()  # author machine filePath
                m.i64()        # fileLastModified
                has_data = True
            elif imc == 2:
                has_data = False
            else:
                raise FmodExtractorError(
                    f"mod[{mod_index}]: packed instruction with {imc} "
                    f"members (tag {tag}) is not supported"
                )
            files.append({
                "type": tag,
                "flags": f_flags,
                "uri": uri,
                "has_data": has_data,
                "offset": None,
                "size": None,
            })
        archives.append({
            "type": a_type,
            "flags": a_flags,
            "relative_path": rel,
            "files": files,
        })
    loose = []
    for _ in range(m.i32()):
        tag = m.u8()
        l_mc = m.u8()
        if tag == 1:
            raise FmodExtractorError(
                f"mod[{mod_index}]: hook-feature instructions are not "
                "supported by the .fmod extractor"
            )
        if l_mc != 2:
            raise FmodExtractorError(
                f"mod[{mod_index}]: loose instruction with {l_mc} members "
                f"(tag {tag}) is not supported"
            )
        loose.append({
            "type": tag,
            "relative_path": m.mp_string(),
            "file_name": m.mp_string(),
            "has_data": True,
            "offset": None,
            "size": None,
        })
    name = m.mp_string() or ""
    author = m.mp_string() or ""
    description = m.mp_string() or ""
    readme = m.mp_string() or ""
    if m.o != len(blob):
        raise FmodExtractorError(
            f"mod[{mod_index}]: metadata parse consumed {m.o} of "
            f"{len(blob)} bytes (layout drift)"
        )

    thumb_off = r.u64()
    thumb_size = r.u32()
    assets = (
        [f for a in archives for f in a["files"] if f["has_data"]] + loose
    )
    for asset in assets:
        asset["offset"] = r.u64()
        asset["size"] = r.u32()
    last_end = thumb_off + thumb_size
    for asset in assets:
        if asset["size"]:
            last_end = max(last_end, asset["offset"] + asset["size"])
    next_o = (last_end + 15) & ~15
    return {
        "mod_index": mod_index,
        "guid": guid,
        "flags": flags,
        "category": None,
        "name": name,
        "author": author,
        "description": description,
        "readme": readme,
        "archives": archives,
        "loose": loose,
        "thumbnail_offset": thumb_off,
        "thumbnail_size": thumb_size,
    }, next_o


def _binary_payloads(data: bytes,
                     mods: list[dict]) -> tuple[list[dict], list[str]]:
    payloads: list[dict] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for mod in mods:
        for archive in mod["archives"]:
            archive_gp = _archive_to_game_path(archive["relative_path"])
            for f in archive["files"]:
                has_data = f.get("has_data", f["type"] in _LEGACY_DATA_TYPES)
                if not has_data:
                    continue
                gp = _uri_to_game_path(f["uri"])
                if gp in seen:
                    warnings.append(
                        f"skipped duplicate payload {gp!r} (mod "
                        f"{mod['mod_index']}, archive {archive_gp!r})"
                    )
                    continue
                offset, size = f["offset"], f["size"]
                if offset is not None and offset + size > len(data):
                    warnings.append(
                        f"skipped out-of-bounds payload {gp!r} "
                        f"(0x{offset:X}+{size})"
                    )
                    continue
                seen.add(gp)
                payloads.append({
                    "game_path": gp,
                    "entry_type": entry_type_for(gp),
                    "kind": "packed-fragment",
                    "mod_index": mod["mod_index"],
                    "uri": f["uri"],
                    "archive_game_path": archive_gp,
                    "offset": offset,
                    "size": size,
                    "ffg": _ffg_info(data, offset, size) if size else None,
                })
        for lf in mod["loose"]:
            gp = _norm(lf["relative_path"])
            if not gp:
                continue
            if gp in seen:
                warnings.append(
                    f"skipped duplicate loose payload {gp!r} (mod "
                    f"{mod['mod_index']})"
                )
                continue
            seen.add(gp)
            payloads.append({
                "game_path": gp,
                "entry_type": entry_type_for(gp),
                "kind": "loose-file",
                "mod_index": mod["mod_index"],
                "uri": None,
                "archive_game_path": None,
                "offset": lf["offset"],
                "size": lf["size"],
                "ffg": None,
            })
    return payloads, warnings


def parse_fmod_binary(data: bytes) -> dict:
    """Parse a binary Flagrum .fmod container (versions 1-4) into a report dict."""
    report = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "fmod_version": None,
        "mod_count": 0,
        "mods": [],
        "payloads": [],
    }
    try:
        if len(data) < 8:
            raise FmodExtractorError(f".fmod is only {len(data)} bytes")
        if data[:4] != FMOD_MAGIC:
            raise FmodExtractorError("missing FMOD magic")
        version = _U32.unpack_from(data, 4)[0]
        if version > FMOD_MAX_VERSION:
            raise FmodExtractorError(
                f"FMOD version {version} is newer than supported "
                f"({FMOD_MAX_VERSION})"
            )
        report["fmod_version"] = version
        mods: list[dict] = []
        if version < 3:
            mods.append(_parse_legacy_mod(data, 0, version, 0))
        elif version == 3:
            r = _Bin(data, 8)
            count = r.u32()
            if count > 4096:
                raise FmodExtractorError(f"implausible v3 mod count {count}")
            offsets = [r.u64() for _ in range(count)]
            for i, off in enumerate(offsets):
                mods.append(_parse_legacy_mod(data, off, version, i))
        else:  # version 4
            r = _Bin(data, 8)
            count = r.u32()
            if count > 4096:
                raise FmodExtractorError(f"implausible v4 mod count {count}")
            r.align(16)
            for i in range(count):
                mod, r.o = _parse_v4_mod(data, r.o, i)
                r.align(16)
                mods.append(mod)
        report["mods"] = mods
        report["mod_count"] = len(mods)
        payloads, warnings = _binary_payloads(data, mods)
        report["payloads"] = payloads
        report["warnings"].extend(warnings)
        if not payloads:
            report["errors"].append("no game payload entries found in .fmod")
    except FmodExtractorError as exc:
        report["errors"].append(str(exc))
    report["ok"] = not report["errors"]
    return report


# ---------------------------------------------------------------------------
# Historical zip-based .fmod fallback helpers
# ---------------------------------------------------------------------------

def _json_field(obj, name, default=None):
    if not isinstance(obj, dict):
        return default
    if name in obj and obj[name] is not None:
        return obj[name]
    low = name.lower()
    for key, value in obj.items():
        if key.lower() == low and value is not None:
            return value
    return default


def _find_metadata_entry(names: list[str]) -> str | None:
    by_norm = {_norm(n): n for n in names}
    for candidate in FMOD_METADATA_CANDIDATES:
        if candidate in by_norm:
            return by_norm[candidate]
    json_files = [
        n for n in names
        if n.lower().endswith(".json")
        and _norm(n).count("/") == 0
        and n.lower() != MANIFEST_NAME
    ]
    return json_files[0] if len(json_files) == 1 else None


def _pick_entry(names: list[str], by_norm: dict[str, str],
                *candidates) -> str | None:
    for raw in candidates:
        if not isinstance(raw, str) or not raw.strip():
            continue
        c = _norm(raw)
        if c and c in by_norm:
            return by_norm[c]
    for raw in candidates:
        if not isinstance(raw, str) or not raw.strip():
            continue
        c = _norm(raw)
        if not c:
            continue
        matches = [
            n for n in names
            if _norm(n) == c or _norm(n).endswith("/" + c)
        ]
        if matches:
            return sorted(matches, key=lambda n: (len(_norm(n)), n))[0]
    return None


def _zip_entry_sha256(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with zf.open(info) as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discover_zip_payloads(zf: zipfile.ZipFile, metadata, names: list[str]):
    payloads: list[dict] = []
    warnings: list[str] = []
    by_norm = {_norm(n): n for n in names}

    def register(gp_raw: str, *candidates) -> None:
        try:
            gp = normalize_game_path(gp_raw)
        except FmodExtractorError as exc:
            warnings.append(f"skipping path {gp_raw!r}: {exc}")
            return
        if any(p["game_path"] == gp for p in payloads):
            return
        entry_name = _pick_entry(names, by_norm, *candidates)
        if not entry_name:
            warnings.append(
                f"no zip payload entry for game_path {gp!r} "
                f"(looked at {[c for c in candidates if c]!r})"
            )
            return
        info = zf.getinfo(entry_name)
        payloads.append({
            "game_path": gp,
            "entry_name": entry_name,
            "entry_type": entry_type_for(gp),
            "size": info.file_size,
            "modified_sha256": _zip_entry_sha256(zf, info),
        })

    if isinstance(metadata, dict):
        archives = _json_field(metadata, "archives")
        instructions = _json_field(metadata, "instructions")
        if isinstance(archives, list):
            for archive in archives:
                if not isinstance(archive, dict):
                    continue
                rel = (
                    _json_field(archive, "relativePath")
                    or _json_field(archive, "relative_path")
                    or _json_field(archive, "path")
                    or _json_field(archive, "name")
                )
                if not isinstance(rel, str) or not rel.strip():
                    continue
                rel_n = _norm(rel)
                gp = (
                    rel_n
                    if rel_n.lower().startswith("datas/")
                    else f"datas/{rel_n}"
                )
                register(gp, rel_n, gp)
        if isinstance(instructions, list):
            for inst in instructions:
                if not isinstance(inst, dict):
                    continue
                rel = (
                    _json_field(inst, "relativePath")
                    or _json_field(inst, "relative_path")
                )
                file_path = (
                    _json_field(inst, "filePath")
                    or _json_field(inst, "file_path")
                    or _json_field(inst, "path")
                    or _json_field(inst, "source")
                )
                if isinstance(rel, str) and rel.strip():
                    register(_norm(rel), file_path, rel)
                elif isinstance(file_path, str) and file_path.strip():
                    register(_norm(file_path), file_path)

    if not payloads:
        meta_entry = _find_metadata_entry(names)
        for name in sorted(names):
            if name == meta_entry or name.lower() == MANIFEST_NAME:
                continue
            register(_norm(name), name)

    return payloads, warnings


def inspect_fmod(fmod_path, *, _data: bytes | None = None) -> dict:
    """Structurally inspect a Flagrum .fmod without writing files."""
    report = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "layout": None,
        "metadata_entry": None,
        "metadata": None,
        "payloads": [],
        "fmod_version": None,
        "mod_count": 0,
        "mods": [],
    }
    path = Path(fmod_path)
    if _data is None:
        if not path.is_file():
            report["errors"].append(f"not a file: {path}")
            return report
        data = path.read_bytes()
    else:
        data = _data

    if data[:4] == FMOD_MAGIC:
        parsed = parse_fmod_binary(data)
        report.update({
            k: parsed[k]
            for k in (
                "ok", "errors", "warnings", "fmod_version",
                "mod_count", "mods", "payloads",
            )
        })
        report["layout"] = f"fmod-binary-v{parsed['fmod_version']}"
        report["metadata"] = {
            "fmod_version": parsed["fmod_version"],
            "mod_count": parsed["mod_count"],
            "mods": [
                {
                    "name": m["name"],
                    "author": m["author"],
                    "description": m["description"],
                    "guid": m["guid"],
                    "archive_count": len(m["archives"]),
                    "loose_file_count": len(m["loose"]),
                }
                for m in parsed["mods"]
            ],
        }
        return report

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        report["errors"].append(f"not a zip archive: {path} ({exc})")
        return report
    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        meta_entry = _find_metadata_entry(names)
        report["metadata_entry"] = meta_entry
        metadata = None
        if meta_entry:
            try:
                raw = zf.read(meta_entry)
                obj = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                report["errors"].append(
                    f"{meta_entry}: not valid UTF-8 JSON ({exc})"
                )
            else:
                if not isinstance(obj, dict):
                    report["errors"].append(
                        f"{meta_entry}: root must be a JSON object"
                    )
                else:
                    metadata = obj
        else:
            report["warnings"].append(
                "no Flagrum metadata file found; using mirrored-layout fallback"
            )
        report["metadata"] = metadata
        report["layout"] = (
            "flagrum-project" if metadata is not None else "mirrored"
        )
        payloads, warnings = _discover_zip_payloads(zf, metadata, names)
        report["payloads"] = payloads
        report["warnings"].extend(warnings)
    if not report["errors"] and not report["payloads"]:
        report["errors"].append("no game payload entries found in .fmod")
    report["ok"] = not report["errors"]
    return report


# ---------------------------------------------------------------------------
# Open Mod Package Format v1 .zip builder (byte-exact parity with mod_packager)
# ---------------------------------------------------------------------------

def _zinfo(arcname: str, compress_type: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=ZIP_FIXED_DATE_TIME)
    info.compress_type = compress_type
    info.create_system = ZIP_CREATE_SYSTEM
    info.external_attr = 0o100644 << 16
    return info


def _fmod_metadata(report: dict, fmod_path, mod_id: str | None,
                   mod_version: str, target_game_build: str) -> dict:
    mod0 = report["mods"][0]
    name = mod0.get("name") or Path(fmod_path).stem or "Imported Mod"
    author = mod0.get("author") or "unknown"
    description = mod0.get("description") or ""
    identifier = mod0.get("guid") or ""
    if identifier and set(identifier) <= {"0"}:
        identifier = ""
    if len(report["mods"]) > 1:
        description = (
            description
            + " [combined from a multi-mod .fmod: "
            + "; ".join(
                m.get("name") or f"mod{m['mod_index']}"
                for m in report["mods"]
            )
            + "]"
        ).strip()
    return {
        "mod_id": mod_id or _derive_mod_id(str(identifier or name)),
        "name": str(name),
        "author": str(author),
        "version": str(mod_version or "1.0.0").strip() or "1.0.0",
        "description": str(description),
        "target_game_build": target_game_build,
    }


def build_mod_package(output_zip, metadata: dict,
                      staged_files: dict[str, Path]) -> dict:
    """Write a deterministic format_version=1 Open Mod Package .zip."""
    mod_id = str(metadata.get("mod_id") or "").strip()
    if not _MOD_ID_RE.match(mod_id):
        raise FmodExtractorError(
            f"metadata mod_id must match {_MOD_ID_RE.pattern}, got {mod_id!r}"
        )
    items = sorted(
        (normalize_game_path(gp), Path(local))
        for gp, local in staged_files.items()
    )
    if not items:
        raise FmodExtractorError("no staged files to package")

    entries = [
        {
            "game_path": gp,
            "entry_type": entry_type_for(gp),
            "original_sha256": None,
            "modified_sha256": _sha256_file(local),
            "size": local.stat().st_size,
        }
        for gp, local in items
    ]
    manifest = {
        "format_version": FORMAT_VERSION,
        "mod_id": mod_id,
        "name": str(metadata["name"]).strip(),
        "author": str(metadata["author"]).strip(),
        "version": str(metadata["version"]).strip(),
        "description": str(metadata.get("description") or ""),
        "target_game_build": str(metadata["target_game_build"]).strip(),
        "entries": entries,
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    out = Path(output_zip)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", allowZip64=True) as zf:
        zf.writestr(
            _zinfo(MANIFEST_NAME, compression_for(MANIFEST_NAME)),
            manifest_bytes,
        )
        for gp, local in items:
            info = _zinfo(gp, compression_for(gp))
            info.file_size = local.stat().st_size
            with zf.open(info, "w") as dst, open(local, "rb") as src:
                shutil.copyfileobj(src, dst, _CHUNK)
    return {
        "output_zip": str(out),
        "format_version": FORMAT_VERSION,
        "mod_id": mod_id,
        "entry_count": len(entries),
        "zip_size_bytes": out.stat().st_size,
        "package_sha256": _sha256_file(out),
        "warnings": [],
    }


def import_fmod_to_zip(fmod_path, output_zip_path, *,
                       target_game_build: str = "ffxv-windows-retail",
                       mod_id: str | None = None,
                       mod_version: str = "1.0.0") -> dict:
    """Convert a Flagrum .fmod into an Open Mod Package Format v1 .zip."""
    path = Path(fmod_path)
    if not path.is_file():
        raise FmodExtractorError(f"not a file: {path}")
    data = path.read_bytes()
    report = inspect_fmod(path, _data=data)
    if report["errors"]:
        raise FmodExtractorError("; ".join(report["errors"]))

    out_parent = Path(output_zip_path).resolve().parent
    out_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".fmod_zip_",
                                     dir=str(out_parent)) as td:
        stage = Path(td)
        staged: dict[str, Path] = {}
        if report["layout"].startswith("fmod-binary"):
            metadata = _fmod_metadata(
                report, path, mod_id, mod_version, target_game_build
            )
            for payload in report["payloads"]:
                gp = payload["game_path"]
                dst = stage.joinpath(*gp.split("/"))
                dst.parent.mkdir(parents=True, exist_ok=True)
                ffg = payload.get("ffg")
                off, size = payload["offset"], payload["size"]
                if ffg:
                    try:
                        out = _ffg_original_bytes(data, ffg, gp)
                    except FmodExtractorError as exc:
                        report["warnings"].append(
                            f"{gp}: staged raw processed fragment ({exc})"
                        )
                        out = data[off:off + size]
                else:
                    out = data[off:off + size]
                    if len(out) != size:
                        raise FmodExtractorError(
                            f"short read for payload {gp!r} at 0x{off:X}"
                        )
                dst.write_bytes(out)
                staged[gp] = dst
            del data
        else:
            del data
            metadata_obj = report["metadata"] or {}
            name = (
                _json_field(metadata_obj, "name")
                or path.stem
                or "Imported Mod"
            )
            author = _json_field(metadata_obj, "author") or "unknown"
            description = _json_field(metadata_obj, "description") or ""
            identifier = (
                _json_field(metadata_obj, "identifier")
                or _json_field(metadata_obj, "id")
                or ""
            )
            metadata = {
                "mod_id": mod_id or _derive_mod_id(str(identifier or name)),
                "name": str(name),
                "author": str(author),
                "version": str(mod_version or "1.0.0").strip() or "1.0.0",
                "description": str(description),
                "target_game_build": target_game_build,
            }
            with zipfile.ZipFile(path) as zf:
                for payload in report["payloads"]:
                    gp = payload["game_path"]
                    dst = stage.joinpath(*gp.split("/"))
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(zf.getinfo(payload["entry_name"])) as src, \
                            open(dst, "wb") as out_f:
                        shutil.copyfileobj(src, out_f, _CHUNK)
                    staged[gp] = dst
        summary = build_mod_package(output_zip_path, metadata, staged)

    summary["imported_from"] = str(path)
    summary["fmod_layout"] = report["layout"]
    summary["fmod_version"] = report.get("fmod_version")
    summary["fmod_metadata_entry"] = report["metadata_entry"]
    summary["warnings"] = list(summary.get("warnings") or []) + list(
        report["warnings"]
    )
    return summary


# ---------------------------------------------------------------------------
# Embedded stdlib SEDBbtex -> PNG (+ DX10 DDS fallback) preview exporter
# ---------------------------------------------------------------------------

_BTEX_FORMATS = (
    "none,X1R5G5B5,R5G6B5,X8R8G8B8,A8R8G8B8,B8,G8B8,A16B16G16R16F,"
    "A32B32G32R32F,R32F,X8B8G8R8,A8B8G8R8,D16,D16F,D24X8,D24S8,D24S8F,R6G5B5,"
    "G16R16F,G16R16,R16,A1R5G5B5,A4R4G4B4,R5G5B5A1,DXT1,DXT3,DXT5,V8U8,DXTN,"
    "G8R8A8B8,A8L8,B8G8R8A8,D32F,BC4,BC5,BC6,BC7,D32S8F,G8B8G8R8UN,G8R8,"
    "R16G16B16A16_SNORM,R8G8B8A8_SNORM,R8,R10G10B10A2_UNORM,R10G10B10A2_UINT,"
    "R8_UINT,R8_SNORM,R8_SINT,R8G8_UINT,R8G8_SNORM,R8G8_SINT,R8G8B8A8_UINT,"
    "R8G8B8A8_SINT,R16_UINT,R16_SNORM,R16_SINT,R16_FLOAT,R16G16_UINT,"
    "R16G16_SNORM,R16G16_SINT,R16G16B16A16_UNORM,R16G16B16A16_UINT,"
    "R16G16B16A16_SINT,R32_UINT,R32_SINT,R32G32_FLOAT,R32G32_UINT,R32G32_SINT,"
    "R32G32B32A32_UINT,R32G32B32A32_SINT,R8G8B8A8_UNORM_SRGB,BC1_UNORM_SRGB,"
    "BC2_UNORM_SRGB,BC3_UNORM_SRGB,B8G8R8A8_UNORM_SRGB,B8G8R8X8_UNORM_SRGB,"
    "BC7_UNORM_SRGB,R11G11B10_FLOAT,B10G10R10A2_UNORM"
).split(",")

_DDS_FORMAT_MAP = {
    "A8R8G8B8": (28, None), "A8B8G8R8": (28, None),
    "X8R8G8B8": (28, None), "X8B8G8R8": (28, None),
    "B8G8R8A8": (87, None), "G8R8A8B8": (28, None),
    "R8G8B8A8_UNORM_SRGB": (29, None), "B8G8R8A8_UNORM_SRGB": (91, None),
    "R8": (61, None), "B8": (61, None),
    "DXT1": (71, 8), "BC1_UNORM_SRGB": (72, 8),
    "DXT3": (74, 16), "BC2_UNORM_SRGB": (75, 16),
    "DXT5": (77, 16), "BC3_UNORM_SRGB": (78, 16),
    "BC4": (80, 8), "BC5": (83, 16),
    "BC6": (95, 16), "BC7": (98, 16), "BC7_UNORM_SRGB": (99, 16),
}

_BC1_STRUCT = struct.Struct("<HHI")
_BC2_STRUCT = struct.Struct("<QHHI")
_BC3_STRUCT = struct.Struct("<BB6sHHI")


def _btex_mip_size(width: int, height: int, fmt: str) -> int | None:
    if fmt in ("DXT1", "BC1_UNORM_SRGB", "BC4"):
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 8
    if fmt in ("DXT3", "BC2_UNORM_SRGB", "DXT5", "BC3_UNORM_SRGB",
               "BC5", "BC6", "BC7", "BC7_UNORM_SRGB"):
        return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    if fmt in ("A8R8G8B8", "A8B8G8R8", "X8R8G8B8", "X8B8G8R8",
               "B8G8R8A8", "G8R8A8B8", "R8G8B8A8_UNORM_SRGB",
               "B8G8R8A8_UNORM_SRGB", "B8G8R8X8_UNORM_SRGB"):
        return width * height * 4
    if fmt in ("R8", "R8_UINT", "R8_SNORM", "R8_SINT", "B8"):
        return width * height
    return None


def _bc_alpha_palette(a0: int, a1: int) -> list[int]:
    if a0 > a1:
        return [
            a0, a1,
            (6 * a0 + a1) // 7, (5 * a0 + 2 * a1) // 7,
            (4 * a0 + 3 * a1) // 7, (3 * a0 + 4 * a1) // 7,
            (2 * a0 + 5 * a1) // 7, (a0 + 6 * a1) // 7,
        ]
    return [
        a0, a1,
        (4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
        (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5,
        0, 255,
    ]


def _decode_bc1(width: int, height: int, data: bytes) -> bytes:
    bw, bh = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    if len(data) < bw * bh * 8:
        raise FmodExtractorError("BC1 payload too small")
    out = bytearray(width * height * 4)
    row_stride = width * 4
    unpack = _BC1_STRUCT.unpack_from
    off = 0
    for by in range(bh):
        oy = by * 4
        max_dy = min(4, height - oy)
        for bx in range(bw):
            ox = bx * 4
            c0, c1, bits = unpack(data, off)
            off += 8
            r0 = ((c0 >> 11) & 0x1F) * 255 // 31
            g0 = ((c0 >> 5) & 0x3F) * 255 // 63
            b0 = (c0 & 0x1F) * 255 // 31
            r1 = ((c1 >> 11) & 0x1F) * 255 // 31
            g1 = ((c1 >> 5) & 0x3F) * 255 // 63
            b1 = (c1 & 0x1F) * 255 // 31
            if c0 > c1:
                pal = (
                    bytes((r0, g0, b0, 255)),
                    bytes((r1, g1, b1, 255)),
                    bytes(((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255)),
                    bytes(((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255)),
                )
            else:
                pal = (
                    bytes((r0, g0, b0, 255)),
                    bytes((r1, g1, b1, 255)),
                    bytes(((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255)),
                    b"\x00\x00\x00\x00",
                )
            max_dx = min(4, width - ox)
            if max_dx == 4:
                for dy in range(max_dy):
                    sh = dy * 8
                    dst = (oy + dy) * row_stride + ox * 4
                    out[dst:dst + 16] = (
                        pal[(bits >> sh) & 3]
                        + pal[(bits >> (sh + 2)) & 3]
                        + pal[(bits >> (sh + 4)) & 3]
                        + pal[(bits >> (sh + 6)) & 3]
                    )
            else:
                for dy in range(max_dy):
                    sh = dy * 8
                    base = (oy + dy) * row_stride + ox * 4
                    for dx in range(max_dx):
                        out[base + dx * 4:base + dx * 4 + 4] = pal[(bits >> (sh + dx * 2)) & 3]
    return bytes(out)


def _decode_bc2(width: int, height: int, data: bytes) -> bytes:
    bw, bh = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    if len(data) < bw * bh * 16:
        raise FmodExtractorError("BC2 payload too small")
    out = bytearray(width * height * 4)
    row_stride = width * 4
    unpack = _BC2_STRUCT.unpack_from
    off = 0
    for by in range(bh):
        oy = by * 4
        max_dy = min(4, height - oy)
        for bx in range(bw):
            ox = bx * 4
            alpha_bits, c0, c1, bits = unpack(data, off)
            off += 16
            r0 = ((c0 >> 11) & 0x1F) * 255 // 31
            g0 = ((c0 >> 5) & 0x3F) * 255 // 63
            b0 = (c0 & 0x1F) * 255 // 31
            r1 = ((c1 >> 11) & 0x1F) * 255 // 31
            g1 = ((c1 >> 5) & 0x3F) * 255 // 63
            b1 = (c1 & 0x1F) * 255 // 31
            pal = (
                (r0, g0, b0), (r1, g1, b1),
                ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3),
                ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3),
            )
            max_dx = min(4, width - ox)
            for dy in range(max_dy):
                dst_row = (oy + dy) * row_stride + ox * 4
                for dx in range(max_dx):
                    idx = dy * 4 + dx
                    r, g, b = pal[(bits >> (idx * 2)) & 3]
                    p = dst_row + dx * 4
                    out[p] = r; out[p + 1] = g; out[p + 2] = b
                    out[p + 3] = ((alpha_bits >> (idx * 4)) & 0xF) * 17
    return bytes(out)


def _decode_bc3(width: int, height: int, data: bytes) -> bytes:
    bw, bh = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    if len(data) < bw * bh * 16:
        raise FmodExtractorError("BC3 payload too small")
    out = bytearray(width * height * 4)
    row_stride = width * 4
    unpack = _BC3_STRUCT.unpack_from
    from_bytes = int.from_bytes
    off = 0
    for by in range(bh):
        oy = by * 4
        max_dy = min(4, height - oy)
        for bx in range(bw):
            ox = bx * 4
            a0, a1, abytes, c0, c1, bits = unpack(data, off)
            off += 16
            apal = _bc_alpha_palette(a0, a1)
            abits = from_bytes(abytes, "little")
            r0 = ((c0 >> 11) & 0x1F) * 255 // 31
            g0 = ((c0 >> 5) & 0x3F) * 255 // 63
            b0 = (c0 & 0x1F) * 255 // 31
            r1 = ((c1 >> 11) & 0x1F) * 255 // 31
            g1 = ((c1 >> 5) & 0x3F) * 255 // 63
            b1 = (c1 & 0x1F) * 255 // 31
            if c0 > c1:
                pal = (
                    (r0, g0, b0), (r1, g1, b1),
                    ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3),
                    ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3),
                )
            else:
                pal = (
                    (r0, g0, b0), (r1, g1, b1),
                    ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2),
                    (0, 0, 0),
                )
            max_dx = min(4, width - ox)
            for dy in range(max_dy):
                dst_row = (oy + dy) * row_stride + ox * 4
                for dx in range(max_dx):
                    idx = dy * 4 + dx
                    r, g, b = pal[(bits >> (idx * 2)) & 3]
                    p = dst_row + dx * 4
                    out[p] = r; out[p + 1] = g; out[p + 2] = b
                    out[p + 3] = apal[(abits >> (idx * 3)) & 7]
    return bytes(out)


def _decode_bc4(width: int, height: int, data: bytes) -> bytes:
    bw, bh = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    if len(data) < bw * bh * 8:
        raise FmodExtractorError("BC4 payload too small")
    out = bytearray(width * height)
    from_bytes = int.from_bytes
    off = 0
    for by in range(bh):
        oy = by * 4
        max_dy = min(4, height - oy)
        for bx in range(bw):
            ox = bx * 4
            apal = _bc_alpha_palette(data[off], data[off + 1])
            abits = from_bytes(data[off + 2:off + 8], "little")
            off += 8
            max_dx = min(4, width - ox)
            if max_dx == 4:
                for dy in range(max_dy):
                    sh = dy * 12
                    dst = (oy + dy) * width + ox
                    out[dst] = apal[(abits >> sh) & 7]
                    out[dst + 1] = apal[(abits >> (sh + 3)) & 7]
                    out[dst + 2] = apal[(abits >> (sh + 6)) & 7]
                    out[dst + 3] = apal[(abits >> (sh + 9)) & 7]
            else:
                for dy in range(max_dy):
                    sh = dy * 12
                    dst = (oy + dy) * width + ox
                    for dx in range(max_dx):
                        out[dst + dx] = apal[(abits >> (sh + dx * 3)) & 7]
    return bytes(out)


def _decode_bc5(width: int, height: int, data: bytes) -> bytes:
    bw, bh = max(1, (width + 3) // 4), max(1, (height + 3) // 4)
    if len(data) < bw * bh * 16:
        raise FmodExtractorError("BC5 payload too small")
    out = bytearray(width * height * 3)
    row_stride = width * 3
    from_bytes = int.from_bytes
    off = 0
    for by in range(bh):
        oy = by * 4
        max_dy = min(4, height - oy)
        for bx in range(bw):
            ox = bx * 4
            pal_r = _bc_alpha_palette(data[off], data[off + 1])
            bits_r = from_bytes(data[off + 2:off + 8], "little")
            pal_g = _bc_alpha_palette(data[off + 8], data[off + 9])
            bits_g = from_bytes(data[off + 10:off + 16], "little")
            off += 16
            max_dx = min(4, width - ox)
            for dy in range(max_dy):
                sh = dy * 12
                dst_row = (oy + dy) * row_stride + ox * 3
                for dx in range(max_dx):
                    s = sh + dx * 3
                    p = dst_row + dx * 3
                    out[p] = pal_r[(bits_r >> s) & 7]
                    out[p + 1] = pal_g[(bits_g >> s) & 7]
    return bytes(out)


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


IMAGE_PREVIEW_FORMATS = ("png", "tga", "both", "none")


def _encode_png(width: int, height: int, pixels: bytes, color_type: int) -> bytes:
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    stride = width * channels
    mv = memoryview(pixels)
    raw = b"".join(
        b"\x00" + bytes(mv[y * stride:(y + 1) * stride]) for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
        )
        + _png_chunk(b"IDAT", zlib.compress(raw, 1))
        + _png_chunk(b"IEND", b"")
    )


def _to_rgba(pixels: bytes, color_type: int, width: int, height: int) -> bytes:
    if color_type == 6:
        return pixels
    n = width * height
    rgba = bytearray(n * 4)
    if color_type == 2:
        rgba[0::4] = pixels[0::3]
        rgba[1::4] = pixels[1::3]
        rgba[2::4] = pixels[2::3]
        rgba[3::4] = b"\xff" * n
        return bytes(rgba)
    if color_type == 0:
        rgba[0::4] = pixels[:n]
        rgba[1::4] = pixels[:n]
        rgba[2::4] = pixels[:n]
        rgba[3::4] = b"\xff" * n
        return bytes(rgba)
    raise FmodExtractorError(f"unsupported color_type {color_type}")


def encode_tga(width: int, height: int, rgba: bytes | bytearray) -> bytes:
    """Encode uncompressed 32-bit BGRA TGA (top-left origin, descriptor 0x28)."""
    n = width * height * 4
    if len(rgba) < n:
        raise FmodExtractorError(f"RGBA buffer too small for TGA: {len(rgba)} < {n}")
    src = rgba[:n]
    bgra = bytearray(n)
    bgra[0::4] = src[2::4]
    bgra[1::4] = src[1::4]
    bgra[2::4] = src[0::4]
    bgra[3::4] = src[3::4]
    header = struct.pack(
        "<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, width, height, 32, 0x28
    )
    return header + bytes(bgra)


def _export_btex_preview(raw: bytes, out_dir: Path, stem: str, *,
                         image_format: str = "png") -> dict:
    """Decode SEDBbtex -> companion ``.png`` / ``.tga`` (or DX10 ``.dds`` fallback)."""
    req_fmt = str(image_format).lower()
    if req_fmt not in IMAGE_PREVIEW_FORMATS:
        raise ValueError(
            f"image_format must be one of {IMAGE_PREVIEW_FORMATS}, got {image_format!r}"
        )
    if req_fmt == "none":
        return {
            "paths": [],
            "path": None,
            "formats": [],
            "format": None,
            "fallback": False,
            "diagnostic": None,
        }
    if len(raw) < 160 or raw[:8] != _SEDBBTEX_MAGIC or raw[128:132] != b"BTEX":
        raise FmodExtractorError("not a valid SEDBbtex container")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (header_size, image_data_size, _ver, _plat, _flags, _cnt,
     _stride, image_header_offset) = struct.unpack_from("<IIHBBHHI", raw, 132)
    is_retail = (
        0 < image_header_offset < 128
        and 128 + header_size + image_data_size == len(raw)
    )
    base = (
        128 + image_header_offset if is_retail
        else (image_header_offset or 160)
    )
    (f0, f1, pitch, fmt_enum, mipcount, _depth, _dims,
     image_flags) = struct.unpack_from("<HHHHBBBB", raw, base)
    width, height = (f0, f1) if is_retail else (f1, f0)
    fmt = _BTEX_FORMATS[fmt_enum] if fmt_enum < len(_BTEX_FORMATS) else f"<bad:{fmt_enum}>"
    if width <= 0 or height <= 0:
        raise FmodExtractorError(f"invalid BTEX dimensions {width}x{height}")

    if is_retail:
        data_base = 128 + header_size
        mip0_sz = _btex_mip_size(width, height, fmt) or (len(raw) - data_base)
        mip0 = raw[data_base:data_base + mip0_sz]
        all_mip_bytes = raw[data_base:]
    else:
        mip_off, mip_sz = struct.unpack_from("<ii", raw, base + 56)
        mip0 = raw[mip_off:mip_off + mip_sz]
        all_mip_bytes = mip0

    pixels = color_type = None
    try:
        if fmt in ("A8R8G8B8", "X8R8G8B8", "G8R8A8B8",
                   "B8G8R8A8_UNORM_SRGB", "B8G8R8X8_UNORM_SRGB"):
            n = width * height * 4
            if len(mip0) < n:
                raise FmodExtractorError("BGRA payload too small")
            src = mip0[:n]
            px = bytearray(n)
            px[0::4] = src[2::4]; px[1::4] = src[1::4]
            px[2::4] = src[0::4]; px[3::4] = src[3::4]
            pixels, color_type = bytes(px), 6
        elif fmt in ("A8B8G8R8", "X8B8G8R8", "B8G8R8A8", "R8G8B8A8_UNORM_SRGB"):
            n = width * height * 4
            if len(mip0) < n:
                raise FmodExtractorError("RGBA payload too small")
            pixels, color_type = mip0[:n], 6
        elif fmt in ("R8", "R8_UINT", "R8_SNORM", "R8_SINT", "B8"):
            n = width * height
            if len(mip0) < n:
                raise FmodExtractorError("R8 payload too small")
            pixels, color_type = mip0[:n], 0
        elif fmt in ("DXT1", "BC1_UNORM_SRGB"):
            pixels, color_type = _decode_bc1(width, height, mip0), 6
        elif fmt in ("DXT3", "BC2_UNORM_SRGB"):
            pixels, color_type = _decode_bc2(width, height, mip0), 6
        elif fmt in ("DXT5", "BC3_UNORM_SRGB"):
            pixels, color_type = _decode_bc3(width, height, mip0), 6
        elif fmt == "BC4":
            pixels, color_type = _decode_bc4(width, height, mip0), 0
        elif fmt == "BC5":
            pixels, color_type = _decode_bc5(width, height, mip0), 2
        else:
            raise FmodExtractorError(f"PNG/TGA export unsupported for BTEX format {fmt!r}")

        written: list[str] = []
        written_fmts: list[str] = []
        if req_fmt in ("png", "both"):
            png_path = out_dir / f"{stem}.png"
            png_path.write_bytes(_encode_png(width, height, pixels, color_type))
            written.append(str(png_path))
            written_fmts.append("png")
        if req_fmt in ("tga", "both"):
            rgba = _to_rgba(pixels, color_type, width, height)
            tga_path = out_dir / f"{stem}.tga"
            tga_path.write_bytes(encode_tga(width, height, rgba))
            written.append(str(tga_path))
            written_fmts.append("tga")
        return {
            "paths": written,
            "path": written[0],
            "formats": written_fmts,
            "format": written_fmts[0],
            "fallback": False,
            "diagnostic": None,
        }
    except FmodExtractorError as exc:
        if fmt not in _DDS_FORMAT_MAP:
            raise
        dxgi, block = _DDS_FORMAT_MAP[fmt]
        if (image_flags & 0x30) == 0x30:
            dxgi = {28: 29, 71: 72, 74: 75, 77: 78, 87: 91, 98: 99}.get(dxgi, dxgi)
        lin = pitch if block is None else max(1, (width + 3) // 4) * block
        dds_flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | (0x80000 if block else 0x8)
        caps = 0x1000 | (0x400000 if mipcount > 1 else 0)
        hdr = bytearray(128)
        struct.pack_into("<4s", hdr, 0, b"DDS ")
        struct.pack_into("<7I", hdr, 4, 124, dds_flags, height, width, lin, 0, max(1, mipcount))
        struct.pack_into("<II4s", hdr, 76, 32, 0x4, b"DX10")
        struct.pack_into("<IIII", hdr, 104, caps, 0, 0, 0)
        dx10 = struct.pack("<IIIII", dxgi, 3, 0, 1, 0)
        dds_path = out_dir / f"{stem}.dds"
        dds_path.write_bytes(bytes(hdr) + dx10 + all_mip_bytes)
        return {
            "paths": [str(dds_path)],
            "path": str(dds_path),
            "formats": ["dds"],
            "format": "dds",
            "fallback": True,
            "diagnostic": f"{exc}; wrote lossless companion .dds fallback",
        }


# ---------------------------------------------------------------------------
# Standalone extraction into <out>/<mod_slug>/
# ---------------------------------------------------------------------------

def extract_fmod(fmod_path, out_root=None, *,
                 to_zip: bool = False,
                 export_png: bool = True,
                 image_format: str | None = None,
                 target_game_build: str = "ffxv-windows-retail",
                 mod_version: str = "1.0.0",
                 progress_cb=None) -> dict:
    """Extract a .fmod into ``<out_root>/<mod_slug>/`` with normalized files,
    thumbnail, ``extraction_manifest.json``, and optional Open Mod Package
    Format v1 ``.zip``.
    """
    resolved_image_format = (
        ("png" if export_png else "none")
        if image_format is None
        else str(image_format).lower()
    )
    if resolved_image_format not in IMAGE_PREVIEW_FORMATS:
        raise ValueError(
            f"image_format must be one of {IMAGE_PREVIEW_FORMATS}, "
            f"got {image_format!r}"
        )

    path = Path(fmod_path)
    if not path.is_file():
        raise FmodExtractorError(f"not a file: {path}")
    data = path.read_bytes()
    source_sha = _sha256_bytes(data)
    report = inspect_fmod(path, _data=data)
    if report["errors"]:
        raise FmodExtractorError("; ".join(report["errors"]))

    base_out = Path(out_root) if out_root is not None else path.resolve().parent
    mod_slug = _slugify(path.stem)
    dest_dir = base_out / mod_slug
    dest_dir.mkdir(parents=True, exist_ok=True)

    ebex_scripts: list[str] = []
    prefab_files: list[str] = []
    asset_files: list[str] = []
    archive_game_paths: set[str] = set()
    source_uri_map: dict[str, str] = {}
    derived_previews: list[str] = []
    preview_diagnostics: list[str] = []
    file_records: list[dict] = []
    staged_for_zip: dict[str, Path] = {}
    renamed_count = 0

    total_payloads = len(report["payloads"])
    if report["layout"].startswith("fmod-binary"):
        for idx, payload in enumerate(report["payloads"], 1):
            gp = payload["game_path"]
            if payload["kind"] == "packed-fragment":
                sub_root = dest_dir / "earcs"
                if payload.get("archive_game_path"):
                    archive_game_paths.add(payload["archive_game_path"])
            else:
                sub_root = dest_dir / "loose"

            ffg = payload.get("ffg")
            off, size = payload["offset"], payload["size"]
            if ffg:
                try:
                    raw = _ffg_original_bytes(data, ffg, gp)
                except FmodExtractorError as exc:
                    report["warnings"].append(
                        f"{gp}: staged raw processed fragment ({exc})"
                    )
                    raw = data[off:off + size]
            else:
                raw = data[off:off + size]

            native_gp = normalize_extracted_filename(gp, raw)
            was_renamed = native_gp != gp
            if was_renamed:
                renamed_count += 1

            dst = sub_root.joinpath(*native_gp.split("/"))
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(raw)
            if to_zip:
                staged_for_zip[gp] = dst

            rel = dst.relative_to(dest_dir).as_posix()
            orig_uri = payload.get("uri") or gp
            source_uri_map[rel] = orig_uri

            if resolved_image_format != "none" and raw[:8] == _SEDBBTEX_MAGIC:
                try:
                    preview = _export_btex_preview(
                        raw, dst.parent, dst.stem, image_format=resolved_image_format
                    )
                    for p_str in preview.get("paths") or []:
                        prev_rel = Path(p_str).relative_to(dest_dir).as_posix()
                        derived_previews.append(prev_rel)
                    if preview.get("diagnostic"):
                        preview_diagnostics.append(
                            f"{rel}: {preview['diagnostic']}"
                        )
                except Exception as exc:  # noqa: BLE001
                    preview_diagnostics.append(
                        f"{rel}: preview export skipped ({exc})"
                    )

            ext = _member_ext(gp)
            if ext == ".ebex":
                ebex_scripts.append(rel)
            elif ext == ".prefab":
                prefab_files.append(rel)
            else:
                asset_files.append(rel)

            file_records.append({
                "extracted_path": rel,
                "game_path": gp,
                "original_uri": orig_uri,
                "archive_game_path": payload.get("archive_game_path"),
                "kind": payload["kind"],
                "size": len(raw),
                "sha256": _sha256_bytes(raw),
                "normalized": was_renamed,
            })
            if progress_cb and (idx == total_payloads or idx % 25 == 0):
                progress_cb(idx, total_payloads, rel)
    else:
        with zipfile.ZipFile(path) as zf:
            for idx, payload in enumerate(report["payloads"], 1):
                gp = payload["game_path"]
                raw = zf.read(zf.getinfo(payload["entry_name"]))
                sub_root = (
                    dest_dir / "earcs"
                    if payload["entry_type"] == ENTRY_TYPE_EARC
                    else dest_dir / "loose"
                )
                native_gp = normalize_extracted_filename(gp, raw)
                was_renamed = native_gp != gp
                if was_renamed:
                    renamed_count += 1
                dst = sub_root.joinpath(*native_gp.split("/"))
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(raw)
                if to_zip:
                    staged_for_zip[gp] = dst
                rel = dst.relative_to(dest_dir).as_posix()
                source_uri_map[rel] = gp
                asset_files.append(rel)
                file_records.append({
                    "extracted_path": rel,
                    "game_path": gp,
                    "original_uri": gp,
                    "archive_game_path": (
                        gp if payload["entry_type"] == ENTRY_TYPE_EARC else None
                    ),
                    "kind": payload["entry_type"],
                    "size": len(raw),
                    "sha256": _sha256_bytes(raw),
                    "normalized": was_renamed,
                })
                if progress_cb:
                    progress_cb(idx, total_payloads, rel)

    thumbnail_rel = None
    thumbnail_abs = None
    if report["mods"]:
        mod0 = report["mods"][0]
        if mod0.get("thumbnail_size"):
            try:
                thumb = _data_slice(
                    data,
                    mod0["thumbnail_offset"],
                    mod0["thumbnail_size"],
                    "thumbnail",
                )
            except FmodExtractorError:
                thumb = None
            if thumb:
                thumb_ext = ".png" if thumb[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
                thumbnail_rel = f"thumbnail{thumb_ext}"
                thumbnail_abs = dest_dir / thumbnail_rel
                thumbnail_abs.write_bytes(thumb)

    del data

    zip_summary = None
    zip_path_str = None
    if to_zip:
        zip_target = base_out / f"{mod_slug}.zip"
        if report["layout"].startswith("fmod-binary"):
            pkg_meta = _fmod_metadata(
                report, path, None, mod_version, target_game_build
            )
            zip_summary = build_mod_package(
                zip_target, pkg_meta, staged_for_zip
            )
        else:
            zip_summary = import_fmod_to_zip(
                path,
                zip_target,
                target_game_build=target_game_build,
                mod_version=mod_version,
            )
        zip_path_str = str(zip_target)

    mods_meta = [
        {
            "mod_index": m["mod_index"],
            "name": m["name"],
            "author": m["author"],
            "description": m["description"],
            "readme": m["readme"],
            "guid": m["guid"],
            "archive_count": len(m["archives"]),
            "loose_file_count": len(m["loose"]),
        }
        for m in report["mods"]
    ]

    manifest_doc = {
        "version": 1,
        "generated_by": GENERATED_BY,
        "tool_version": TOOL_VERSION,
        "source_fmod": path.name,
        "source_sha256": source_sha,
        "layout": report["layout"],
        "fmod_version": report["fmod_version"],
        "mod_count": report["mod_count"],
        "mods": mods_meta,
        "payload_count": len(file_records),
        "renamed_source_count": renamed_count,
        "earc_archive_count": len(archive_game_paths),
        "earc_archives": sorted(archive_game_paths),
        "ebex_script_count": len(ebex_scripts),
        "ebex_scripts": sorted(ebex_scripts),
        "prefab_count": len(prefab_files),
        "prefab_files": sorted(prefab_files),
        "asset_file_count": len(asset_files),
        "asset_files": sorted(asset_files),
        "source_uri_map": source_uri_map,
        "derived_previews": sorted(derived_previews),
        "derived_preview_count": len(derived_previews),
        "preview_diagnostics": preview_diagnostics,
        "thumbnail": thumbnail_rel,
        "zip_package": zip_path_str,
        "package_sha256": (
            zip_summary["package_sha256"] if zip_summary else None
        ),
        "warnings": list(report["warnings"]),
        "files": file_records,
    }
    manifest_path = dest_dir / "extraction_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return {
        "ok": True,
        "fmod_path": str(path),
        "mod_slug": mod_slug,
        "output_dir": str(dest_dir),
        "manifest_path": str(manifest_path),
        "thumbnail_path": str(thumbnail_abs) if thumbnail_abs else None,
        "zip_path": zip_path_str,
        "package_sha256": (
            zip_summary["package_sha256"] if zip_summary else None
        ),
        "layout": report["layout"],
        "fmod_version": report["fmod_version"],
        "mod_count": report["mod_count"],
        "payload_count": len(file_records),
        "renamed_source_count": renamed_count,
        "source_uri_map": source_uri_map,
        "derived_previews": sorted(derived_previews),
        "derived_preview_count": len(derived_previews),
        "preview_diagnostics": preview_diagnostics,
        "warnings": list(report["warnings"]),
    }


# ---------------------------------------------------------------------------
# Human-readable inspection formatting
# ---------------------------------------------------------------------------

def format_inspection(fmod_path, report: dict | None = None) -> str:
    """Format a structured human-readable summary of a .fmod file."""
    path = Path(fmod_path)
    rep = report if report is not None else inspect_fmod(path)
    lines = [f"=== {path.name} ==="]
    if path.is_file():
        lines.append(f"  Path:       {path}")
        lines.append(f"  Size:       {path.stat().st_size:,} bytes")
    if not rep["ok"]:
        lines.append("  Status:     INVALID")
        for err in rep["errors"]:
            lines.append(f"  Error:      {err}")
        return "\n".join(lines)

    gen = (
        f"v{rep['fmod_version']} ({rep['layout']})"
        if rep.get("fmod_version") is not None
        else str(rep["layout"])
    )
    lines.append(f"  Container:  {gen}")
    lines.append(f"  Mod Count:  {rep['mod_count'] or 1}")
    lines.append(f"  Payloads:   {len(rep['payloads'])}")

    if rep["mods"]:
        for m in rep["mods"]:
            lines.append(
                f"  [Mod {m['mod_index']}] {m['name'] or '<unnamed>'} "
                f"(by {m['author'] or 'unknown'})"
            )
            if m.get("guid"):
                lines.append(f"    GUID:        {m['guid']}")
            if m.get("description"):
                desc = " ".join(m["description"].splitlines()).strip()
                lines.append(f"    Description: {desc}")
            lines.append(
                f"    Targets:     {len(m['archives'])} archive(s), "
                f"{len(m['loose'])} loose file(s)"
            )
    elif isinstance(rep.get("metadata"), dict):
        meta = rep["metadata"]
        lines.append(
            f"  [Project] {_json_field(meta, 'name', path.stem)} "
            f"(by {_json_field(meta, 'author', 'unknown')})"
        )
        desc = _json_field(meta, "description", "")
        if desc:
            lines.append(f"    Description: {desc}")

    if rep["warnings"]:
        lines.append(f"  Warnings ({len(rep['warnings'])}):")
        for w in rep["warnings"][:10]:
            lines.append(f"    - {w}")
        if len(rep["warnings"]) > 10:
            lines.append(f"    ... and {len(rep['warnings']) - 10} more")

    lines.append(f"  Members ({len(rep['payloads'])}):")
    for p in rep["payloads"]:
        kind = p.get("kind") or p.get("entry_type", "member")
        label = p.get("uri") or p["game_path"]
        ffg = p.get("ffg")
        if ffg:
            size_info = (
                f"orig={ffg['original_size']:,} B, "
                f"packed={ffg['processed_size']:,} B"
            )
        else:
            size_info = f"{p.get('size', 0):,} B"
        arch = (
            f"  [{p['archive_game_path']}]"
            if p.get("archive_game_path")
            else ""
        )
        lines.append(f"    - ({kind}) {label} ({size_info}){arch}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Desktop GUI (tkinter, stdlib only)
# ---------------------------------------------------------------------------

def launch_gui(initial_files: list[str] | None = None) -> int:
    """Launch the standalone tkinter desktop extractor window."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"FFXV .fmod Extractor v{TOOL_VERSION}")
    root.geometry("860x620")
    root.minsize(700, 480)

    queued_files: list[Path] = []
    out_dir_var = tk.StringVar(value="")
    to_zip_var = tk.BooleanVar(value=False)
    image_format_var = tk.StringVar(value="png")
    status_var = tk.StringVar(value="Ready. Add one or more .fmod files to inspect or extract.")
    busy = False

    main = ttk.Frame(root, padding=12)
    main.pack(fill=tk.BOTH, expand=True)

    # --- 1. File queue frame ---
    files_frame = ttk.LabelFrame(main, text="1. Flagrum .fmod Files", padding=8)
    files_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 8))

    list_row = ttk.Frame(files_frame)
    list_row.pack(fill=tk.BOTH, expand=True)

    file_listbox = tk.Listbox(
        list_row, height=6, selectmode=tk.EXTENDED, activestyle="dotbox"
    )
    file_scroll = ttk.Scrollbar(
        list_row, orient=tk.VERTICAL, command=file_listbox.yview
    )
    file_listbox.configure(yscrollcommand=file_scroll.set)
    file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    file_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    btn_row = ttk.Frame(files_frame)
    btn_row.pack(fill=tk.X, pady=(6, 0))

    def _add_paths(paths: list[Path]) -> None:
        existing = {p.resolve() for p in queued_files}
        added = 0
        for p in paths:
            rp = p.resolve()
            if rp.is_file() and rp not in existing:
                queued_files.append(rp)
                existing.add(rp)
                file_listbox.insert(tk.END, str(rp))
                added += 1
        if queued_files and not out_dir_var.get().strip():
            out_dir_var.set(str(queued_files[0].parent / "extracted_fmods"))
        if added:
            status_var.set(f"Queued {len(queued_files)} .fmod file(s).")

    def on_add_files() -> None:
        if busy:
            return
        chosen = filedialog.askopenfilenames(
            parent=root,
            title="Select Flagrum .fmod file(s)",
            filetypes=[("Flagrum Mod Archives", "*.fmod"), ("All Files", "*.*")],
        )
        if chosen:
            _add_paths([Path(c) for c in chosen])

    def on_add_folder() -> None:
        if busy:
            return
        folder = filedialog.askdirectory(
            parent=root, title="Select folder containing .fmod files"
        )
        if folder:
            found = sorted(Path(folder).glob("*.fmod"))
            if not found:
                messagebox.showinfo(
                    "No .fmod Files",
                    f"No .fmod files found in:\n{folder}",
                    parent=root,
                )
                return
            _add_paths(found)

    def on_remove_selected() -> None:
        if busy:
            return
        sel = list(file_listbox.curselection())
        for idx in reversed(sel):
            file_listbox.delete(idx)
            del queued_files[idx]
        status_var.set(f"Queued {len(queued_files)} .fmod file(s).")

    def on_clear_files() -> None:
        if busy:
            return
        file_listbox.delete(0, tk.END)
        queued_files.clear()
        status_var.set("Queue cleared.")

    ttk.Button(btn_row, text="Add .fmod File(s)...", command=on_add_files).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(btn_row, text="Add Folder...", command=on_add_folder).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(btn_row, text="Remove Selected", command=on_remove_selected).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(btn_row, text="Clear List", command=on_clear_files).pack(
        side=tk.LEFT
    )

    # --- 2. Output & options frame ---
    opts_frame = ttk.LabelFrame(main, text="2. Output & Package Options", padding=8)
    opts_frame.pack(fill=tk.X, pady=(0, 8))

    out_row = ttk.Frame(opts_frame)
    out_row.pack(fill=tk.X, pady=(0, 6))
    ttk.Label(out_row, text="Output Directory:").pack(side=tk.LEFT, padx=(0, 8))
    out_entry = ttk.Entry(out_row, textvariable=out_dir_var)
    out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

    def on_browse_out() -> None:
        if busy:
            return
        chosen = filedialog.askdirectory(
            parent=root, title="Select Extraction Output Directory"
        )
        if chosen:
            out_dir_var.set(chosen)

    ttk.Button(out_row, text="Browse...", command=on_browse_out).pack(
        side=tk.RIGHT
    )

    fmt_row = ttk.Frame(opts_frame)
    fmt_row.pack(fill=tk.X, pady=(0, 4))
    ttk.Label(fmt_row, text="Preview image format:").pack(side=tk.LEFT, padx=(0, 8))
    ttk.Combobox(
        fmt_row,
        textvariable=image_format_var,
        values=IMAGE_PREVIEW_FORMATS,
        state="readonly",
        width=10,
    ).pack(side=tk.LEFT)
    ttk.Label(
        fmt_row, text=" (png = default; tga = 32-bit BGRA for Blender/Flagrum)"
    ).pack(side=tk.LEFT, padx=(6, 0))

    ttk.Checkbutton(
        opts_frame,
        text="Also export open .zip package (Mod Package Format v1)",
        variable=to_zip_var,
    ).pack(anchor=tk.W)

    # --- 3. Action bar ---
    act_frame = ttk.Frame(main)
    act_frame.pack(fill=tk.X, pady=(0, 8))

    inspect_btn = ttk.Button(act_frame, text="Inspect")
    extract_btn = ttk.Button(act_frame, text="Extract")
    inspect_btn.pack(side=tk.LEFT, padx=(0, 8))
    extract_btn.pack(side=tk.LEFT, padx=(0, 12))

    progress = ttk.Progressbar(act_frame, orient=tk.HORIZONTAL, mode="determinate")
    progress.pack(side=tk.LEFT, fill=tk.X, expand=True)

    # --- 4. Progress log ---
    log_frame = ttk.LabelFrame(main, text="Progress & Inspection Log", padding=6)
    log_frame.pack(fill=tk.BOTH, expand=True)

    log_text = tk.Text(
        log_frame, wrap=tk.NONE, height=14, font=("Consolas", 9), state=tk.DISABLED
    )
    log_yscroll = ttk.Scrollbar(
        log_frame, orient=tk.VERTICAL, command=log_text.yview
    )
    log_xscroll = ttk.Scrollbar(
        log_frame, orient=tk.HORIZONTAL, command=log_text.xview
    )
    log_text.configure(
        yscrollcommand=log_yscroll.set, xscrollcommand=log_xscroll.set
    )
    log_yscroll.pack(side=tk.RIGHT, fill=tk.Y)
    log_xscroll.pack(side=tk.BOTTOM, fill=tk.X)
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    status_bar = ttk.Label(main, textvariable=status_var, anchor=tk.W)
    status_bar.pack(fill=tk.X, pady=(6, 0))

    def append_log(message: str) -> None:
        log_text.configure(state=tk.NORMAL)
        log_text.insert(tk.END, message + "\n")
        log_text.see(tk.END)
        log_text.configure(state=tk.DISABLED)

    def set_busy(state: bool) -> None:
        nonlocal busy
        busy = state
        btn_state = tk.DISABLED if state else tk.NORMAL
        inspect_btn.configure(state=btn_state)
        extract_btn.configure(state=btn_state)

    def on_inspect() -> None:
        if busy:
            return
        if not queued_files:
            messagebox.showwarning(
                "No Files Selected",
                "Add at least one .fmod file first.",
                parent=root,
            )
            return
        targets = list(queued_files)
        set_busy(True)
        progress.configure(maximum=max(len(targets), 1), value=0)
        status_var.set(f"Inspecting {len(targets)} file(s)...")

        def worker() -> None:
            ok_count = 0
            for idx, fpath in enumerate(targets, 1):
                rep = inspect_fmod(fpath)
                if rep["ok"]:
                    ok_count += 1
                summary_text = format_inspection(fpath, rep)
                root.after(0, append_log, summary_text + "\n")
                root.after(0, lambda v=idx: progress.configure(value=v))
            root.after(
                0,
                lambda: status_var.set(
                    f"Inspection complete: {ok_count}/{len(targets)} valid."
                ),
            )
            root.after(0, set_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    def on_extract() -> None:
        if busy:
            return
        if not queued_files:
            messagebox.showwarning(
                "No Files Selected",
                "Add at least one .fmod file first.",
                parent=root,
            )
            return
        targets = list(queued_files)
        raw_out = out_dir_var.get().strip()
        out_root = Path(raw_out) if raw_out else None
        also_zip = bool(to_zip_var.get())
        chosen_img_fmt = image_format_var.get().strip().lower() or "png"

        set_busy(True)
        progress.configure(maximum=max(len(targets), 1), value=0)
        status_var.set(f"Extracting {len(targets)} .fmod file(s)...")

        def worker() -> None:
            ok_count = 0
            fail_count = 0
            for idx, fpath in enumerate(targets, 1):
                root.after(
                    0,
                    append_log,
                    f"[{idx}/{len(targets)}] Extracting {fpath.name}...",
                )
                try:
                    res = extract_fmod(
                        fpath,
                        out_root=out_root,
                        to_zip=also_zip,
                        image_format=chosen_img_fmt,
                    )
                    ok_count += 1
                    msg = (
                        f"  OK -> {res['output_dir']} "
                        f"({res['payload_count']} files, "
                        f"{res['renamed_source_count']} normalized, "
                        f"{res['derived_preview_count']} previews)"
                    )
                    if res["zip_path"]:
                        msg += f"\n  ZIP -> {res['zip_path']}"
                    root.after(0, append_log, msg)
                except Exception as exc:  # noqa: BLE001
                    fail_count += 1
                    root.after(0, append_log, f"  ERROR: {exc}")
                root.after(0, lambda v=idx: progress.configure(value=v))

            done_msg = (
                f"Extraction finished: {ok_count} succeeded, "
                f"{fail_count} failed."
            )
            root.after(0, append_log, done_msg + "\n")
            root.after(0, lambda: status_var.set(done_msg))
            root.after(0, set_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    inspect_btn.configure(command=on_inspect)
    extract_btn.configure(command=on_extract)

    if initial_files:
        _add_paths([Path(f) for f in initial_files])

    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _expand_inputs(raw_paths: list[str]) -> list[Path]:
    expanded: list[Path] = []
    for raw in raw_paths:
        p = Path(raw)
        if p.is_dir():
            expanded.extend(sorted(p.glob("*.fmod")))
        else:
            expanded.append(p)
    return expanded


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fmod_extractor.py",
        description=(
            "Standalone Flagrum .fmod extractor (v2/v3/v4) & Open Mod Package "
            "v1 converter. Launch with no arguments or --gui for the desktop "
            "window."
        ),
    )
    parser.add_argument(
        "fmods",
        nargs="*",
        help="One or more .fmod files (or directories containing .fmod files).",
    )
    parser.add_argument(
        "--out",
        dest="out_dir",
        default=None,
        help="Output root directory (default: alongside each .fmod file).",
    )
    parser.add_argument(
        "--to-zip",
        action="store_true",
        help="Also convert each .fmod into an Open Mod Package Format v1 .zip.",
    )
    parser.add_argument(
        "--image-format",
        choices=IMAGE_PREVIEW_FORMATS,
        default=None,
        help="Companion preview format alongside .btex: png (default), tga, both, or none.",
    )
    parser.add_argument(
        "--no-png",
        action="store_true",
        help="Alias for --image-format none (skip exporting companion textures).",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print container generation, metadata, and member list without extracting.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the tkinter desktop GUI.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        return launch_gui()

    args = _build_arg_parser().parse_args(raw_argv)
    if args.gui:
        return launch_gui(args.fmods)

    targets = _expand_inputs(args.fmods)
    if not targets:
        print("error: no .fmod files specified.", file=sys.stderr)
        return 2

    if args.inspect:
        rc = 0
        for idx, path in enumerate(targets):
            if idx > 0:
                print()
            rep = inspect_fmod(path)
            print(format_inspection(path, rep))
            if not rep["ok"]:
                rc = 1
        return rc

    resolved_fmt = "none" if args.no_png else (args.image_format or "png")
    rc = 0
    for path in targets:
        try:
            res = extract_fmod(
                path,
                out_root=args.out_dir,
                to_zip=args.to_zip,
                image_format=resolved_fmt,
            )
            print(
                f"[OK] {path.name} ({res['layout']}, mods={res['mod_count']}) "
                f"-> {res['output_dir']} "
                f"({res['payload_count']} files, "
                f"{res['renamed_source_count']} normalized, "
                f"{res['derived_preview_count']} previews)"
            )
            if res["zip_path"]:
                print(
                    f"     Open .zip v1 -> {res['zip_path']} "
                    f"(sha256={res['package_sha256'][:16]}...)"
                )
        except FmodExtractorError as exc:
            rc = 1
            print(f"[ERROR] {path}: {exc}", file=sys.stderr)
    return rc


__all__ = [
    "TOOL_VERSION",
    "FORMAT_VERSION",
    "MANIFEST_NAME",
    "FmodExtractorError",
    "FmodImportError",
    "normalize_extracted_filename",
    "normalize_game_path",
    "parse_fmod_binary",
    "inspect_fmod",
    "import_fmod_to_zip",
    "extract_fmod",
    "format_inspection",
    "launch_gui",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
