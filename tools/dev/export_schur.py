"""Save the matrices the preconditioner factorises, for benchmarking other sparse solvers (docs/SPEED.md, M3).

    .venv/Scripts/python tools/dev/export_schur.py BOARD SETTINGS --only "conv1" OUT_DIR
        [--resolution 8] [--models board-wide,conducted,DC] [--f 100e6]

Builds the chosen nets' / power stages' models exactly as Simulate does (tools/dev/solver_bench.board_models) and
writes, per model, the Schur complement S = Arᵀ·D⁻¹·Ar that PlaneStack._factorise hands to SuperLU at the run's
middle planned frequency (or --f): `<name>_<kind>_<MHz>.npz` (scipy.sparse.save_npz, complex, CSC) and a `.json`
beside it (kind, preconditioner, nodes, unknowns, frequency, nonzeros). For a derived stack (a converter's LISN on
the board-wide model) its base's matrix is written: the derived one is a low-rank update of it. Nothing is
factorised here.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def schur(st, f: float):
    """The Schur complement PlaneStack._factorise factorises for stack `st` at frequency f (CSC)."""
    w, R_strip, z_tail = st._impedances(f)
    Dinv = st._preconditioner_admittance(w, R_strip, z_tail)
    return (st.ArT @ Dinv @ st.Ar).tocsc()


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("settings")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--only", required=True, help="comma-separated parts of net / power stage names")
    ap.add_argument("--resolution", type=float, help="analysis.resolution (the plugin's Resolution)")
    ap.add_argument("--models", help="comma-separated model kinds (simulate.run_kind; default: all)")
    ap.add_argument("--f", type=float, help="frequency (Hz) instead of each run's middle planned one")
    args = ap.parse_args()
    import scipy.sparse as sp
    from solver_bench import board_models
    args.out_dir.mkdir(parents=True, exist_ok=True)
    kinds = set(args.models.split(",")) if args.models else None
    done = set()
    for name, kind, st, f, _ in board_models(args.board, args.settings, args.only, args.resolution, kinds):
        st = st._base if st._base is not None else st       # a derived stack: its base's matrix (see the module)
        f = args.f or f
        if (id(st), f) in done:
            continue
        done.add((id(st), f))
        t = time.perf_counter()
        S = schur(st, f)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{name}_{kind}_{f / 1e6:g}MHz")
        sp.save_npz(args.out_dir / f"{stem}.npz", S)
        meta = {"name": name, "kind": kind, "precond": st.precond_mode, "f": f, "nodes": int(st.n_nodes),
                "unknowns": int(st.nb + st.Ar.shape[1]), "layers": int(st.N), "grid": [int(st.nx), int(st.ny)],
                "pitch_mm": float(st.d * 1e3), "shape": list(S.shape), "nnz": int(S.nnz)}
        (args.out_dir / f"{stem}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        print(f"{stem}: {S.shape[0]} x {S.shape[1]}, {S.nnz} nonzeros ({time.perf_counter() - t:.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
