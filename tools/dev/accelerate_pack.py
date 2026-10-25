"""A folder to run Apple Accelerate's sparse solvers on a Mac against SuperLU, on the matrices Simulate factorises
(docs/SPEED.md, M3): the Mac needs only Xcode's command line tools (clang), no Python packages for Accelerate.

    .venv/Scripts/python tools/dev/accelerate_pack.py OUT_DIR MATRIX.npz [MATRIX.npz ...]

Writes OUT_DIR/<name>.bin per matrix (export_schur.py's .npz: its upper triangle as complex symmetric CSC, the
format tools/dev/accelerate_bench.c reads), the C source, superlu_bin.py (SuperLU on the same files, as peec calls
it, when the Mac's python3 has scipy) and run.sh: on the Mac, `sh run.sh` compiles the bench, runs it with 1 and
4 threads for each LDLᵀ variant and writes results.txt.
"""
import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
import scipy.sparse as sp

RUN_SH = """#!/bin/sh
# Apple Accelerate's sparse LDLT against SuperLU on the matrices in this folder (GroundTruth, docs/SPEED.md M3).
# Needs Xcode's command line tools (xcode-select --install) and macOS 15.5 or later.
cd "$(dirname "$0")"
sw_vers > results.txt
sysctl -n machdep.cpu.brand_string >> results.txt
xcrun clang -O2 -o accelerate_bench accelerate_bench.c -framework Accelerate 2>> results.txt || {
    echo "compile failed: see results.txt"; exit 1; }
for m in *.bin; do
    for t in 1 4; do
        VECLIB_MAXIMUM_THREADS=$t ./accelerate_bench "$m" LDLT LDLTSBK LDLTTPP LDLTUnpivoted >> results.txt 2>&1
    done
done
if python3 -c "import scipy" 2>/dev/null; then
    for m in *.bin; do python3 superlu_bin.py "$m" >> results.txt 2>&1; done
else
    echo "no python3 with scipy: SuperLU not measured here" >> results.txt
fi
echo "done: results.txt"
"""

SUPERLU_BIN = '''"""SuperLU on one of this folder's .bin matrices, as GroundTruth's solver calls it (the Mac's reference)."""
import json
import sys
import time

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu

with open(sys.argv[1], "rb") as f:
    n, nnz = np.fromfile(f, np.int64, 2)
    cs = np.fromfile(f, np.int64, n + 1)
    ri = np.fromfile(f, np.int32, nnz)
    v = np.fromfile(f, np.complex128, nnz)
U = sp.csc_matrix((v, ri, cs), shape=(n, n))
S = (U + sp.triu(U, 1).T).tocsc()
rng = np.random.default_rng(0)
b = rng.standard_normal(n) + 1j * rng.standard_normal(n)
t0 = time.perf_counter()
lu = splu(S, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.1, options={"SymmetricMode": True})
t1 = time.perf_counter()
best = 1e30
for _ in range(3):
    a = time.perf_counter()
    x = lu.solve(b)
    best = min(best, time.perf_counter() - a)
print(json.dumps({"matrix": sys.argv[1], "type": "SuperLU", "factor_s": round(t1 - t0, 4), "solve1_s": round(best, 5),
                  "fill_M": round((lu.L.nnz + lu.U.nnz) / 1e6, 2),
                  "residual": float(np.linalg.norm(S @ x - b) / np.linalg.norm(b))}))
'''


def main() -> int:
    """The command line (see the module)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("matrices", nargs="+")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for m in args.matrices:
        S = sp.load_npz(m).tocsc()
        U = sp.triu(S, format="csc")
        U.sort_indices()
        target = out / (Path(m).stem + ".bin")
        with open(target, "wb") as f:
            np.array([U.shape[0], U.nnz], np.int64).tofile(f)
            U.indptr.astype(np.int64).tofile(f)
            U.indices.astype(np.int32).tofile(f)
            U.data.astype(np.complex128).tofile(f)
        print(f"{target.name}: n {U.shape[0]}, upper nnz {U.nnz}, {target.stat().st_size / 1e6:.0f} MB")
    shutil.copy(Path(__file__).with_name("accelerate_bench.c"), out / "accelerate_bench.c")
    (out / "superlu_bin.py").write_text(SUPERLU_BIN, encoding="utf-8")
    (out / "run.sh").write_bytes(RUN_SH.encode("utf-8"))          # LF line ends: sh on the Mac
    print(f"{out}: copy this folder to the Mac and run `sh run.sh` there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
