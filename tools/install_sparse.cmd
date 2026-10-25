@echo off
rem GroundTruth's faster sparse solver (Intel MKL PARDISO) on Windows: double-click this file, or run it in a console.
rem
rem   install_sparse.cmd                install it into the GroundTruth plugin's environment
rem   install_sparse.cmd --uninstall    remove it again (SuperLU, as before)
rem
rem It runs install_sparse.py (next to this file) with KiCad 10's own Python. Close KiCad first, and start the
rem GroundTruth plugin once in KiCad before (so KiCad has made the plugin's environment).
setlocal
cd /d "%~dp0"
set "PY=%ProgramFiles%\KiCad\10.0\bin\python.exe"
if not exist "%PY%" (
    echo KiCad 10's Python was not found at "%PY%". Install KiCad 10, or run with another Python:
    echo   ^<python^> install_sparse.py
    goto :end
)
tasklist /fi "imagename eq kicad.exe" 2>nul | find /i "kicad.exe" >nul
if not errorlevel 1 (
    echo KiCad is running: close every KiCad window first, then run this again.
    goto :end
)
echo Installing Intel MKL into the GroundTruth plugin's environment (about 250 MB to download)...
echo.
"%PY%" install_sparse.py %1
:end
echo.
pause
