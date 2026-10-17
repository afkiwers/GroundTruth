"""A solve that does not converge must not pass silently: the refinement closes in on it from below, and the band
above the last solved frequency (where the loop moment is only held) is reported as a band_unsolved warning instead
of a margin. Seen on kraft_flux_mini_2 at 4x resolution: the board-wide solve at 1.58 GHz failed, refinement stopped
at 260 MHz and a +17.5 dB margin at 780 MHz was reported, where the solved 1x model had +3 dB."""
from boardgen import layer_change_board
from conftest import make
from groundtruth.analysis.simulate import ModelRun, simulate, unsolved_band
from groundtruth.config.settings import NetSpec
from groundtruth.solver import peec


def test_the_band_above_the_last_solve_is_named():
    run = ModelRun(None, None, {1e6: object(), 3e8: object(), 1.5e9: None})
    assert unsolved_band(run, 1e9) == {"from": 3e8, "failed": 1.5e9}
    assert unsolved_band(run, 2.9e8) is None                       # no limit above the last solve: nothing missed
    assert unsolved_band(ModelRun(None, None, {1e6: object(), 3e8: None, 9e8: object()}), 1e9) is None


def test_a_failing_solve_is_approached_from_below_and_reported(tmp_path, monkeypatch):
    """Every solve above 300 MHz fails (the route model is the spectrum model here: no plane capacitance)."""
    board, sc, _ = make(tmp_path, layer_change_board(None), "fail",
                        nets=(NetSpec("SIG", "U1.1", ["U2.1"], 50e6, 1e-9),), solve_points=4, max_solves=10)
    solve = peec.RouteExcitation.solve
    cut = 300e6

    def failing(self, f, guess=None):
        if f > cut:
            raise peec.SolverError(f"GMRES did not converge at {f:.4g} Hz (416 iterations)")
        return solve(self, f, guess)

    monkeypatch.setattr(peec.RouteExcitation, "solve", failing)
    res = simulate(board, sc.config)
    r = res.routes[0]
    solved = [row["f"] for row in r.solves]
    assert max(solved) <= cut
    assert r.unsolved is not None and r.unsolved["from"] == max(solved) and r.unsolved["failed"] > cut
    harmonics = [h for h in (50e6 * k for k in range(1, 200, 2)) if max(solved) < h < r.unsolved["failed"]]
    assert len(harmonics) <= 1 or max(solved) > 150e6                  # closed in on the failure from below
    assert any(i.check == "band_unsolved" and i.net == "SIG" for i in res.issues)
