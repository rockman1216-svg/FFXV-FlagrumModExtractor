# FFXV Standalone `.fmod` Extractor (`fmod_extractor`)

Self-contained, **zero-dependency** extractor and converter for Final Fantasy XV Flagrum `.fmod` archives (`v2`, `v3`, and `v4` MemoryPack binary containers, plus legacy zip-based `.fmod` files). Uses **Python 3.10+ standard library only** and runs completely independently of the FFXVLE editor.

## Features

- **All Flagrum `.fmod` generations**: Parses single-mod `v2`, multi-mod `v3`, and MemoryPack `v4` binary containers.
- **Native `FFG ` unwrap**: De-processes `FFG ` (`FmodFragment`) payloads using the verified 128-byte fragment-relative alignment, 64-bit glibc LCG 8-byte key XOR, and 128 KiB chunked `zlib` decompression so extracted files are genuine FFXV assets (never raw `FFG ` wrappers).
- **Compiled extension normalization**:
  - `.ebex` (with `XMB2` or `XMB\0` magic) → `.exml`
  - `.gmdl` → `.gmdl.gfxbin` (without double `.gfxbin.gfxbin`)
  - `.gmtl` → `.gmtl.gfxbin` (without double `.gfxbin.gfxbin`)
  - `.tif` / `.tiff` / `.tga` / `.dds` / `.png` (with `SEDBbtex` magic) → `.btex`
  - Exact original EARC URIs (`data://...`) are preserved in `extraction_manifest.json` (`source_uri_map`).
- **Companion `.png` / `.tga` texture export (`--image-format`, default `png`)**:
  - Automatically writes a decoded `<stem>.png` (default), uncompressed 32-bit BGRA `<stem>.tga`, or both alongside every extracted `.btex` (falling back to lossless `<stem>.dds` for HDR BC6H or exotic formats).
  - **When to use `.tga`**: Uncompressed 32-bit BGRA `.tga` (top-left origin) preserves raw alpha channels without PNG premultiplication/editor re-encoding quirks, making it ideal for channel-packed `_mrs` / hair masks and Flagrum `.tga` round-trip workflows in Blender or Substance Painter.
  - Companion previews are recorded in `extraction_manifest.json` under `derived_previews` (excluded from `source_uri_map` so EARC repacking only packs real FFXV binaries). Pass `--image-format none` (or `--no-png`) to extract `.btex` binaries only.
- **Open Mod Package Format v1 export (`--to-zip`)**: Optionally converts any `.fmod` into a deterministic standard `.zip` package with `manifest.json`.
- **Three ways to use**: Windows drag-and-drop, `tkinter` desktop GUI, or command-line batch CLI.

---

## 1. Windows Drag-and-Drop

Drag one or more `.fmod` files (or a folder of `.fmod` files) directly onto **`extract_fmod.bat`** (or `FFXV_FmodExtractor.exe` once built).

Each `.fmod` is extracted beside the source file into `<mod_slug>/`:

```text
<mod_slug>/
├── earcs/datas/...          # De-processed & normalized EARC member files (.exml, .gmdl.gfxbin, .btex, ...)
├── loose/...                # Loose replacement files (if any)
├── thumbnail.jpg            # Embedded mod preview image (when present)
└── extraction_manifest.json # Metadata, SHA-256 checksums, and exact original EARC URI map
```

Double-clicking `extract_fmod.bat` with no files opens the desktop GUI.

---

## 2. Desktop GUI Mode

Launch with no arguments or pass `--gui`:

```powershell
python standalone\fmod_extractor\fmod_extractor.py
# or
python standalone\fmod_extractor\fmod_extractor.py --gui
```

From the GUI window you can:
1. Select multiple `.fmod` files (**Add .fmod File(s)...**) or scan a directory (**Add Folder...**).
2. Choose an **Output Directory**.
3. Toggle **Also export open .zip package (Mod Package Format v1)**.
4. Click **Inspect** (read-only metadata & member breakdown) or **Extract**.

---

## 3. Command-Line (CLI) Mode

```powershell
# Inspect container version, mod count, author/description, and member list (no files written):
python standalone\fmod_extractor\fmod_extractor.py path\to\mod.fmod --inspect

# Extract one or more .fmod files to a custom directory (writes .btex + companion .png by default):
python standalone\fmod_extractor\fmod_extractor.py mod1.fmod mod2.fmod --out C:\ExtractedMods

# Export uncompressed 32-bit BGRA .tga (or both .png and .tga) alongside .btex:
python standalone\fmod_extractor\fmod_extractor.py mod1.fmod --out C:\ExtractedMods --image-format tga
python standalone\fmod_extractor\fmod_extractor.py mod1.fmod --out C:\ExtractedMods --image-format both

# Extract native binaries only (skip companion preview decoding):
python standalone\fmod_extractor\fmod_extractor.py mod1.fmod --out C:\ExtractedMods --image-format none
# (backward-compatible alias: --no-png)

# Extract native files AND emit an Open Mod Package Format v1 .zip:
python standalone\fmod_extractor\fmod_extractor.py mod1.fmod --out C:\ExtractedMods --to-zip
```

---

## 4. Building `FFXV_FmodExtractor.exe` (Optional)

To package the tool into a single distributable Windows `.exe` (no Python install required on target machines), install PyInstaller in your build environment and run `build_exe.ps1`:

```powershell
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File standalone\fmod_extractor\build_exe.ps1
```

This produces `standalone\fmod_extractor\dist\FFXV_FmodExtractor.exe` supporting both CLI flags and double-click GUI mode (pass `-Windowed` to hide the console window for a GUI-only executable).
