"""The figures' text is readable and fits (report/plots.py). The user, about the 6-7 pt legends and titles: "the
legend and graph descriptions are very small and hard to read". At the plugin's sizes (its canvases on a 1920 x 1080
screen and on a laptop's) and the report's: no text under 8 pt, none cut off by the figure's edge, and no title,
note, legend, axis label or row of tick labels running into another one or into another plot."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.text import Text

from boardgen import discrete_buck_board, layer_change_board
from conftest import make
from groundtruth.analysis.detect import detect_converters
from groundtruth.analysis.simulate import simulate
from groundtruth.config.settings import NetSpec, SettingsFile
from groundtruth.report.plots import (FS_MARK, FS_TICK, FS_TITLE, draw_heatmap, draw_section, draw_spectrum,
                                      heatmap_height_in, section_height_in)
from groundtruth.ui.results_panels import SPECTRUM_IN

# (maps, section, spectrum) in inches: the plugin's canvases on its scrolling page, full width, on a 1920 x 1080
# screen and on a 1366 x 768 laptop (the section at section_height_in: None), then the report's (report/html.py,
# cli.py) and the narrow plots of the plugin before it scrolled (a cramped figure must still not overlap)
PLUGIN = [((19.1, 6.4), (19.1, None), (19.1, SPECTRUM_IN)), ((13.2, 4.2), (13.2, None), (13.2, SPECTRUM_IN))]
REPORT = [((9.5, 6.0), (9.5, 3.4), (9.5, 3.4)), ((9.5, 7.8), (9.5, 3.4), (9.5, 3.8)),
          ((13.0, 3.2), (8.0, 1.45), (5.0, 1.45))]


@pytest.fixture(scope="module")
def route(tmp_path_factory):
    """The demo's layout with plane capacitance: the GND vias at both ICs carry the return (labels near both ends
    of the section) and the plane displacement gets a title line of its own on a tall section."""
    tmp = tmp_path_factory.mktemp("txt")
    board, sc, _ = make(tmp, layer_change_board(None), "txt", nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),),
                        capacitance=True, solve_points=3, max_solves=6, cavity_max_nodes=12000)
    res = simulate(board, sc.config)
    return board, res.routes[0], res.issues


@pytest.fixture(scope="module")
def converter(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("txtc")
    board, sc, _ = make(tmp, discrete_buck_board(), "buck", nets=(), solve_points=1)
    sc.add_detected_converters([d.spec for d in detect_converters(board, sc.config)])
    sc.save()
    return board, simulate(board, SettingsFile.load(sc.path).config).converters[0]


def _drawn_ticklabels(ax, renderer):
    """The tick labels matplotlib draws (inside the view, switched on, not empty) per axis."""
    out = {}
    for name, axis in (("x", ax.xaxis), ("y", ax.yaxis)):
        labels = []
        for tick in axis._update_ticks():                   # the ticks inside the view (matplotlib 3.11)
            for lab in (tick.label1, tick.label2):
                if lab.get_visible() and lab.get_text().strip():
                    labels.append(lab)
        out[name] = labels
    return out


def problems(fig) -> list[str]:
    """Every text under 8 pt, outside the figure or overlapping another text group or another plot."""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    W, H = fig.bbox.width, fig.bbox.height
    found = []
    inside_legend = {id(t) for leg in fig.legends + [ax.get_legend() for ax in fig.axes if ax.get_legend()]
                     for t in leg.get_texts()}
    groups = []                                             # (owner axes or None, name, bbox)
    ticks = {}
    for ax in fig.axes:
        for name, labels in _drawn_ticklabels(ax, r).items():
            ticks.update({id(t): t for t in labels})
            boxes = [(t.get_window_extent(r), t.get_text()) for t in labels]
            for i, (a, ta) in enumerate(boxes):              # one axis' labels: none on another
                for b, tb in boxes[i + 1:]:
                    if min(a.x1, b.x1) - max(a.x0, b.x0) > 1 and min(a.y1, b.y1) - max(a.y0, b.y0) > 1:
                        found.append(f"tick labels {ta!r} and {tb!r} overlap")
            if labels:
                from matplotlib.transforms import Bbox
                groups.append((ax, f"{name} tick labels", Bbox.union([t.get_window_extent(r) for t in labels]),
                               False))
    for t in fig.findobj(Text):
        if not t.get_visible() or not t.get_text().strip():
            continue
        is_tick = id(t) in ticks
        if not is_tick and any(t is lab for ax in fig.axes for axis in (ax.xaxis, ax.yaxis)
                               for tk in axis.get_major_ticks() + axis.get_minor_ticks()
                               for lab in (tk.label1, tk.label2)):
            continue                                        # a tick label outside the view: not drawn
        what = repr(t.get_text()[:40])
        if t.get_fontsize() < FS_MARK:
            found.append(f"{what} is {t.get_fontsize()} pt")
        bb = t.get_window_extent(r)
        if bb.x0 < -1 or bb.y0 < -1 or bb.x1 > W + 1 or bb.y1 > H + 1:
            found.append(f"{what} leaves the figure: {bb.bounds} of {W:.0f} x {H:.0f}")
        if is_tick or id(t) in inside_legend:
            continue
        owner = next((ax for ax in fig.axes if t in (ax.title, ax._left_title, ax._right_title, ax.xaxis.label,
                                                     ax.yaxis.label) or t in ax.texts), None)
        on_map = owner is not None and t in owner.texts and hasattr(fig, "_gt_maps") and owner in fig._gt_maps
        groups.append((owner, what, bb, on_map))
    for leg in fig.legends:
        groups.append((None, "figure legend", leg.get_window_extent(r), False))
    for ax in fig.axes:
        if ax.get_legend() is not None:
            groups.append((ax, "legend", ax.get_legend().get_window_extent(r), False))

    def overlap(a, b):
        return min(a.x1, b.x1) - max(a.x0, b.x0) > 1 and min(a.y1, b.y1) - max(a.y0, b.y0) > 1
    for i, (oa, na, a, ma) in enumerate(groups):
        for ob, nb, b, mb in groups[i + 1:]:
            # labels at vias and pins on the same map may cover each other where they crowd (the wheel zoom
            # spreads them): that is the board's density, not the layout
            if overlap(a, b) and not (ma and mb and oa is ob):
                found.append(f"{na} overlaps {nb}")
        for ax in fig.axes:                                 # another plot's area (a colour bar is a plot too)
            if ax is not oa and ax.get_visible() and ax.axison and overlap(a, ax.get_window_extent(r)):
                found.append(f"{na} runs into a plot at {tuple(round(v) for v in ax.get_window_extent(r).bounds)}")
    return found


def render(draw, size, *args, **kw):
    fig = Figure(figsize=size, dpi=100)
    FigureCanvasAgg(fig)
    draw(fig, *args, **kw)
    return fig


@pytest.mark.parametrize("sizes", PLUGIN + REPORT)
def test_route_figures_text_is_readable_and_fits(route, sizes):
    board, rr, issues = route
    maps, section, spectrum = sizes
    section = (section[0], section[1] or section_height_in(board, rr))
    for fig in (render(draw_heatmap, maps, board, rr, issues),
                render(draw_heatmap, maps, board, rr, issues, layer="In1.Cu", scale="linear"),
                render(draw_section, section, board, rr),
                render(draw_spectrum, spectrum, rr)):
        assert not problems(fig), (fig.get_size_inches(), problems(fig))


def test_power_stage_and_thermal_figures_text_is_readable_and_fits(converter):
    board, c = converter
    for size in ((19.1, 4.8), (13.0, 3.2), (8, 5.5)):
        fig = render(draw_heatmap, size, board, c)
        assert not problems(fig), (size, problems(fig))
    # no cross-section in these views: the spectrum has the plugin's lower row to itself
    for size in ((19.1, 2.15), (13.0, 1.45), (9, 3.4)):
        fig = render(draw_spectrum, size, c)
        assert not problems(fig), (size, problems(fig))
    parts = [{"ref": f"U{k}", "t_junction": 60.0 - 3 * k, "t_c": 50.0, "power_w": 0.5} for k in range(8)]
    th = NS(kind="thermal", parts=parts, warn_c=50.0, max_c=60.0, ambient_c=25.0)
    for size in ((19.1, 2.15), (13.0, 1.45), (5.0, 1.45), (8, 3.2)):
        fig = render(draw_spectrum, size, th)
        assert not problems(fig), (size, problems(fig))


def test_the_section_gives_up_its_axis_label_then_its_share_plot_when_short(route):
    """At the plugin's height (section_height_in: four layers, a plane displacement line in the title): the share
    plot over the side view, at least 0.25 in per layer, and the distance axis labelled. Short: the label moves
    into the title, then the share plot goes (the side view's colours show the same shares)."""
    board, rr, _ = route
    h = section_height_in(board, rr)
    assert 3.0 < h < 3.8
    tall = render(draw_section, (19.1, h), board, rr)
    top, side = tall.axes[0], tall.axes[1]
    assert len(tall._gt_section) == 2 and side.get_xlabel().startswith("mm along the route")
    assert "displacement" in tall._suptitle.get_text()
    assert side.get_position().height * h / 4 >= 0.25 and top.get_position().height * h >= 0.8
    short = render(draw_section, (11.5, 1.3), board, rr)
    assert len(short._gt_section) == 1                       # the side view and the colour bar
    assert "x: mm along the route" in short._suptitle.get_text()
    assert not problems(short)
    assert section_height_in(board, NS(section={}, kind="converter")) == 0.0


def test_the_maps_height_follows_maps_limited_by_the_width(route):
    """heatmap_height_in: the demo's two 40 x 20 mm maps side by side on a 19.1 in wide page need about 5.5 in;
    drawn at that height they fill their cells (no band above or below: the aspect ratio shrinks nothing), drawn
    taller they would leave bands. A page lower than that keeps its height; no width yet: no answer."""
    board, rr, issues = route
    h = heatmap_height_in(rr, 19.1, 10.0)
    assert 4.5 < h < 6.5
    for height, fills in ((h, True), (10.0, False)):
        fig = render(draw_heatmap, (19.1, height), board, rr, issues)
        fig.canvas.draw()
        ax = next(a for a in fig.axes if a in fig._gt_maps)
        shrunk = ax.get_position().height / ax.get_position(original=True).height
        assert (shrunk > 0.97) == fills, (height, shrunk)
        assert not problems(fig)
    assert heatmap_height_in(rr, 19.1, 3.0) == 3.0 and heatmap_height_in(rr, 0.0, 5.0) == 5.0


def test_frequencies_carry_their_unit_and_the_legend_hides_no_data(route):
    """The spectrum's frequency labels read '100 MHz', '1 GHz' (no '10²' and no axis label); with room the
    legend sits on the title's line, above the plot."""
    _, rr, _ = route
    fig = render(draw_spectrum, (7.6, 2.15), rr)
    ax = fig.axes[0]
    labels = [t.get_text() for t in _drawn_ticklabels(ax, fig.canvas.get_renderer())["x"]]
    assert labels and all(l.endswith(("MHz", "GHz")) for l in labels) and not ax.get_xlabel()
    leg = ax.get_legend().get_window_extent(fig.canvas.get_renderer())
    assert leg.y0 >= ax.get_window_extent().y1 - 1
    assert ax.get_legend().get_texts()[0].get_fontsize() == FS_TICK
    assert ax._left_title.get_fontsize() == FS_TITLE


def test_vias_shown_marks_the_busiest_and_keeps_the_parts():
    """"Vias shown": a map marks at most that many vias, the ones carrying most of the current; parts (■) are
    always marked. The user saw 4 of 12 vias on a DC path when the results kept only the five busiest."""
    from matplotlib.lines import Line2D
    from groundtruth.report.plots import _carriers
    ts = [{"kind": "via", "name": f"v{k}", "layers": ["F.Cu", "B.Cu"], "x": float(k), "y": 0.0,
           "fraction": (k + 1) / 100} for k in range(12)]
    ts.append({"kind": "part", "name": "C1", "layers": ["F.Cu"], "x": 20.0, "y": 0.0, "fraction": 0.5})
    rr = NS(transfers=ts)
    board = NS(vias=[])

    def marked(max_vias):
        fig = Figure()
        ax = fig.add_subplot(111)
        _carriers(ax, board, rr, "F.Cu", max_vias)
        pts = {m: [] for m in ("^", "s")}
        for line in ax.get_lines():
            if isinstance(line, Line2D) and line.get_marker() in pts:
                pts[line.get_marker()] += list(line.get_xdata())
        return sorted(pts["^"]), pts["s"]
    vias, parts = marked(None)
    assert len(vias) == 12 and parts == [20.0]
    vias, parts = marked(5)
    assert vias == [7.0, 8.0, 9.0, 10.0, 11.0] and parts == [20.0]
    vias, parts = marked(0)                                       # "Via markers" unticked: the parts stay
    assert vias == [] and parts == [20.0]


def test_the_reports_maps_have_no_labels():
    """The user: "remove the labels from the report. they are actually not helping" (on a power stage's map the
    shares, pin letters and switch names piled up over the switches). labels=False (the report) keeps the markers
    and the pads' coloured outlines and drops every text box; the plugin's maps keep them."""
    from groundtruth.report.plots import _carriers, _place_labels, _switch_pins
    ts = [{"kind": "via", "name": "v", "layers": ["F.Cu", "B.Cu"], "x": 1.0, "y": 0.0, "fraction": 0.6},
          {"kind": "part", "name": "C1", "layers": ["F.Cu"], "x": 20.0, "y": 0.0, "fraction": 0.4}]
    pins = [{"ref": "Q1", "role": role, "layers": ["F.Cu"], "x": x, "y": 5.0, "w": 1.0, "h": 1.0}
            for role, x in (("D", 0.0), ("S", 3.0))]
    rr = NS(transfers=ts, switch_pins=pins)

    def drawn(labels):
        fig = Figure()
        ax = fig.add_subplot(111)
        _switch_pins(ax, rr, "F.Cu", labels)
        _carriers(ax, NS(vias=[]), rr, "F.Cu", None, labels)
        _place_labels(ax)                       # the shares are written once the view is final (draw_heatmap)
        marks = [l for l in ax.get_lines() if l.get_marker() in ("^", "s")]
        outlines = [l for l in ax.get_lines() if len(l.get_xdata()) == 5]
        return [t.get_text() for t in ax.texts], marks, outlines, ax.get_legend_handles_labels()[1]
    texts, marks, outlines, legend = drawn(True)
    assert {"60 %", "40 %", "D", "S", "Q1"} <= set(texts) and legend == ["D = drain", "S = source"]
    texts, marks2, outlines2, legend = drawn(False)
    assert texts == [] and len(marks2) == len(marks) == 2 and len(outlines2) == len(outlines) > 0
    assert legend == ["drain", "source"]


def test_hovering_a_marker_says_what_it_is(route):
    """The user: "it would be good if there is a hover over message saying what the issue is when hovering over a
    circle". Over an issue's circle the plugin shows its severity, check and message; over a via's marker its
    share of the current (an issue first where both are in reach); elsewhere nothing (the map's value instead)."""
    import copy
    from groundtruth.report.plots import mark_at
    board, rr, issues = route
    rr = copy.copy(rr)
    rr.transfers = rr.transfers + [{"kind": "via", "name": "via GND @(30.00, 10.00)", "layers": list(rr.layer_grids),
                                    "x": 30.0, "y": 10.0, "fraction": 0.4, "distance_mm": 0.0}]
    fig = Figure(figsize=(9, 6), dpi=100)
    FigureCanvasAgg(fig)
    draw_heatmap(fig, board, rr, issues)
    fig.canvas.draw()
    marks = [(ax, m) for ax, ms in fig._gt_marks.items() for m in ms]
    issue = next((ax, m) for ax, m in marks if m[2] == 0)
    via = next((ax, m) for ax, m in marks if m[2] == 1)
    for ax, (x, y, _, text) in (issue, via):
        px, py = ax.transData.transform((x, y))
        assert mark_at(fig, ax, px + 3, py - 3) == text
    assert via[1][3] == "via GND @(30.00, 10.00): 40 % of the current"
    ax, (x, y, _, text) = issue
    worst = next(i for i in issues if i.location == (x, y))
    assert text.startswith(f"{worst.severity} · {worst.check}: ")
    px, py = ax.transData.transform((x, y))
    assert mark_at(fig, ax, px + 60, py + 60) == ""
    # what a click on a marker refers to (the plugin shows it in KiCad): the issue itself, the via's transfer
    from groundtruth.report.plots import mark_ref_at
    kind, what, txt = mark_ref_at(fig, ax, px + 3, py - 3)
    assert (kind, what, txt) == ("issue", worst, text)
    vax, (vx, vy, _, vtext) = via
    vpx, vpy = vax.transData.transform((vx, vy))
    kind, what, txt = mark_ref_at(fig, vax, vpx, vpy)
    assert kind == "via" and (what["x"], what["y"]) == (30.0, 10.0) and txt == vtext
    assert mark_ref_at(fig, ax, px + 60, py + 60) is None


def test_part_outlines_are_read_and_drawn(route, tmp_path):
    """Part outlines (Results tab): a footprint's courtyard is read from the board file (else its fab outline);
    over the maps every part gets one, a part without either a box around its pads."""
    from matplotlib.collections import LineCollection
    from groundtruth.adapters.file_source import loads_board
    board, rr, issues = route
    assert all(not fp.outline for fp in board.footprints)          # the generated parts have no graphics
    fig = Figure(figsize=(9, 6), dpi=100)
    FigureCanvasAgg(fig)
    draw_heatmap(fig, board, rr, issues)                            # the line collections a map has anyway
    segs_without = sum(len(c.get_segments()) for c in next(iter(fig._gt_marks)).collections
                       if isinstance(c, LineCollection))
    fig = Figure(figsize=(9, 6), dpi=100)
    FigureCanvasAgg(fig)
    draw_heatmap(fig, board, rr, issues, outlines=True, names=True)
    ax = next(iter(fig._gt_marks))
    segs = sum(len(c.get_segments()) for c in ax.collections if isinstance(c, LineCollection))
    assert segs >= len([fp for fp in board.footprints if fp.pads])
    refs = {t.get_text() for t in ax.texts}
    assert {"U1", "U2"} <= refs
    # outlines and names are separate choices (the user: "a separate enable option for the part names")
    for outlines, names in ((True, False), (False, True)):
        fig = Figure(figsize=(9, 6), dpi=100)
        FigureCanvasAgg(fig)
        draw_heatmap(fig, board, rr, issues, outlines=outlines, names=names)
        ax = next(iter(fig._gt_marks))
        drawn = sum(len(c.get_segments()) for c in ax.collections if isinstance(c, LineCollection)) - segs_without
        assert ("U1" in {t.get_text() for t in ax.texts}) == names
        assert (drawn > 0) == outlines
    src = Path(board.source).read_text(encoding="utf-8")
    k = src.index('(footprint "') + len('(footprint "')
    k = src.index("(pad", k)
    src = src[:k] + '(fp_rect (start -2 -1.5) (end 2 1.5) (layer "F.CrtYd") (width 0.05)) ' + src[k:]
    b2 = loads_board(src)
    fp = next(f for f in b2.footprints if f.outline)
    xs = [p[0] for p in fp.outline[0]]
    assert len(fp.outline[0]) == 5 and max(xs) - min(xs) == pytest.approx(4.0 if fp.angle % 180 == 0 else 3.0)


def test_the_usual_characters_are_measured_in_a_worker_before_the_page_needs_them():
    """The plugin's Results page measures its titles on the GUI thread (heatmap_height_in); the first measurement
    looks matplotlib's font up, which stalled the window ~300 ms on kraft_flux_mini_2 after a Simulate. warm_text
    (submitted to the page's render worker when the tab is built) measures them beforehand, the same widths."""
    from groundtruth.report import plots
    plots._ADVANCE.clear()
    plots.warm_text()
    assert {(c, b) for c in plots.WARM_CHARS for b in (False, True)} <= set(plots._ADVANCE)
    text = "SPI_SCK  U1102.12→U1103.6  return current @ 140 MHz (▲ via / ■ part, µΩ ≈ 2×)"
    warm = plots._width_in(text, 10)
    plots._ADVANCE.clear()
    assert plots._width_in(text, 10) == warm > 0


def test_each_view_of_a_power_stage_marks_only_its_own_issues():
    """A DC path's map marks that path's current-density and via issues only; the hot-loop view marks the stage's
    own issues, none of its DC paths' (the output path's overloaded via showed on the switch-node map)."""
    from types import SimpleNamespace as NS
    from groundtruth.report.plots import view_issues
    issue = lambda key, net="buck conv1": NS(net=net, key=key)                    # noqa: E731
    loop = issue("power:buck conv1:loop")
    out_dc = issue("power:buck conv1:dc:output L301 → J104.2")
    out_via = issue("power:buck conv1:via:output L301 → J104.2")
    sw_dc = issue("power:buck conv1:dc:switch node → L301")
    other = issue("power:buck conv2:loop", net="buck conv2")
    issues = [loop, out_dc, out_via, sw_dc, other]
    assert view_issues(NS(net="buck conv1", view_of=None), issues) == [loop]
    assert view_issues(NS(net="buck conv1", view_of="switch node → L301"), issues) == [sw_dc]
    assert view_issues(NS(net="buck conv1", view_of="output L301 → J104.2"), issues) == [out_dc, out_via]
    assert view_issues(NS(net="SPI_SCK"), [issue("route:SPI_SCK:x", "SPI_SCK")])[0].net == "SPI_SCK"


def test_close_vias_get_their_shares_apart_the_busiest_ringed_and_their_amperes_on_hover():
    """The user, of a DC path's map: three vias a few tenths of a millimetre apart had their shares written over
    each other ("58 %8 %"), so the busiest could not be read. Each share now gets a spot of its own (no two label
    boxes overlap), the busiest via has a white ring, and a marker's hover text gives its current in amperes where
    the view knows it ("…: 12 % of the current, 1.2 A")."""
    from groundtruth.report.plots import _carriers, _place_labels, carrier_text
    ts = [{"kind": "via", "name": f"via GND @({x:.2f}, 75.70)", "layers": ["F.Cu"], "x": x, "y": 75.7,
           "fraction": f, "current_a": f * 10.0} for x, f in ((136.9, 0.07), (137.55, 0.08), (138.25, 0.12),
                                                            (138.6, 0.58))]
    rr = NS(transfers=ts)
    fig = Figure(figsize=(6, 3), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    _carriers(ax, NS(vias=[]), rr, "F.Cu")
    ax.set_xlim(135.5, 140.0)
    ax.set_ylim(77.5, 74.5)
    ax.set_aspect("equal")
    _place_labels(ax)
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    boxes = [t.get_bbox_patch().get_window_extent(r) for t in ax.texts]
    assert len(boxes) == 4
    for i in range(4):
        for j in range(i + 1, 4):
            assert not boxes[i].overlaps(boxes[j]), (ax.texts[i].get_text(), ax.texts[j].get_text())
    rings = [l for l in ax.get_lines() if l.get_marker() == "o" and l.get_markersize() == 19]
    assert rings and all(list(l.get_xdata()) == [138.6] for l in rings)
    assert carrier_text(ts[2]) == "via GND @(138.25, 75.70): 12 % of the current, 1.2 A"
    assert carrier_text({**ts[2], "rms": True}).endswith("1.2 A RMS")
    assert carrier_text({k: v for k, v in ts[2].items() if k != "current_a"}).endswith("12 % of the current")
