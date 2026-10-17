"""tools/install_gpu.py: which PyTorch build it installs for which card (NVIDIA CUDA, AMD ROCm by chip), without
installing anything (--dry-run, the card names and the driver stubbed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import install_gpu  # noqa: E402


def test_radeon_names_give_their_chip():
    """Windows' adapter names -> the chip AMD builds PyTorch for; a Radeon 840M (two possible chips) and unknown
    cards give no chip, but the card's name for the message."""
    chip = install_gpu.amd_chip
    assert chip(["Intel(R) UHD Graphics 620", "AMD Radeon(TM) 860M Graphics"]) == ("gfx1152",
                                                                                   "AMD Radeon(TM) 860M Graphics")
    assert chip(["AMD Radeon 890M Graphics"])[0] == "gfx1150"
    assert chip(["AMD Radeon(TM) 8060S Graphics"])[0] == "gfx1151"
    assert chip(["AMD Radeon 780M Graphics"])[0] == "gfx1103"
    assert chip(["AMD Radeon RX 7900 XTX"])[0] == "gfx1100" and chip(["AMD Radeon RX 7800 XT"])[0] == "gfx1101"
    assert chip(["AMD Radeon RX 9070 XT"])[0] == "gfx1201"
    assert chip(["AMD Radeon(TM) 840M Graphics"]) == ("", "AMD Radeon(TM) 840M Graphics")
    assert chip(["NVIDIA GeForce RTX 3050 Laptop GPU"]) == ("", "")
    assert chip([]) == ("", "")


def test_the_amd_build_comes_from_amds_index_alone():
    """AMD: torch with the chip's device extra at the pinned version, every package from AMD's index (no PyPI next
    to it); NVIDIA: PyTorch's CUDA index as before."""
    py = Path("C:/kicad/python.exe")
    assert install_gpu.install_command(py, amd="gfx1152") == [
        str(py), "-m", "pip", "install", "--index-url", "https://repo.amd.com/rocm/whl-multi-arch/",
        "torch[device-gfx1152]==2.12.0+rocm7.14.1"]
    cmd = install_gpu.install_command(py, cuda="cu126")
    assert cmd[-2:] == ["--index-url", "https://download.pytorch.org/whl/cu126"] and "--extra-index-url" not in cmd


@pytest.fixture
def dry(monkeypatch, tmp_path, capsys):
    """Run main() with --dry-run on a stand-in Python: `cards` (Windows' adapters) and `cuda` (nvidia-smi's
    version) are set per call; returns (exit code, output)."""
    py = tmp_path / "python.exe"
    py.write_text("")

    def run(*args, cards=(), cuda=""):
        monkeypatch.setattr(install_gpu, "video_cards", lambda: list(cards))
        monkeypatch.setattr(install_gpu, "driver_cuda", lambda: cuda)
        monkeypatch.setattr(sys, "argv", ["install_gpu.py", "--python", str(py), "--dry-run", *args])
        rc = install_gpu.main()
        out = capsys.readouterr()
        return rc, out.out + out.err
    return run


def test_the_build_is_chosen_from_the_card(dry):
    """No option: NVIDIA when nvidia-smi is there, else an AMD card Windows names, else CUDA 12.6 as before;
    --amd takes the chip from the name or as given; a chip that isn't a gfx name, or --amd on a card whose chip is
    not known, stops before pip."""
    rc, out = dry(cards=["AMD Radeon(TM) 860M Graphics"])
    assert rc == 0 and "AMD card: AMD Radeon(TM) 860M Graphics (chip gfx1152)" in out
    assert "torch[device-gfx1152]==2.12.0+rocm7.14.1" in out
    rc, out = dry(cards=["AMD Radeon(TM) 860M Graphics"], cuda="12.6")      # an NVIDIA driver too: NVIDIA
    assert rc == 0 and "whl/cu126" in out and "device-gfx" not in out
    rc, out = dry(cards=["Intel(R) UHD Graphics 620"])                      # neither: CUDA as before
    assert rc == 0 and "whl/cu126" in out
    rc, out = dry("--amd", "gfx1153", cards=["AMD Radeon(TM) 840M Graphics"])
    assert rc == 0 and "torch[device-gfx1153]" in out
    rc, out = dry("--amd", "--amd-torch", "2.11.0+rocm7.14.1", cards=["AMD Radeon 890M Graphics"])
    assert rc == 0 and "torch[device-gfx1150]==2.11.0+rocm7.14.1" in out
    rc, out = dry("--amd", cards=["AMD Radeon(TM) 840M Graphics"])
    assert rc == 1 and "--amd gfx1152" in out and "would run" not in out
    rc, out = dry("--amd", "gfx1152; rm", cards=[])
    assert rc == 1 and "not a chip name" in out
