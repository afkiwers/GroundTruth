"""KiCad IPC plugin entry point of the "Add selected net to GroundTruth" action (see plugin.json): hands the nets
of the tracks, vias or pads selected in the PCB editor to the GroundTruth window of the board, or opens it."""
import time

STARTED = time.time()

import os  # noqa: E402
import sys  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # the bundled copy of the groundtruth package lives next to this file

from groundtruth.ui.app import plugin_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(plugin_main(["add-net"], started=STARTED))
