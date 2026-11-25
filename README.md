
# FFXV FMOD Extractor

A lightweight, standalone desktop and CLI tool for inspecting and unpacking *Final Fantasy XV* `.fmod` packages directly into clean, native, ready-to-edit game assets—with **zero external dependencies**.

Unlike basic archive unpackers, **FFXV FMOD Extractor** parses all Flagrum binary `.fmod` generations, automatically de-processes internal `FFG ` (`FmodFragment`) payloads into real FFXV binaries, normalizes compiled extensions for immediate use in Blender / Flagrum Blender and XML editors, and decodes `.btex` textures into ready-to-view `.png` and/or `.tga` images alongside the original `.btex` files.

---

## Features

- **Full `.fmod` Container Support**
  - Binary **v2** (single legacy mod), **v3** (multi-mod container), and **v4** (MemoryPack multi-mod container), plus legacy zip-based `.fmod` archives.
- **Automatic `FFG ` Fragment De-Processing**
  - Unwraps compressed and XOR-obfuscated `FmodFragment` payloads into native FFXV files—no raw `FFG ` wrappers left on disk.
- **Automatic Extension Normalization (Blender & Editor Ready)**
  - Compiled binary `.ebex` (`XMB2` / `XMB\0`) → `.exml` *(plain-text XML `.ebex` scripts remain `.ebex`)*
  - `.gmdl` → `.gmdl.gfxbin`
  - `.gmtl` → `.gmtl.gfxbin`
  - `SEDBbtex`-headed `.tif` / `.tiff` / `.tga` / `.dds` / `.png` → `.btex`
- **Built-In `.btex` Texture Export (`.png` / `.tga` / `.dds`)**
  - Exports companion images right next to every `.btex` file for direct import into Blender, Flagrum Blender, and Substance Painter.
  - Pure-Python decoders for **RGBA8**, **BGRA8**, **R8**, **BC1 (DXT1)**, **BC2 (DXT3)**, **BC3 (DXT5)**, **BC4**, and **BC5**.
  - Choose between **PNG** *(default)*, **TGA** *(uncompressed 32-bit BGRA, top-left origin—ideal for `_mrs`/hair channel-packed masks and Flagrum `.tga` workflows)*, **Both**, or **None**, with automatic lossless DX10 `.dds` fallback for HDR/unsupported formats.
- **URI & Metadata Preservation**
  - Extracts mod thumbnails (`thumbnail.jpg`) and writes `extraction_manifest.json`, mapping every normalized file back to its exact original `data://` EARC URI while keeping derived preview images cleanly separated from repack sources.
- **Inspect Mode & Open `.zip` Conversion**
  - Inspect container version, mod count, author, description, and member lists without writing files (`--inspect`), or export to an open, non-proprietary `.zip` Mod Package with SHA-256 checksums (`--to-zip`).
- **Zero Dependencies**
  - Written in pure Python standard library (`tkinter`, `zlib`, `struct`, `zipfile`, `json`, `hashlib`) and packaged as a standalone Windows `.exe`.

---

## Quick Start (Pre-Built `.exe`)

No installation, Python, .NET, or Flagrum runtime is required.

1. Download the latest release from the **[Releases](../../releases)** page and extract it anywhere.
2. **GUI Mode:** Double-click `FFXV_FmodExtractor.exe`, pick one or more `.fmod` files (or a folder), select your **Preview image format** (`png`, `tga`, `both`, or `none`), and click **Extract**.
3. **Drag-and-Drop Mode:** Drag one or more `.fmod` files directly onto `FFXV_FmodExtractor.exe` to unpack them immediately into subfolders next to the input file(s).

---

## Running from Source

Requires **Python 3.10+** (standard library only—no `pip install` needed).

- **Windows GUI / Drag-and-Drop:** Double-click `extract_fmod.bat` to open the GUI, or drag `.fmod` files onto `extract_fmod.bat`.
- **Direct Python Launch:**
  ```powershell
  python fmod_extractor.py
  ```

---

## CLI Usage

Both `FFXV_FmodExtractor.exe` and `python fmod_extractor.py` support the same command-line flags:

```powershell
# Extract one or more .fmod files (default: .btex + companion .png)
FFXV_FmodExtractor.exe "path\to\mod.fmod"

# Extract to a specific output folder with both .png and .tga previews
FFXV_FmodExtractor.exe "mod1.fmod" "mod2.fmod" --out "D:\ExtractedMods" --image-format both

# Extract with .tga previews only (great for Flagrum .tga / channel-packed workflows)
FFXV_FmodExtractor.exe "mod.fmod" --image-format tga

# Extract native FFXV binaries only (skip preview image decoding)
FFXV_FmodExtractor.exe "mod.fmod" --image-format none
# (or use the shorthand alias: --no-png)

# Inspect metadata and file list without writing anything to disk
FFXV_FmodExtractor.exe "mod.fmod" --inspect

# Also export an open .zip Mod Package v1 with SHA-256 manifest
FFXV_FmodExtractor.exe "mod.fmod" --to-zip

# Force-open the desktop GUI
FFXV_FmodExtractor.exe --gui
```

### CLI Options Summary

| Flag | Description |
| :--- | :--- |
| `<files...>` | One or more `.fmod` paths (launching with no arguments opens the GUI) |
| `--out <dir>` | Output root directory (defaults to a subfolder beside each `.fmod`) |
| `--image-format {png,tga,both,none}` | Companion texture preview format alongside `.btex` (default: `png`) |
| `--no-png` | Alias for `--image-format none` |
| `--to-zip` | Also generate an open Mod Package `.zip` with SHA-256 hashes |
| `--inspect` | Print mod metadata, container version, and payload list without extracting |
| `--gui` | Launch the desktop Tkinter GUI |

---

## Output Structure

Extracting `example_mod.fmod` produces a clean folder layout:

```text
example_mod/
├── extraction_manifest.json   # Mod metadata, source_uri_map, derived_previews, diagnostics
├── thumbnail.jpg              # Embedded mod thumbnail (when present)
├── earcs/                     # De-processed EARC member binaries (normalized extensions + previews)
│   └── datas/
│       └── ...
│           ├── model.gmdl.gfxbin
│           ├── material.gmtl.gfxbin
│           ├── script.exml
│           ├── diffuse.btex
│           └── diffuse.png    # (and/or diffuse.tga, tracked under derived_previews)
└── loose/                     # Loose replacement files (when present)
```

- `source_uri_map` maps every extracted binary back to its exact original `data://` EARC URI so repacking tools can target the right archive entry.
- `derived_previews` tracks companion `.png` / `.tga` / `.dds` files separately so they are never accidentally repacked into game archives.

---

## Building the `.exe` from Source

If you want to compile `FFXV_FmodExtractor.exe` yourself on Windows (requires PyInstaller):

```powershell
.\build_exe.ps1
```

This runs a single-file PyInstaller build and outputs `dist\FFXV_FmodExtractor.exe`.

---

## Repository Contents

```text
.
├── fmod_extractor.py   # Complete single-file tool (CLI + Tkinter GUI, stdlib only)
├── extract_fmod.bat    # Windows drag-and-drop / GUI launcher for Python source
├── build_exe.ps1       # One-command PyInstaller --onefile build script
└── README.md           # Documentation
```

---

## Requirements

- **Pre-Built `.exe`:** Windows 10 or Windows 11 (64-bit). No additional runtimes required.
- **From Source:** Python 3.10+ (stdlib only; Tkinter included with standard Windows Python installers).

---

## Legal & Permissions

- Please respect original mod authors' permissions before modifying, reusing, or redistributing extracted mod assets.
- *Final Fantasy XV* and its original game assets are the property of Square Enix. This tool is intended for personal inspection, learning, and modding workflows with legally owned game files.

---

## Shout Outs & Credits

- **Kizari** — Massive thanks for creating Flagrum and for being the primary inspiration and code-study source that made understanding the `.fmod` container and FFXV binary formats possible.
- **AI Tools Used in Development** — Built, reverse-engineered, and verified with assistance from **GLM 5.3**, **Claude Opus 5.5**, **Google Antigravity (Opus)**, and **Gemini (Carbon & Neon)**.
- **The FFXV Modding Community** — Thanks to everyone who has documented FFXV file formats, templates, and tooling over the years.
