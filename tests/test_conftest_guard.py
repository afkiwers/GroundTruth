"""The tests keep conftest.py's protections for their whole run.

A test and conftest's autouse fixtures share one `monkeypatch`, so a test's `monkeypatch.undo()` also undoes
conftest's patches: the private LOCALAPPDATA (the user's real ui.json would be written) and the GPU kept off in
`analysis.gpu: auto` (gpu._speedup). In 2026-11 that made the grouped-memory test in test_solver.py time the
laptop's GPU for real, pick it, and cache the choice for the rest of the process: its own grouped run and every
later test's Simulate ran on the GPU (a carrier 7.96 instead of 7.10 mm away; test_stats.py counting no CPU
time split). A test undoes its own patches with `with monkeypatch.context() as m:` instead."""
import re
from pathlib import Path


def test_no_test_undoes_conftests_patches():
    """No test file calls monkeypatch.undo() (in code: comments may name it)."""
    here = Path(__file__).parent
    hits = [f"{p.name}:{n}" for p in sorted(here.glob("*.py")) if p.name != Path(__file__).name
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"\bmonkeypatch\.undo\(\)", line.split("#")[0])]
    assert not hits, f"monkeypatch.undo() also undoes conftest's patches; use monkeypatch.context(): {hits}"
