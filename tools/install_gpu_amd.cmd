@echo off
rem GroundTruth GPU support for an AMD Radeon (e.g. the Radeon 860M): double-click this file, or run it in a console.
rem
rem   install_gpu_amd.cmd               the chip from the card's name (Radeon 860M: gfx1152)
rem   install_gpu_amd.cmd gfx1153       the chip given (a Radeon 840M is gfx1152 or gfx1153)
rem
rem It runs install_gpu.py --amd and then check_gpu.py (both next to this file) with KiCad 10's own Python. Close
rem KiCad first, and start the GroundTruth plugin once in KiCad before (so KiCad has made the plugin's environment).
setlocal
cd /d "%~dp0"
set "PY=%ProgramFiles%\KiCad\10.0\bin\python.exe"
if not exist "%PY%" (
    echo KiCad 10's Python was not found at "%PY%". Install KiCad 10, or run with another Python:
    echo   ^<python^> install_gpu.py --amd
    goto :end
)
tasklist /fi "imagename eq kicad.exe" 2>nul | find /i "kicad.exe" >nul
if not errorlevel 1 (
    echo KiCad is running: close every KiCad window first, then run this again.
    goto :end
)
echo Installing PyTorch on AMD ROCm into the GroundTruth plugin's environment (a few GB to download)...
echo.
"%PY%" install_gpu.py --amd %1
if errorlevel 1 (
    echo.
    echo The installation failed: see above. GPU_README.md next to this file, "Problems", explains the messages.
    goto :end
)
echo.
echo Checking what GroundTruth will use...
echo.
"%PY%" check_gpu.py
echo.
echo Done. Start KiCad again; after a Simulate the status line says "solved on GPU ..." when the GPU did the work.
echo If the verdict above says the GPU is not clearly faster, set Preferences... ^> GPU: off in the plugin.
:end
echo.
pause
