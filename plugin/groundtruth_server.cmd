@echo off
rem GroundTruth compute server: double-click to start it on the server PC (see the plugin's Help, "Compute server").
rem It runs the GroundTruth installed next to this file with the Python environment KiCad made for the plugin
rem (open KiCad's PCB editor once after installing the plugin). KiCad itself does not need to run.
rem Options are passed on, e.g.:  groundtruth_server.cmd --workers 2
rem Each start-up step says when it begins and ends, so a stall shows where it is.
setlocal
set "HERE=%~dp0"
set "GTVER=unknown version (no version.txt next to this file)"
if exist "%HERE%version.txt" set /p GTVER=<"%HERE%version.txt"
echo %GTVER%
echo Plugin folder: %HERE%
echo.
set "PY="
rem KiCad names the environment after the plugin (io.github.iwers.groundtruth); any *groundtruth* one will do
for /d %%V in ("%LOCALAPPDATA%\KiCad\*") do (
    for /d %%E in ("%%V\python-environments\*groundtruth*") do (
        if exist "%%E\Scripts\python.exe" set "PY=%%E\Scripts\python.exe"
    )
)
if not defined PY (
    echo The plugin's Python environment was not found under %LOCALAPPDATA%\KiCad\...\python-environments.
    echo Install GroundTruth in KiCad, open the PCB editor once so KiCad builds it, then run this again.
    pause
    exit /b 1
)
set "PYTHONPATH=%HERE%"
set "PYTHONUNBUFFERED=1"
rem The environment's python.exe is a launcher that starts KiCad's Python: what that prints straight into a
rem console window is lost (the window stayed empty). Through a pipe it arrives, so every step's output goes
rem through one: a file for the checks (their exit code is kept), findstr for the server (it passes each line on
rem as it comes).
set "STEP=%TEMP%\groundtruth_server_step.txt"
echo [1/3] Python: %PY%
"%PY%" -c "import sys; print('      ok, Python', sys.version.split()[0])" > "%STEP%" 2>&1
set "RC=%errorlevel%"
type "%STEP%"
if not "%RC%"=="0" goto failed
echo [2/3] loading GroundTruth and NumPy...
"%PY%" -c "import numpy, groundtruth.cli; print('      ok, NumPy', numpy.__version__)" > "%STEP%" 2>&1
set "RC=%errorlevel%"
type "%STEP%"
if not "%RC%"=="0" goto failed
del "%STEP%" 2>nul
echo [3/3] starting the server (leave this window open; Ctrl+C stops it)...
echo.
"%PY%" -m groundtruth serve --log "%LOCALAPPDATA%\GroundTruth\server.log" %* 2>&1 | findstr /r "^"
pause
exit /b 0

:failed
echo.
echo That step failed (see the message above). Send it to whoever set up GroundTruth.
pause
exit /b 1
