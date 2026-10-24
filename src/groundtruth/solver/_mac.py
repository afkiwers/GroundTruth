"""The layer-kernel multiply-add of the L_p product (PlaneStack._conv) as one compiled loop (numba).

out[t] = Σ_s K[t, s] · F[s] for every frequency pixel: N² array multiplies and adds in numpy, each a pass over
memory, single-threaded. Fused per pixel it reads K and F once: refboard's 6-layer route model
(231 x 264 pixels) 9.4 ms -> 3.3 ms per product, bit-identical. Serial on purpose: the solver already runs
solves in parallel threads, and numba's default thread pool aborts when two threads launch parallel loops at
once. nogil, so those threads really run side by side.

The kernels come as one quadrant of their spectra (PlaneStack._kernels): a spectrum is even in both axes,
K[i, j] = K[P0 - i, j] = K[i, P1 - j], so the loop mirrors the pixel index. Every (t, s) pair is stored, not only
the N(N+1)/2 distinct ones: looking the pair up in the inner loop made it 10-35 % slower; this way it runs as fast
as with full kernels (6 layers, 462 x 528: 16.1 ms against 15.2).

Imported only when numba is installed (the plugin's requirements have it, a plain CLI install may not):
peec.py falls back to numpy."""
import numpy as np
from numba import njit


@njit(cache=True, nogil=True)
def kernel_mac(Kq, F, out):   # pragma: no cover - compiled
    """Kq: (P0 // 2 + 1, P1 // 2 + 1, N, N) real kernel quadrants; F, out: (N, P0, P1) complex."""
    n, p0, p1 = F.shape
    for i in range(p0):
        # the stored quadrant's row for pixel row i: the spectrum is even, so row i equals row p0 - i
        qi = i if i <= p0 - i else p0 - i
        for j in range(p1):
            qj = j if j <= p1 - j else p1 - j
            for t in range(n):
                acc = 0j
                for s in range(n):
                    acc += Kq[qi, qj, t, s] * F[s, i, j]
                out[t, i, j] = acc
    return out


# --------------------------------------------------------------------------- Gram-Schmidt on a single-precision basis
# GMRES's Krylov basis stored as two float32 planes, real and imaginary (peec._Basis32Vectors), read here with the
# arithmetic in float64: half the memory of a complex128 basis and half the bytes each Gram-Schmidt pass reads, the
# products as accurate as before on the stored vectors (mixed-precision GMRES [Lindquist 2020] [Aliaga 2023]).
# Planes, not complex64: LLVM vectorises the separate real loops (interleaved complex ran 3x slower than BLAS);
# in column blocks so the vector's block stays in cache while every basis row passes over it. fastmath only for
# reassociation and fused multiply-adds (the reductions vectorise); the order is fixed by the compiled code, so the
# same inputs give the same bits on the same machine.
GS_BLOCK = 8192
_FAST = {"reassoc", "contract"}


@njit(cache=True, nogil=True, fastmath=_FAST)
def _dot2(vr, vi, wr, wi):   # pragma: no cover - compiled
    """(Re, Im) of conj(v)·w for one basis row's block (vr, vi float32) and the vector's (wr, wi float64)."""
    a = 0.0
    b = 0.0
    for k in range(vr.shape[0]):
        x = np.float64(vr[k])
        y = np.float64(vi[k])
        a += x * wr[k] + y * wi[k]
        b += x * wi[k] - y * wr[k]
    return a, b


@njit(cache=True, nogil=True, fastmath=_FAST)
def _axpy2(hr, hi, vr, vi, wr, wi, sign):   # pragma: no cover - compiled
    """w += sign·h·v over one basis row's block (h complex as hr, hi; v float32 planes; w float64 planes)."""
    for k in range(vr.shape[0]):
        x = np.float64(vr[k])
        y = np.float64(vi[k])
        wr[k] += sign * (hr * x - hi * y)
        wi[k] += sign * (hr * y + hi * x)


@njit(cache=True, nogil=True)
def gs_project(Vr, Vi, m, wr, wi, hr, hi):   # pragma: no cover - compiled
    """One classical Gram-Schmidt pass over the first m basis rows: h = Vᴴ·w, then w -= V·h (w in place)."""
    n = wr.shape[0]
    for i in range(m):
        hr[i] = 0.0
        hi[i] = 0.0
    for k0 in range(0, n, GS_BLOCK):
        k1 = min(k0 + GS_BLOCK, n)
        for i in range(m):
            a, b = _dot2(Vr[i, k0:k1], Vi[i, k0:k1], wr[k0:k1], wi[k0:k1])
            hr[i] += a
            hi[i] += b
    for k0 in range(0, n, GS_BLOCK):
        k1 = min(k0 + GS_BLOCK, n)
        for i in range(m):
            _axpy2(hr[i], hi[i], Vr[i, k0:k1], Vi[i, k0:k1], wr[k0:k1], wi[k0:k1], -1.0)


@njit(cache=True, nogil=True)
def gs_combine(Vr, Vi, k, yr, yi, xr, xi):   # pragma: no cover - compiled
    """x = Σ_i y[i]·V[i] over the first k basis rows (x zeroed first)."""
    n = xr.shape[0]
    xr[:] = 0.0
    xi[:] = 0.0
    for k0 in range(0, n, GS_BLOCK):
        k1 = min(k0 + GS_BLOCK, n)
        for i in range(k):
            _axpy2(yr[i], yi[i], Vr[i, k0:k1], Vi[i, k0:k1], xr[k0:k1], xi[k0:k1], 1.0)
