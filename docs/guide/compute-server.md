# Compute server

Running the heavy analyses on other machines on the network. Back to the [README](../../README.md).

The heavy analyses (Simulate, Suggest fixes, Check in Simulate, Compare what-if, Thermal, Current capacity) can run
on a powerful PC on the local network while you work in KiCad on your own machine:

```sh
groundtruth serve [--host 0.0.0.0] [--port 8765] [--workers 1] [--gpu auto] [--threads auto] [--keep-hours 24] [--log server.log]
                  [--key-file FILE] [--new-key]
                  [--lead | --no-lead] [--peers HOST,...] [--beacon-port 8766]
```

**Several servers form a cluster** when they share the access key: they find each other on the local network (signed
UDP beacons, port 8766) and the first to find another becomes the lead (`--lead` makes one the lead; `--peers` adds
servers on another subnet). `install_server.cmd install --key` joins a further server to the cluster (asks for the
key), Test connection and `install_server.cmd status` show the members, Preferences' **Find servers** lists the servers
with the user's key. **A Simulate is split over the cluster** by item (nets, power stages, motor bridges): a member
passes it to the lead, the lead spreads the items over the servers that answer and joins the parts into the same
result one server gives (analysis/split.py, remote/split.py).

**As a background service** (recommended): unzip the server package `out/dist/groundtruth-server-<version>-<commit>.zip`
(`tools/make_server_package.py`) on the server and double-click `install_server.cmd` (as administrator; KiCad 10 must
be installed there): it installs into `C:\ProgramData\GroundTruth` with its own libraries, opens the firewall
port, registers a scheduled task that starts the server at boot as SYSTEM, and shows the address and key
(`install_server.cmd status | key | stop | start | uninstall`, `--cuda cu126` for an NVIDIA GPU; its README.md).
**Remote updates**: a server installed with `--remote-update on` (asked on a first install) is updated from the
plugin: Preferences, Compute server, **Servers** lists the cluster's servers with their versions, and **Update** (one server) or **Update all**
installs the plugin's own version on the out-of-date ones (their running jobs finish first; a new server that does
not stay up is rolled back by the service). Only packages signed with the release key of the build PC the server was
last installed from by hand are taken (`tools/make_pcm_package.py` signs; the key is in
`%LOCALAPPDATA%\GroundTruth\release\` of the build PC, or the release workflow's encrypted secret, never in the
repository; `tools/release_key.pub` is its public half, and a build with any other key stops), and only newer ones.
**On a Mac** (Apple Silicon such as an M2, or Intel; in use on an M2 since 2026-10-15): unzip the same package and
double-click `install_server.command` (the first time: right-click, Open, Open), then type the Mac's password. It
installs into `/Library/Application Support/GroundTruth` as the launchd daemon `com.groundtruth.server` (starts at
boot) with a Python 3.11 or newer (KiCad for macOS brings 3.9: install Python 3.13 from python.org first), allows it in the
macOS firewall and shows the address and key; `./install_server.command status | log | key | stop | start | uninstall`,
`install --key` joins a cluster. A Mac solves on its CPU only (no CUDA; Apple GPUs have no double precision).
**Or in a window:** on the server, double-click `groundtruth_server.cmd` in the installed plugin's folder (it runs the server with the Python environment KiCad made for the plugin; KiCad itself need not run; the plugin's Help, "Compute server: set up and run", has the steps). Every start shows the access key (made on the first start, kept in `%LOCALAPPDATA%\GroundTruth\server.key`), the addresses to enter and a READY line. In the plugin:
Preferences, **Compute server**: run analyses on a GroundTruth compute server, its address and the key; **Test
connection**. Simulate and Suggest fixes are **handed over**: the window is free at once, the **Server jobs** tab
lists the job, and its result comes back by itself whenever the server can be reached (a sleeping laptop or a night at
home only delay it) and is shown, or offered there (Show results). Only the user cancels a job (Cancel job, or
Remove from list, which also takes it off the list; a job whose server can't be reached any more is removed after a
second question). Check in Simulate,
Compare what-if, Thermal and Current capacity run there too while the window waits. Jobs queue first come, first
served (`workers` at once; with as many workers as GPUs, one GPU each); every job lives on the server's disk, so a
restart keeps the queue and the results; a result waits 24 h (`keep_hours`) and is removed once fetched. The server's
settings are in `%LOCALAPPDATA%\GroundTruth\server.json` (made on the first start; options override them). A server
that can't be reached when a job starts offers to run it locally. Requests and results are signed (HMAC-SHA256 with the
key, no replay) and only GroundTruth's own data is unpickled; the traffic is not encrypted (local network only). The
server must run the same GroundTruth code (and NumPy major version) as the plugin, otherwise it refuses the job.
