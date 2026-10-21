"""Issue rings on the maps grouped where they would overlap (report/plots.cluster_issues), after the user's "the heatmap
is cluttered with circles. we need to find a better way" (a thermal map with a ring per hot part): one ring per group
in its worst issue's colour with the count beside it, the hover listing the group, a click showing its worst issue;
zooming in (a smaller view) parts the groups again."""
from types import SimpleNamespace

import numpy as np

from groundtruth.core.issues import Issue
from groundtruth.report import plots


def _issue(sev, x, y, k, check="part_temperature"):
    """An issue at (x, y) mm."""
    return Issue(check, sev, "thermal", f"part {k}", (x, y), key=str(k))


def test_issues_within_the_radius_are_one_group_led_by_the_worst():
    """Three warnings and an error within 1 mm and one issue 10 mm away: two groups; the first is led by the error
    (its place, colour and click) and holds the three warnings; the far one stands alone; order-stable."""
    near = [_issue("warning", 0.0, 0.0, 1), _issue("warning", 0.5, 0.0, 2), _issue("error", 0.3, 0.4, 3),
            _issue("warning", 0.0, 0.8, 4)]
    far = _issue("info", 10.0, 0.0, 5, check="esd_protection")
    groups = plots.cluster_issues(near + [far], 1.0)
    assert [len(g) for g in groups] == [4, 1] and groups[0][0].key == "3" and groups[1] == [far]
    assert plots.cluster_issues(near + [far], 1.0) == groups
    assert [len(g) for g in plots.cluster_issues(near + [far], 0.1)] == [1, 1, 1, 1, 1]     # zoomed in: apart
    text = plots.cluster_text(groups[0])
    assert text.startswith("4 issues here (1 error, 3 warnings); the worst: error · part_temperature: part 3")
    assert "part_temperature ×3" in text
    assert plots.cluster_text(groups[1]) == "info · esd_protection: part 5"


def test_a_crowded_map_draws_one_ring_per_group_and_its_hover_and_click_match():
    """A map with 30 issues packed in 4 mm: few rings, each group's count written beside it, the hover (_gt_marks)
    and the click (_gt_mark_refs) one entry per ring, in the same order; a click gives the group's worst issue."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from boardgen import microstrip_board
    from conftest import make
    from groundtruth.analysis.simulate import simulate
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        board, sc, _ = make(Path(tmp), microstrip_board(), "m")
        rr = simulate(board, sc.config).routes[0]
    rng = np.random.default_rng(1)
    x0, x1, y0, y1 = rr.layer_grids[rr.main_layers()[0]].extent
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    issues = [Issue("emi_margin", "error" if k == 7 else "warning", rr.net, f"m {k}",
                    (cx + rng.uniform(-2, 2), cy + rng.uniform(-2, 2)), key=str(k)) for k in range(30)]
    fig = Figure(figsize=(9.5, 6.0), dpi=60)
    FigureCanvasAgg(fig)
    plots.draw_heatmap(fig, board, rr, issues, labels=False)
    ax = next(iter(fig._gt_marks))
    marks, refs = fig._gt_marks[ax], fig._gt_mark_refs[ax]
    issue_marks = [m for m in marks if m[2] == 0]
    assert 1 <= len(issue_marks) < 30 and len(marks) == len(refs)
    groups = plots.cluster_issues(issues, plots.ISSUE_CLUSTER * max(abs(x1 - x0), abs(y1 - y0)))
    assert len(issue_marks) == len(groups)
    assert refs[0] == ("issue", issues[7])                  # the error leads its group and is what a click shows
    counts = [t.get_text() for t in ax.texts if t.get_text().isdigit()]
    assert sorted(int(c) for c in counts) == sorted(len(g) for g in groups if len(g) > 1)
