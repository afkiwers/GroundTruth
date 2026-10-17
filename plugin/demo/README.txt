GroundTruth demo board
======================

A 40 x 20 mm, 4-layer board: the clock SIG runs from U1 on F.Cu to a via at x = 20 mm and on to U2 on B.Cu. F.Cu is
referenced to the GND plane on In1, B.Cu to the GND plane on In2. The two planes are joined only by the ICs' GND vias
at the ends: there is no stitching via where the signal changes layers.

Try it:
1. In KiCad: File > Open Project > groundtruth_demo.kicad_pro (in this folder), then open its PCB editor.
2. Start GroundTruth (the toolbar button "Analyse return paths"). The settings file groundtruth/groundtruth_demo.groundtruth.yaml
   already lists the net SIG (Nets tab).
3. Run checks (top bar): the Issues tab shows a missing stitching via at the layer change (layer_change_stitch).
   Clicking it shows the spot in KiCad.
4. Simulate, then the Heatmap tab (the SIG view in "Route"): on each GND plane the return current runs under its
   trace up to the layer change, then fans out over the whole plane to the far IC's GND via. The colour is the
   current density; the arrows, all the same size, point the way it flows, and the more of them, the more current.
   Untick "Log scale" to see where most of it flows. Scroll down (the mouse wheel; Ctrl + wheel over a map zooms
   it): the cross-section under the maps ("Cross-section" ticked) follows the return along the route, layer by
   layer: In1 and In2 share it half and half at the layer change, and far from it about 30 % still flows on the
   plane the signal has left. The tile "plane displacement" (12.9x the signal current at 1450 MHz) is a resonance of the two
   planes' capacitance with the ICs' GND vias: a current circulating between them.
5. Fixes tab > Suggest fixes: a GND stitching via next to the signal via, predicted and verified. "Draw ticked on
   board" puts it on the board; "Add ticked to what-if" and Simulate with "with what-if edits" ticked (top bar) show
   the return staying in narrow bands under the traces, and the cross-section shows it swapping planes right at
   the vias. "Compare what-if" lists what changed.
6. Clear EMI layers (top bar) removes everything GroundTruth drew.

Preferences... (top bar, right) sets the resolution, the via markers and part outlines of the maps and the theme;
Help (F1) explains every tab.

Reinstalling the plugin puts the demo back as it was (copy this folder elsewhere to keep your changes).
