@echo off
REM Gerber to G-code converter wrapper for Windows
REM
REM Usage:
REM   gerber2gcode.bat -t traces.gtl -o output.nc
REM   gerber2gcode.bat -t board.gtl -e edges.gm1 -d holes.drl -o board.nc
REM   gerber2gcode.bat -t board.gtl --separate
REM
REM See README.md for full options or run: gerber2gcode.bat --help
REM
REM This script automatically uses config.yaml from the tool directory
REM unless you specify --config manually.

setlocal

REM Use the venv Python from the tool directory if available
set "VENV_PYTHON=%~dp0venv\Scripts\python.exe"
if exist "%VENV_PYTHON%" (
    set "PYTHON=%VENV_PYTHON%"
) else (
    set "PYTHON=python"
)

REM Check if --config was provided in arguments
echo %* | findstr /i "\-\-config" >nul
if %errorlevel% equ 0 (
    REM User provided --config, use their arguments as-is
    "%PYTHON%" "%~dp0gerber_to_gcode.py" %*
) else (
    REM No --config provided, use default from tool directory
    "%PYTHON%" "%~dp0gerber_to_gcode.py" --config "%~dp0config.yaml" %*
)

endlocal
