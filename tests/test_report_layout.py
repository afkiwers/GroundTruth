"""The HTML report's summary and layout (report/html.py, report/plots.figure_png), after the user's "Have a look at the
html report, it's content and format and see if you can improve it": the main findings one line per check (before,
eight copies of one clearance error hid the converters' failing margins), the overshoot judged as the hot_loop check
judges it (before, a green L·di/dt beside a 40 V ringing the check called an error), the victims' units, and the
maps' title moved down to the maps (before, an empty band up to half the image)."""
from types import SimpleNamespace

import numpy as np
import pytest

from groundtruth.core.issues import Issue
from groundtruth.report import html
from groundtruth.report.plots import _close_title_gap


def _issue(check, sev, net, k, **details):
    """An issue of `check` at `sev` on `net`, its own place, with `details`."""
    return Issue(check, sev, net, f"{net}: {check} {k}", (float(k), 0.0), key=str(k), details=details)


def test_findings_are_one_line_per_check_most_severe_first():
    """Three clearance errors and a warning, two emission errors (the worse margin shown), an info: errors first, the
    more frequent first among them; each line counts its severities and links to the check's rows."""
    live = ([_issue("clearance_voltage", "error", "+VDC", k) for k in range(3)]
            + [_issue("clearance_voltage", "warning", "+VDC", 3)]
            + [_issue("emi_margin", "error", "buck conv1", 4, margin_db=-15.2),
               _issue("emi_margin", "error", "buck conv2", 5, margin_db=-19.6),
               _issue("esd_protection", "info", "J101", 6)])
    out = html._findings_by_check(live)
    order = [c for c in ("clearance_voltage", "emi_margin", "esd_protection") if f"data-show-check='{c}'" in out]
    assert order == ["clearance_voltage", "emi_margin", "esd_protection"]
    assert out.index("data-show-check='clearance_voltage'") < out.index("data-show-check='emi_margin'")
    assert "3 errors, 1 warning</a>" in out and "2 errors</a>" in out and "1 info</a>" in out
    assert "emi_margin 5" in out and "emi_margin 4" not in out        # the lower margin (-19.6 dB) is the worst
    assert html._findings_by_check([]) == "<p class='empty'>No findings.</p>"


def test_the_overshoot_is_the_largest_estimate_judged_by_the_rules():
    """L·di/dt 7.5 V, ringing 40.2 V on 55 V: the ringing, 73 % of V_in, an error at the rules' 50 %; a warning at
    25-50 %; without v_in no level."""
    rules = SimpleNamespace(overshoot_warning=0.25, overshoot_error=0.5)
    c = SimpleNamespace(overshoot_v=7.5, ringing={"overshoot_v": 40.2}, recovery=None, v_in=55.0)
    name, over, parts, level = html._overshoot(c, rules)
    assert (name, over, level) == ("ringing", 40.2, "error") and parts[0] == ("L·di/dt", 7.5)
    c.ringing = {"overshoot_v": 20.0}
    assert html._overshoot(c, rules)[3] == "warn"
    c.v_in = None
    assert html._overshoot(c, rules)[3] == ""


def test_victims_show_their_units_and_the_peak_over_the_threshold():
    """A net's noise in V, an injected current in A; at or above the threshold the peak is red."""
    net = {"kind": "net", "peak": 0.0022, "limit": 0.3, "name": "/NRST", "to": "U1.4", "why": "reset",
           "from": "J1.3", "leg": "buck", "m_nh": 4.4e-4}
    row = html._victim_row(net)
    assert "2.2mV" in row and "300mV" in row and " bad" not in row
    inj = {"kind": "c", "peak": 0.5, "limit": 0.1, "name": "X", "why": "y", "leg": "buck", "c_pf": 1.0}
    row = html._victim_row(inj, lambda v: "injected")
    assert "500mA" in row and "num bad" in row


def test_the_title_moves_down_to_the_maps():
    """Three tall maps in a wide figure sit low; the title at the figure's top is moved to just above them (the
    margin 0.12 in); an ordinary figure (its axes up to the usual gap under the title) is left alone, and a figure
    legend above the axes stays below the title."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    fig = Figure(figsize=(9.5, 7.8), dpi=80)
    FigureCanvasAgg(fig)
    for k in range(3):
        ax = fig.add_subplot(1, 3, k + 1)
        ax.imshow(np.zeros((40, 20)))
        ax.set_title("F.Cu")
    fig.suptitle("a title", y=0.99, va="top")
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    top = max(fig.transFigure.inverted().transform(ax.get_tightbbox(r))[1][1] for ax in fig.axes)
    before = fig._suptitle.get_position()[1]
    _close_title_gap(fig)
    low = fig.transFigure.inverted().transform(fig._suptitle.get_window_extent(r))[0][1]
    assert fig._suptitle.get_position()[1] < before - 0.1
    assert low - top == pytest.approx(0.12 / 7.8, abs=0.005)
    fig2 = Figure(figsize=(6, 4), dpi=80)
    FigureCanvasAgg(fig2)
    fig2.add_subplot(1, 1, 1).plot([0, 1])
    fig2.suptitle("t", y=0.99, va="top")
    _close_title_gap(fig2)
    assert fig2._suptitle.get_position()[1] == pytest.approx(0.99)
    # the three maps again, with a legend over them: the title stops above the legend
    fig3 = Figure(figsize=(9.5, 7.8), dpi=80)
    FigureCanvasAgg(fig3)
    for k in range(3):
        fig3.add_subplot(1, 3, k + 1).imshow(np.zeros((40, 20)), label="x")
    line = fig3.axes[0].plot([0, 1], label="a line")[0]
    leg = fig3.legend([line], ["a line"], loc="lower center", bbox_to_anchor=(0.5, 0.86))
    fig3.suptitle("a title", y=0.99, va="top")
    fig3.canvas.draw()
    _close_title_gap(fig3)
    r = fig3.canvas.get_renderer()
    inv = fig3.transFigure.inverted()
    assert inv.transform(fig3._suptitle.get_window_extent(r))[0][1] >= inv.transform(leg.get_window_extent(r))[1][1]
