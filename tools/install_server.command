#!/bin/bash
# GroundTruth compute server for macOS: double-click to install (or update) it as a background service on this Mac.
#
#   ./install_server.command                     install, or update an existing install (keeps key, settings, jobs)
#   ./install_server.command install --workers 2 --threads 8
#   ./install_server.command install --key       join a cluster: asks for the key of its servers
#   ./install_server.command install --remote-update on   the users' plugins may update it (signed builds only)
#   ./install_server.command install --address    the address it announces: pick one of this Mac's networks
#   ./install_server.command status | log | key | stop | start | uninstall
#
# It asks for your password (administrator rights, through sudo), then runs server_setup.py next to this file with
# a Python 3.11 or newer (KiCad.app's if it is new enough, else python.org's, Homebrew's or python3). Everything goes to /Library/Application Support/GroundTruth;
# the service is a launchd daemon that starts at boot, nobody needs to log in.
#
# Downloaded from the internet, macOS may refuse to open it at first ("unidentified developer"): right-click it,
# Open, Open; or in Terminal: xattr -d com.apple.quarantine install_server.command

cd "$(dirname "$0")" || exit 1

# the first Python 3.11 or newer (GroundTruth needs it; KiCad 10 for macOS brings 3.9): KiCad's, python.org's,
# Homebrew's, the PATH's
PY=""
for p in /Applications/KiCad*/KiCad.app/Contents/Frameworks/Python.framework/Versions/*/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3)"; do
    if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        PY="$p"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "GroundTruth needs Python 3.11 or newer, and this Mac has none (KiCad for macOS brings an older one)."
    echo "Install Python 3.13 from https://www.python.org/downloads/macos/ (the macOS 64-bit universal2 installer),"
    echo "then run this again."
    read -n 1 -s -r -p "Press any key to close this window."
    exit 1
fi

ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then ARGS=(install); fi
# a --python given on the command line wins (e.g. python.org's Python 3.11+ when KiCad's cannot make a venv)
case " ${ARGS[*]} " in
    *" --python "*) ;;
    *) ARGS+=(--python "$PY") ;;
esac
echo "GroundTruth compute server: $PY"
echo "Administrator rights are needed: enter your Mac's password (it is not shown while you type)."
sudo "$PY" server_setup.py "${ARGS[@]}"
RC=$?
echo
read -n 1 -s -r -p "Press any key to close this window."
exit $RC
