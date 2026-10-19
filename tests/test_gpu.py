"""The optional GPU backend (solver/gpu.py), run with PyTorch on the CPU (GROUNDTRUTH_GPU_DEVICE=cpu): the same code
a CUDA/ROCm device runs, so it is tested on machines without a GPU. The device results must equal the numpy ones
to the GMRES tolerance. Skipped where PyTorch is not installed (it is an optional extra)."""
import numpy as np
import pytest
from shapely.geometry import box

from boardgen import layer_change_board
from conftest import make
from groundtruth import build_version
from groundtruth.analysis.simulate import simulate
from groundtruth.analysis.suggest import suggest_fixes
from groundtruth.cli import EXIT_CONFIG, main
from groundtruth.config.settings import SettingsFile
from groundtruth.solver import gpu
from groundtruth.solver.peec import PlaneLayer, PlaneStack, _HostMath, build_grid
from test_speed import microstrip

pytest.importorskip("torch")


@pytest.fixture
def torch_cpu(monkeypatch):
    monkeypatch.setenv(gpu.ENV_DEVICE, "cpu")


def test_device_products_match_numpy(torch_cpu):
    """L_p, the system product and the preconditioner on the device equal numpy's (3 layers, both orientations)."""
    g = build_grid(box(0, 0, 12, 9), (0, 0, 12, 9), 0.5)
    st = PlaneStack([PlaneLayer(f"L{k}", z, g) for k, z in enumerate((0.0, 0.2e-3, 0.7e-3))])
    op = st.operator(100e6)
    rng = np.random.default_rng(4)
    n = st.nb + st.Ar.shape[1]
    x = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    host = _HostMath(st)
    with gpu.use("on") as run:
        assert run.label == "cpu (PyTorch)"
        dev = gpu.TorchMath(run, st)
        for name, a, b in (("matvec", dev.host(dev.matvec(op, dev.dev(x))), host.matvec(op, x)),
                           ("precond", dev.host(dev.precond(op, dev.dev(x))), host.precond(op, x))):
            assert np.abs(a - b).max() <= 1e-12 * np.abs(b).max(), name


def test_simulate_on_the_device_matches_the_cpu(tmp_path, torch_cpu):
    """The layer-change board with the board-wide model (per-position layer blocks in the preconditioner): the
    same margins, inductance and solved harmonics on the device as on the CPU, and the run says where it ran."""
    board, sc, _ = make(tmp_path, layer_change_board(None), "dev", capacitance=True, solve_points=3, max_solves=6,
                        cavity_max_nodes=12000)
    cfg = sc.config
    cfg.analysis.gpu = "off"
    cpu = simulate(board, cfg)
    cfg.analysis.gpu = "on"
    dev = simulate(board, cfg)
    assert cpu.solver["device"] == "CPU" and dev.solver == {"device": "cpu (PyTorch)", "gpu_fallbacks": 0,
                                                            "gpu_error": "", "gpu_note": "",
                                                            "build": build_version()}
    a, b = cpu.routes[0], dev.routes[0]
    assert [s["f"] for s in a.cavity_solves] == [s["f"] for s in b.cavity_solves]
    assert b.worst["margin_db"] == pytest.approx(a.worst["margin_db"], abs=1e-3)
    assert b.l_ratio == pytest.approx(a.l_ratio, rel=1e-5)
    assert [i.fingerprint for i in dev.issues] == [i.fingerprint for i in cpu.issues]


def test_fix_search_on_the_device(tmp_path, torch_cpu):
    """Candidates are low-rank updates of the shared factorisations (derived stacks with more branches): the device
    keeps their own incidence matrices and shares the kernels, and picks the same fix with the same prediction."""
    board, sc, _ = make(tmp_path, layer_change_board(None), "devfix", solve_points=1)
    cfg = sc.config
    cfg.analysis.gpu = "off"
    cpu = suggest_fixes(board, cfg)
    cfg.analysis.gpu = "on"
    dev = suggest_fixes(board, cfg)
    assert dev.solver["device"] == "cpu (PyTorch)" and dev.solver["gpu_fallbacks"] == 0
    pick = lambda r: [(s.modification.key(), round(s.delta_l, 4)) for s in r.selected]     # noqa: E731
    assert pick(dev) == pick(cpu) and pick(cpu)


def test_a_solve_the_device_cannot_finish_is_done_on_the_cpu(torch_cpu, monkeypatch):
    def out_of_memory(*a, **k):
        raise RuntimeError("CUDA out of memory. Tried to allocate 400.00 MiB")
    monkeypatch.setattr(gpu.TorchMath, "matvec", out_of_memory)
    ref = microstrip().solve(100e6)
    with gpu.use("on") as run:
        sol = microstrip().solve(100e6)
    assert sol.Z_loop == pytest.approx(ref.Z_loop, rel=1e-9)
    assert run.fallbacks == 1 and "out of memory" in run.error
    assert "1 solve(s) done on the CPU instead" in gpu.describe(run.summary())


def test_modes_without_a_gpu(monkeypatch, tmp_path, capsys):
    """This machine's PyTorch has no CUDA: auto stays on the CPU, on refuses with a clear message (also in the
    CLI), off never looks; the setting accepts YAML's on/off booleans."""
    monkeypatch.delenv(gpu.ENV_DEVICE, raising=False)
    import torch
    if torch.cuda.is_available():
        pytest.skip("this machine has a CUDA GPU")
    assert gpu.probe("auto") is None and gpu.probe("off") is None
    with pytest.raises(gpu.GpuUnavailable, match="finds no CUDA/ROCm GPU"):
        gpu.probe("on")
    with pytest.raises(ValueError):
        gpu.probe("fast")
    board, sc, path = make(tmp_path, layer_change_board(None), "modes", gpu=True)
    assert sc.config.analysis.gpu == "on"
    text = (tmp_path / "groundtruth" / "modes.groundtruth.yaml").read_text(encoding="utf-8").replace("gpu: true", "gpu: maybe")
    (tmp_path / "bad.groundtruth.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(Exception, match="analysis.gpu must be auto, on or off"):
        SettingsFile.load(tmp_path / "bad.groundtruth.yaml").config
    assert main(["simulate", str(path), "--gpu", "on", "-q"]) == EXIT_CONFIG
    assert "analysis.gpu is on, but" in capsys.readouterr().err


def test_auto_declines_a_device_slower_than_the_cpu_and_says_why(monkeypatch):
    """auto times the solver's convolution on the device against the CPU (_speedup) and keeps to the CPU below
    GPU_MIN_SPEEDUP, with the measured ratio in the reason (an RTX 3050 Laptop GPU: 0.8x); at or above it the
    device is used; `on` (untimed) uses it either way."""
    monkeypatch.setattr(gpu, "_speedup", lambda torch, dev: 0.5)
    found, why = gpu._find("cpu", timed=True)
    assert found is None and "0.5x the CPU's speed" in why and "analysis.gpu: on" in why
    assert gpu._find("cpu", timed=False)[0] is not None
    monkeypatch.setattr(gpu, "_speedup", lambda torch, dev: gpu.GPU_MIN_SPEEDUP)
    assert gpu._find("cpu", timed=True)[0] is not None


def test_one_process_at_a_time_on_a_gpu(monkeypatch):
    """A GPU held by another run (its lock file taken: here a second handle, as another process would hold it):
    auto runs on the CPU and says why, without running anything on the GPU (not even the speed check: two processes
    timing it at once stalled each other), and gets the GPU again once it is released; the lock is released when the
    run ends."""
    import torch
    fake = gpu.Device(torch, torch.device("cuda"), "Test GPU")
    monkeypatch.setattr(gpu, "_candidate", lambda: fake)
    probed = []
    monkeypatch.setattr(gpu, "probe", lambda mode: probed.append(mode) or fake)
    other = gpu._DeviceLock(fake)
    assert other.acquire(wait=False)
    try:
        with gpu.use("auto") as run:
            assert run.device is None and "Test GPU is in use by another process" in run.note
            assert "in use by another process" in gpu.describe(run.summary())
        assert probed == []                      # nothing ran on the busy GPU, not even the speed check
    finally:
        other.release()
    with gpu.use("auto") as run:
        assert run.device is fake and run.lock is not None
        held = gpu._DeviceLock(fake)
        assert not held.acquire(wait=False)              # this run holds it
    assert held.acquire(wait=False)                      # released at the end of the run
    held.release()


def test_a_model_the_device_cannot_hold_is_solved_on_the_cpu(tmp_path, torch_cpu, monkeypatch):
    """A device whose free memory is too small for the model (device_bytes against DEVICE_MEMORY_SHARE of it):
    every solve is done on the CPU before anything is uploaded, the run counts them and says why, and the results are
    the CPU's. The estimate covers what a solve does upload (the kernels and incidence matrices)."""
    board, sc, _ = make(tmp_path, layer_change_board(None), "small", solve_points=3, max_solves=4)
    cfg = sc.config
    cfg.analysis.gpu = "off"
    cpu = simulate(board, cfg)
    monkeypatch.setattr(gpu, "_free_bytes", lambda dev: 1000)
    cfg.analysis.gpu = "on"
    dev = simulate(board, cfg)
    assert dev.solver["gpu_fallbacks"] > 0 and "DeviceTooSmall" in dev.solver["gpu_error"]
    assert "needs" in dev.solver["gpu_error"]
    assert dev.routes[0].worst["margin_db"] == pytest.approx(cpu.routes[0].worst["margin_db"], abs=1e-9)
    monkeypatch.setattr(gpu, "_free_bytes", lambda dev: None)
    g = build_grid(box(0, 0, 12, 9), (0, 0, 12, 9), 0.5)
    st = PlaneStack([PlaneLayer(f"L{k}", z, g) for k, z in enumerate((0.0, 0.2e-3, 0.7e-3))])
    with gpu.use("on") as run:
        data = gpu.TorchMath(run, st).data
    uploaded = sum(o.K.numel() * 8 + (o.into.numel() + o.outof.numel()) * 8 for o in data["orients"])
    assert gpu.device_bytes(st) > uploaded
