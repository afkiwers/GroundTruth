"""Could this machine's GPU speed up the field solver? (development aid; needs pyopencl, not a project dependency)

    .venv/Scripts/python -m pip install pyopencl
    .venv/Scripts/python tools/dev/gpu_probe.py [--layers 6] [--size 231x264]

Times the L_p product's layer-kernel multiply-add (PlaneStack._conv, out[t] = Σ_s K[t,s]·F[s] per frequency pixel)
in double precision on every OpenCL device, with and without copying F to the device and the result back, against
numpy and the compiled CPU kernel (solver/_mac.py). The multiply-add is the most GPU-friendly part of a GMRES
iteration (regular, no FFT library needed); the FFTs and the Gram-Schmidt vectors are similar memory-bound work.
The preconditioner (a sparse LU factorisation and triangular solves) is not GPU work. Default sizes: the fine
model of refboard's SPI_SCK on its 6-layer board.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

KERNEL = """
#pragma OPENCL EXTENSION cl_khr_fp64 : enable
__kernel void mac(__global const double *Kp, __global const double2 *F, __global double2 *out,
                  const int N, const int P) {
    const int p = get_global_id(0);
    if (p >= P) return;
    for (int t = 0; t < N; t++) {
        double2 acc = (double2)(0.0, 0.0);
        for (int s = 0; s < N; s++)
            acc += Kp[(p * N + t) * N + s] * F[s * P + p];
        out[t * P + p] = acc;
    }
}
"""


def best(fn, n=20):
    """The mean time of fn over n calls, after one warm-up call (compilation, first transfers)."""
    fn()
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n


def main() -> int:
    """The command line (see the module): random layer kernels and fields of the given size, then the multiply-add
    timed with numpy, the compiled CPU kernel and on each OpenCL device."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layers", type=int, default=6)
    ap.add_argument("--size", default="231x264", help="padded FFT grid P0xP1")
    args = ap.parse_args()
    N = args.layers
    P0, P1 = (int(v) for v in args.size.split("x"))
    P = P0 * P1
    rng = np.random.default_rng(0)
    K = rng.standard_normal((N, N, P0, P1))
    K = (K + K.transpose(1, 0, 2, 3)) / 2                   # symmetric in the layers, like the real kernels
    F = rng.standard_normal((N, P0, P1)) + 1j * rng.standard_normal((N, P0, P1))
    ref = np.einsum("tsij,sij->tij", K, F)

    def numpy_mac():
        """out[t] = sum over s of K[t, s] * F[s] with numpy, in place (no temporaries per term)."""
        out = np.empty_like(F)
        tmp = np.empty_like(F[0])
        for t in range(N):
            np.multiply(K[t, 0], F[0], out=out[t])
            for s in range(1, N):
                np.multiply(K[t, s], F[s], out=tmp)
                out[t] += tmp
        return out
    print(f"{N} layers, {P0} x {P1} pixels ({F.nbytes / 1e6:.1f} MB of currents per product)")
    print(f"  CPU numpy:            {best(numpy_mac) * 1e3:7.2f} ms")
    try:
        from groundtruth.solver._mac import kernel_mac
        Kp = np.ascontiguousarray(K.transpose(2, 3, 0, 1))
        out = np.empty_like(F)
        print(f"  CPU compiled (numba): {best(lambda: kernel_mac(Kp, F, out)) * 1e3:7.2f} ms")
    except ImportError:
        print("  CPU compiled: numba not installed")
    try:
        import pyopencl as cl
    except ImportError:
        print("pyopencl is not installed: .venv/Scripts/python -m pip install pyopencl")
        return 1
    Kd = np.ascontiguousarray(K.transpose(2, 3, 0, 1)).reshape(-1)        # pixel-major
    Fd = np.ascontiguousarray(F.reshape(N, P))
    for platform in cl.get_platforms():
        for dev in platform.get_devices():
            name = f"{dev.name} ({cl.device_type.to_string(dev.type)}, {dev.max_compute_units} units)"
            if "cl_khr_fp64" not in dev.extensions:
                print(f"  {name}: no double precision, skipped (single precision stalls GMRES, see peec.py)")
                continue
            ctx = cl.Context([dev])
            q = cl.CommandQueue(ctx)
            prog = cl.Program(ctx, KERNEL).build()
            mf = cl.mem_flags
            k_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=Kd)
            f_buf = cl.Buffer(ctx, mf.READ_WRITE, Fd.nbytes)
            o_buf = cl.Buffer(ctx, mf.READ_WRITE, Fd.nbytes)
            res = np.empty_like(Fd)
            mac = prog.mac

            def run(copy: bool):
                """One multiply-add on the device, with or without the copies to and from it; waits for it."""
                if copy:
                    cl.enqueue_copy(q, f_buf, Fd)
                mac(q, (P,), None, k_buf, f_buf, o_buf, np.int32(N), np.int32(P))
                if copy:
                    cl.enqueue_copy(q, res, o_buf)
                q.finish()
            cl.enqueue_copy(q, f_buf, Fd)
            run(True)
            err = np.abs(res.reshape(N, P0, P1) - ref).max() / np.abs(ref).max()
            print(f"  {name}:\n    kernel only {best(lambda: run(False)) * 1e3:7.2f} ms, with the copies "
                  f"{best(lambda: run(True)) * 1e3:7.2f} ms (error {err:.1e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
