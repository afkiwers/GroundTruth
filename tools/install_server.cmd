@echo off
rem GroundTruth compute server: double-click to install (or update) it as a background service on this PC.
rem
rem   install_server.cmd                     install, or update an existing install (keeps key, settings, jobs)
rem   install_server.cmd install --workers 2 --threads 8 --gpu off
rem   install_server.cmd install --cuda cu126           with PyTorch for an NVIDIA GPU, a few GB
rem   install_server.cmd install --key                  join a cluster: asks for the key of its servers
rem   install_server.cmd install --lead                 (this server leads the cluster; --no-lead undoes it)
rem   install_server.cmd install --remote-update on     the users' plugins may update it (signed builds only)
rem   install_server.cmd install --address              the address it announces: pick one of this PC's networks
rem   install_server.cmd status ^| log ^| key ^| stop ^| start ^| uninstall
rem   install_server.cmd log --find split               the log lines about splitting (also a job id, cluster)
rem
rem It asks for administrator rights, then runs server_setup.py next to this file with KiCad's own Python.
rem Everything goes to C:\ProgramData\GroundTruth; the service starts with Windows, nobody needs to log in.
rem
rem No message is echoed inside a bracketed block: cmd.exe ends a block at the first ) it meets, also in
rem an echo or an expanded variable such as a path with (x86), and then fails with ". was unexpected".
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if not errorlevel 1 goto :elevated
echo Asking Windows for administrator rights: the installation then runs in a NEW window,
echo which shows each step as it goes. This window can be closed.
if "%~1"=="" goto :elevate_plain
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs -WorkingDirectory '%~dp0'"
exit /b
:elevate_plain
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0'"
exit /b

:elevated
set "PY="
for /d %%V in ("%ProgramFiles%\KiCad\*") do if exist "%%V\bin\python.exe" set "PY=%%V\bin\python.exe"
if not defined PY goto :no_kicad
set "CMD=%~1"
if not "%CMD%"=="" goto :given
set "CMD=install"
if exist "%ProgramData%\GroundTruth\run_server.py" set "CMD=update"
"%PY%" server_setup.py %CMD%
goto :after
:given
"%PY%" server_setup.py %*
:after
if errorlevel 1 goto :failed
goto :end

:no_kicad
echo KiCad was not found in the Program Files folder. Install KiCad 10 on this PC first:
echo the server runs on KiCad's Python.
goto :end

:failed
echo.
echo That did not work: see the messages above. README.md explains them.

:end
echo.
pause
