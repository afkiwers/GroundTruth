# GroundTruth compute server

Runs the GroundTruth KiCad plugin's heavy analyses (Simulate, Suggest fixes, Thermal, …) for the users on the local
network. Users keep working in KiCad on their own computers; the server computes in the background and keeps each
result for them until they fetch it (24 hours by default).

This package installs it as a **background service**: it starts with Windows, runs without anyone logged in, and
restarts itself if it ever stops.

## Requirements

- Windows 10/11 or Windows Server, or a **Mac** (macOS on Apple Silicon such as an M2, or Intel), on the office
  network. A Mac solves on its CPU only (no CUDA, and Apple GPUs have no double precision for the solver).
- **KiCad 10** installed on the server (the server runs on KiCad's Python; KiCad itself never needs to be opened).
- Internet access during the install (the libraries come from PyPI, about 300 MB; PyTorch for a GPU a few GB).
- The **same GroundTruth version** as the users' plugins: install the KiCad package and this server package from
  the same build (their names carry the same version and commit). The server refuses jobs from another version.
- For a big Simulate: plenty of memory, and a **page file** of at least the RAM's size (Windows: System, About,
  Advanced system settings, Performance, Advanced, Virtual memory: "System managed size"). The sparse solver
  reserves far more memory than it uses; with a small page file Windows refuses it although RAM is free.

## Install

1. Unzip this package anywhere (e.g. `C:\Temp\groundtruth-server-…`).
2. Double-click **install_server.cmd** and allow it to run as administrator.

It installs into `C:\ProgramData\GroundTruth`, opens TCP port **8765** in the firewall for the private and domain
networks (never public), starts the service, and finally shows:

```
In each user's plugin: Preferences, Compute server: run analyses on a GroundTruth compute server,
  Server address: BIGSERVER or 192.168.1.20
  Access key:     <the key>
```

Give each user the address and the key. Keep the key like a password: anyone on the network with it can run jobs.

Options (from a command prompt in the unpacked folder):

| Command | |
|---|---|
| `install_server.cmd install --workers 2 --threads 8 --gpu off` | two jobs at once with 8 solver threads each, no GPU |
| `install_server.cmd install --cuda cu126` | also PyTorch for an NVIDIA GPU (`cu128` for the newest cards) |
| `install_server.cmd install --key` | join a cluster: asks for the key of its servers (below) |
| `install_server.cmd install --lead` | this server leads its cluster (`--no-lead` undoes it) |
| `install_server.cmd install --address` | the address this server announces to the other servers and the users: lists this PC's network cards and addresses to pick one (for a PC with two networks, see [A PC with several networks](#a-pc-with-several-networks)) |
| `install_server.cmd install --address 192.168.0.6` | that address directly (it must be one of this PC's); `--address auto`: every address again (the default) |
| `install_server.cmd install --remote-update on` | the users' plugins may update this server with signed packages (Preferences, Compute server, Servers; see [Remote updates](#remote-updates-from-the-users-plugins)); `off` turns it off. A first install asks |
| `install_server.cmd status` | the service, and the server's name and IP addresses (and the one it announces), version, cores, memory, GPUs, queue and cluster, remote updates and the last one, and where it is installed (Path) |
| `install_server.cmd log` | the end of server.log and service.log (`--find split`: only those lines; `--lines 200`). The folder is for administrators only, so Explorer and Notepad can't open the logs: use this |
| `install_server.cmd key` | the access key again |
| `install_server.cmd stop` / `start` | stop / start the service |
| `install_server.cmd uninstall` | remove the service and the firewall rule (keeps the folder; `--purge` removes it) |

## On a Mac

1. **Python 3.11 or newer** must be on the Mac: KiCad for macOS brings Python 3.9, too old. Install Python 3.13
   from https://www.python.org/downloads/macos/ (the macOS 64-bit universal2 installer) once.
2. Unzip the package (double-click the zip in Finder).
3. Double-click **install_server.command**. If macOS says it is from an unidentified developer: right-click it,
   **Open**, **Open** (or in Terminal: `xattr -d com.apple.quarantine install_server.command`).
4. Enter your Mac's password when Terminal asks (administrator rights, through `sudo`).

If the Mac says you "do not have appropriate access privileges" (the file lost its execute permission, e.g. copied
through a Windows share or a cloud folder): open Terminal, type `bash ` (with a space), drag install_server.command
into the window and press Return. That runs it without the permission and without the Gatekeeper question.

It installs into `/Library/Application Support/GroundTruth` (readable by root only: the access key is a secret) with
the first Python 3.11 or newer it finds (KiCad.app's if new enough, python.org's, Homebrew's, `python3`), registers the launchd daemon **com.groundtruth.server**
(`/Library/LaunchDaemons/com.groundtruth.server.plist`: starts at boot, nobody needs to log in, restarted if it
stops) and allows the server's Python in the macOS firewall (it matters only when the firewall is on). Then it shows
the address and key as on Windows.

The commands are the same, from Terminal in the unpacked folder: `./install_server.command status | log | key |
stop | start | uninstall`, `./install_server.command install --key` (join a cluster), `--workers 2 --threads 8`.
Windows and Mac servers can be in one cluster (the same key, the same GroundTruth version).

Without a Python 3.11 or newer the install stops and says so. A server installed earlier on KiCad's Python 3.9 (every
Simulate failed: "cannot import name 'pairwise' from 'itertools'") is repaired by installing Python 3.13 and running
install_server.command again: it makes the server's environment anew (key and settings are kept). Another Python:
`bash install_server.command install --python /path/to/python3`.

Test connection says "no GPU found (CPU solves): a Mac": that is normal, nothing is missing (no CUDA on macOS).

Tried on an M2 MacBook Air (2026-10-15).

## Several servers (a cluster)

Servers with the **same access key** find each other on the local network: every few seconds each one announces
itself on **UDP port 8766** (the installer opens it next to TCP 8765, private and domain networks), signed with the
key; servers with another key are ignored, and the key itself is never sent. The first server to find another
becomes the **lead**.

1. Install the first server as above.
2. On every further server: `install_server.cmd install --key`. It asks for the key: run
   `install_server.cmd key` on the first server and type or paste that (right-click pastes in the console); the
   installer then says which servers on the network use that key, so a typo shows at once. (A key typed on the command line, `--key <key>`,
   also works but stays in the console's history.)
3. Optional: `install_server.cmd install --lead` on the server that should always lead (else the first to find
   another leads; when the lead goes off, the others elect a new one within half a minute).
4. A server on another subnet (behind a router; broadcasts don't cross it): `install_server.cmd install --peers
   NAME1,NAME2` on both sides (`NAME:port` if its UDP port isn't 8766).

`install_server.cmd status` shows the cluster: its size and since when, the lead (since when, how chosen), every
server with its name, IP address, cores, memory, GPUs, jobs and time in the cluster, any server with the key but
another GroundTruth version, and the last 10 events (joined, lost, back, restarted, lead changes; all of them are in
server.log). In the plugin, Preferences, Compute server, **Find servers** lists the
servers with the user's key, the lead first.

**A Simulate is split over the cluster:** the lead (a member passes the job on to it) spreads the board's nets,
power stages and motor bridges over the servers that answer by what each can do (an item only where its memory
fits: a 16x power stage needs about 9 GB; more to the faster servers; what each item and server really needed and
did is kept in split_stats.json for the next run), and joins the parts into one result (the same as from one
server; a note names the servers). A part that runs out of memory moves to a server it fits. The parts show in each server's
queue and log as "part" jobs. A server lost for two minutes has its part redone by the lead. Other jobs (Thermal,
Suggest fixes, …) run on one server as before.

### A PC with several networks

A server announces every address of its real networks, and the others take the one on their own subnet. On a PC with
two network cards (a second office network, a lab network) the others may pick the wrong one, or not hear it at all:
Windows sends a broadcast out of one card only. Choose the address the others and the users reach it at:

    install_server.cmd install --address

lists this PC's network cards and addresses to pick one (`--address 192.168.0.6` sets it directly, checked against
this PC's addresses; `--address auto` goes back to all of them). The server then announces that address alone and
broadcasts on its network. `install_server.cmd status` shows the server's addresses and the one it announces. Every
server also broadcasts on each of its networks' own broadcast address (192.168.0.255), so a second card's network
hears it too.

## Update

Unzip the new server package and double-click its **install_server.cmd**: an existing install is updated (the key,
settings, queued jobs and waiting results stay). Update the users' plugins with the KiCad package of the same build.
A job running during the update starts over afterwards.

### Remote updates (from the users' plugins)

A server installed with remote updates on is updated from any GroundTruth plugin: **Preferences, Compute server,
Servers** lists the server and the others of its cluster with their versions, and **Update** (or **Update all**)
installs the plugin's own version on the out-of-date ones. Turn it on at the server once: a first install asks
("Allow remote updates? [y/N]"), or `install_server.cmd install --remote-update on` (`off` turns it off again).

- Only **signed builds** are taken: the build PC signs every package with its release key (made on its first build,
  kept in `%LOCALAPPDATA%\GroundTruth\release\` there, never in the repository: keep and back it up). The server
  trusts the key of the package it was **installed from by hand**; every manual install or update resets it to the
  installed package's key, and a remote update never changes it. A package from another build PC is refused until
  it has been installed by hand once. The access key alone can't push code to a server.
- Only a **newer** build than the server's own is taken (an old package can't take a server back).
- A busy server **finishes its running jobs** first; queued jobs wait and run after the update.
- The service installs the update with the new package's own installer, restarts, and goes **back to the previous
  version** by itself if the new server does not stay up for two minutes (`install_server.cmd status` says how the
  last update went; the previous program is kept in `app.prev`).
- Libraries are installed only when the new build needs other ones (pip, internet access on the server).

## Settings

`C:\ProgramData\GroundTruth\server.json`, read at every start of the service (`install_server.cmd stop`, then
`start`, after editing):

```json
{ "port": 8765, "workers": 1, "threads": "-", "gpu": "auto", "keep_hours": 24,
  "beacon_port": 8766, "lead": false, "peers": [], "remote_update": false, "address": "" }
```

- **workers**: jobs at once. 1: each job gets the whole machine, the others wait (first come, first served). 2 or
  more lets several people simulate at the same time; give each job fewer threads. With as many workers as NVIDIA
  GPUs, each job gets its own GPU.
- **threads**: solver threads per job: a number (e.g. the physical cores ÷ workers), or `"-"` for each user's
  Preferences. GroundTruth runs fewer when the memory would not suffice.
- **gpu**: `"auto"` uses an NVIDIA GPU if PyTorch is installed (`--cuda`), `"off"` never, `"-"` as each user chooses.
- **keep_hours**: how long a finished result waits for its owner (it is removed once fetched).
- **port**: change it only together with the firewall rule (install again with `--port`) and the users' address
  (`name:port`).
- **beacon_port**, **lead**, **peers**: the cluster (above): the UDP port the servers find each other on (0: no
  cluster), this server as the lead, servers on another subnet.
- **remote_update**: take signed updates from the users' plugins (Remote updates, above).
- **address**: the address the server announces (A PC with several networks, above); `""`: every real one.

## Files

| | `C:\ProgramData\GroundTruth\` (administrators and SYSTEM only) |
|---|---|
| `server.key` | the access key |
| `server.json` | the settings |
| `server.log` | every job: queued, started, finished or failed (with the error); the cluster's events; why a Simulate was not split (`install_server.cmd log`) |
| `service.log` | the service's own messages (start-ups, a crash) |
| `server_jobs\` | the jobs and their results waiting to be fetched |
| `app\`, `venv\`, `run_server.py`, `task.xml` | the installed program (`app.prev\`: the one before the last remote update) |
| `release_key.pub` | the release key remote updates must be signed with (from the last manual install) |
| `updates\` | a remote update on its way, and `last.json`: how the last one went |

The service is the scheduled task **GroundTruth compute server** (Task Scheduler, Task Scheduler Library): at boot,
as SYSTEM, no time limit. Stopping it also ends a running job within seconds.

## Problems

- **"run this as administrator"**: start install_server.cmd by double-click and allow the prompt.
- **Users can't connect** (Test connection: "can't be reached"): `install_server.cmd status` on the server; is the
  network profile Public (the rule allows private and domain only)? Is another firewall in between?
- **"wrong access key"**: the users' key differs from `install_server.cmd key`.
- **The servers don't see each other** (status: "standalone"): the same key on all (`install_server.cmd key`)?
  The same subnet (else `--peers`)? UDP 8766 blocked by another firewall?
- **A server is in the cluster but missing or unreachable in Preferences' Servers**, or the others list it at an
  address nobody reaches: a PC with two networks announces both. `install_server.cmd install --address` on it and
  pick the address the others reach it at (`install_server.cmd status` lists its addresses); then **Check** in
  Preferences.
- **"install the same package on both"**: the server and the plugin are different builds: update one of them.
- **A job fails for want of memory with RAM free**: enlarge the page file (Requirements), or fewer workers/threads.
- Anything else: `install_server.cmd log` (server.log and service.log); the users' Server jobs tab shows a failed job's error in full.
