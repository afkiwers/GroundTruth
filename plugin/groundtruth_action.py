"""KiCad IPC plugin entry point (see plugin.json). KiCad runs this in the plugin's virtualenv."""
import time

STARTED = time.time()   # first line: the start-up log measures Python's own start-up up to here

import os  # noqa: E402
import sys  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # the bundled copy of the groundtruth package lives next to this file

from groundtruth.ui.app import plugin_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(plugin_main(sys.argv[1:], started=STARTED))
