"""Generate minimal synthetic .kicad_pcb files for validation tests."""

from __future__ import annotations

from pathlib import Path


def _pad_fp(ref, x, y, w, h, net, layer="F.Cu", kind="smd", drill=None, angle=0, net_id=1):
    layers = '"F.Cu" "F.Mask"' if layer == "F.Cu" else '"B.Cu" "B.Mask"'
    if kind == "thru_hole":
        layers = '"*.Cu" "*.Mask"'
    dr = f"(drill {drill})" if drill else ""
    ang = f" {angle}" if angle else ""
    return f'''  (footprint "Test:Pad" (layer "{layer}") (at {x} {y}{ang})
    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS"))
    (property "Value" "PAD" (at 0 2 0) (layer "F.Fab"))
    (pad "1" {kind} rect (at 0 0{ang}) (size {w} {h}) {dr} (layers {layers}) (net {net_id} "{net}"))
  )
'''


def make_board(path: Path, *, outline=(0, 0, 60, 20), tracks=(), pads=(), vias=(), zones=(), layers=2,
               cu_mm=0.035, thickness=1.6, net="N1") -> Path:
    cu_names = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, layers - 1)] + ["B.Cu"]
    ids = {"F.Cu": 0, "B.Cu": 2}
    for i in range(1, layers - 1):
        ids[f"In{i}.Cu"] = 2 + 2 * i
    layer_defs = "\n".join(f'    ({ids[n]} "{n}" signal)' for n in cu_names)
    d = (thickness - layers * cu_mm) / (layers - 1)
    st = []
    for i, n in enumerate(cu_names):
        st.append(f'      (layer "{n}" (type "copper") (thickness {cu_mm}))')
        if i < layers - 1:
            st.append(f'      (layer "dielectric {i + 1}" (type "core") (thickness {d}) (material "FR4"))')
    x0, y0, x1, y1 = outline
    # Zones are (layer, pts) on the main net, or (layer, pts, net_name) on another net.
    nets = [net] + sorted({z[2] for z in zones if len(z) > 2 and z[2] != net})
    net_defs = "\n".join(f'  (net {i + 1} "{n}")' for i, n in enumerate(nets))
    body = [f'''(kicad_pcb (version 20241229) (generator "pcbsim-test")
  (general (thickness {thickness}))
  (layers
{layer_defs}
    (25 "Edge.Cuts" user)
    (1 "F.Mask" user)
    (3 "B.Mask" user)
  )
  (setup (stackup
{chr(10).join(st)}
  ))
  (net 0 "")
{net_defs}
  (gr_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.05) (type default)) (fill no) (layer "Edge.Cuts"))
''']
    for p in pads:
        body.append(_pad_fp(**p, net=net))
    for (xa, ya), (xb, yb), w, layer in tracks:
        body.append(f'  (segment (start {xa} {ya}) (end {xb} {yb}) (width {w}) (layer "{layer}") (net 1))\n')
    for x, y, size, drill in vias:
        body.append(f'  (via (at {x} {y}) (size {size}) (drill {drill}) (layers "F.Cu" "B.Cu") (net 1))\n')
    for z in zones:
        layer, pts = z[0], z[1]
        zn = z[2] if len(z) > 2 else net
        p = " ".join(f"(xy {a} {b})" for a, b in pts)
        body.append(f'''  (zone (net {nets.index(zn) + 1}) (net_name "{zn}") (layer "{layer}")
    (polygon (pts {p}))
    (filled_polygon (layer "{layer}") (pts {p}))
  )
''')
    body.append(")\n")
    path.write_text("".join(body), encoding="utf-8")
    return path
