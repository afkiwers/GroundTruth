"""The route model's filament chain (analysis/simulate._chain): a route through a pour of its own net, and what
happens where the model's grid has no copper of that pour."""


def test_a_pour_too_narrow_for_the_grid_is_modelled_along_the_route():
    """A route through a pour of its net whose copper the model has no node of where the route enters it (a neck
    narrower than the grid, the user's phase-voltage path to its second receiver, 2026-11-03): that stretch runs
    along the route's own steps, with a note on the route; before, the route failed ("no result")."""
    from types import SimpleNamespace as NS
    from groundtruth.analysis.route import Route, RouteSegment
    from groundtruth.analysis.simulate import _chain
    grid = NS(x0=0.0, y0=0.0, nets=["PH"], xy=lambda i, j: (i * 1.0, j * 1.0))
    st = NS(grids=[grid], names=["F.Cu"], z=[0.0], d=1.0, node=lambda *a, **k: None)
    pad = lambda x: NS(layers=["F.Cu"], position=(x, 5.0))                        # noqa: E731
    route = Route("PH", pad(0.0), pad(10.0), steps=[
        RouteSegment((0.0, 5.0), (4.0, 5.0), "F.Cu", 0.2, net="PH"),
        RouteSegment((4.0, 5.0), (8.0, 5.0), "F.Cu", 2.0, kind="zone", net="PH"),
        RouteSegment((8.0, 5.0), (10.0, 5.0), "F.Cu", 0.2, net="PH")])
    chain, sections = _chain(route, st, (0, 0, 0), (0, 10, 0), {"F.Cu": 0.0}, pours=True)
    assert sections == [] and len(chain) >= 3                                    # filaments, no pour stretch
    assert any("too narrow for the model's grid" in n for n in route.notes)
