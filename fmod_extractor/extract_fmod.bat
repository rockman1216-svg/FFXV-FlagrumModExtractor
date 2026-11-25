@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "EXTRACTOR=%SCRIPT_DIR%fmod_extractor.py"
set "PY_EXE="

where py >nul 2>nul && (
    py -3 -c "import sys" >nul 2>nul && set "PY_EXE=py -3"
)
if not defined PY_EXE (
    where python >nul 2>nul && set "PY_EXE=python"
)
if not defined PY_EXE (
    if exist "%USERPROFILE%\miniconda3\python.exe" (
        set "PY_EXE=%USERPROFILE%\miniconda3\python.exe"
    )
)

if not defined PY_EXE (
    echo [ERROR] Python 3 was not found on PATH or in %%USERPROFILE%%\miniconda3.
    echo Install Python 3.10+ or build FFXV_FmodExtractor.exe with build_exe.ps1.
    pause
    exit /b 1
)

if "%~1"=="" (
    %PY_EXE% "%EXTRACTOR%" --gui
    exit /b %ERRORLEVEL%
)

echo Extracting Flagrum .fmod archive(s)...
echo.
%PY_EXE% "%EXTRACTOR%" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo [FAILED] Extraction exited with code %RC%.
) else (
    echo [DONE] All .fmod archive(s) extracted successfully.
)
pause
exit /b %RC%
