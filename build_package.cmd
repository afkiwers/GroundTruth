@echo off
rem Build the GroundTruth package for KiCad's Plugin and Content Manager from the current files.
rem
rem   build_package.cmd                 the files as they are now (uncommitted changes included)
rem   build_package.cmd --committed     the last commit only
rem   build_package.cmd --status stable (any other option goes to tools\make_pcm_package.py)
rem
rem The zip lands in out\dist; install it in KiCad with Tools > Plugin and Content Manager > Install from File.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo No Python environment in .venv: create it first, see CLAUDE.md ^("Environment"^).
    goto :end
)
set "MODE=--working-tree"
if /i "%~1"=="--committed" (
    set "MODE="
    shift
)
".venv\Scripts\python.exe" tools\make_pcm_package.py %MODE% %1 %2 %3 %4 %5 %6
if errorlevel 1 (
    echo.
    echo Building the package failed: see above.
    goto :end
)
echo.
echo Install it in KiCad: Tools ^> Plugin and Content Manager ^> Install from File..., pick the zip above,
echo Apply Pending Changes, restart KiCad. Only one GroundTruth may be installed: remove a copied one first with
echo   .venv\Scripts\python.exe tools\install_plugin.py --uninstall
:end
rem started by a double-click: keep the window open to read the result
echo %cmdcmdline% | find /i "%~0" >nul && pause
endlocal
