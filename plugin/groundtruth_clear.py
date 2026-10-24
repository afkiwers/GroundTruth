"""KiCad IPC plugin entry point of the "Clear GT markers" action (see plugin.json). KiCad 10 starts an action's
entry script without the "args" listed in plugin.json, so each action has a script of its own."""
import time

STARTED = time.time()

import os  # noqa: E402
import sys  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # the bundled copy of the groundtruth package lives next to this file

from groundtruth.ui.app import plugin_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(plugin_main(["clear"], started=STARTED))
