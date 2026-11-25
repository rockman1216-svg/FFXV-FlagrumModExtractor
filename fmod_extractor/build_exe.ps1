<#
.SYNOPSIS
    Builds a single-file standalone FFXV_FmodExtractor.exe using PyInstaller.

.DESCRIPTION
    Optional packaging helper for standalone/fmod_extractor/fmod_extractor.py.
    Requires PyInstaller (`python -m pip install pyinstaller`) in the chosen
    Python environment. Produces `dist\FFXV_FmodExtractor.exe`.

.PARAMETER PythonExe
    Python interpreter to invoke (defaults to `python` on PATH, falling back to
    `$env:USERPROFILE\miniconda3\python.exe`).

.PARAMETER Windowed
    Hide the console window (GUI-only build). Omit to keep both CLI (`--inspect`,
    `--to-zip`, drag-and-drop) and GUI (`--gui` / double-click) functional in a
    single executable.
#>
param(
    [string]$PythonExe = "",
    [switch]$Windowed
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourcePy  = Join-Path $ScriptDir "fmod_extractor.py"

if (-not $PythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
    } elseif (Test-Path "$env:USERPROFILE\miniconda3\python.exe") {
        $PythonExe = "$env:USERPROFILE\miniconda3\python.exe"
    } else {
        Write-Error "Python 3 interpreter not found. Pass -PythonExe <path\to\python.exe>."
    }
}

& $PythonExe -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] PyInstaller is not installed in: $PythonExe" -ForegroundColor Red
    Write-Host "Install it first with:  `"$PythonExe`" -m pip install pyinstaller" -ForegroundColor Yellow
    exit 1
}

$PyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name", "FFXV_FmodExtractor",
    "--distpath", (Join-Path $ScriptDir "dist"),
    "--workpath", (Join-Path $ScriptDir "build"),
    "--specpath", $ScriptDir
)
if ($Windowed) {
    $PyArgs += "--windowed"
}
$PyArgs += $SourcePy

Write-Host "Building standalone FFXV_FmodExtractor.exe..." -ForegroundColor Cyan
& $PythonExe @PyArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed with exit code $LASTEXITCODE."
}

$OutExe = Join-Path $ScriptDir "dist\FFXV_FmodExtractor.exe"
Write-Host "[OK] Built standalone executable: $OutExe" -ForegroundColor Green
