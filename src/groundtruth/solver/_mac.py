"""The layer-kernel multiply-add of the L_p product (PlaneStack._conv) as one compiled loop (numba).

out[t] = Σ_s K[t, s] · F[s] for every frequency pixel: N² array multiplies and adds in numpy, each a pass over
memory, single-threaded. Fused per pixel it reads K and F once: kraft_flux_mini_2's 6-layer route model
(231 x 264 pixels) 9.4 ms -> 3.3 ms per product, bit-identical. Serial on purpose: the solver already runs
solves in parallel threads, and numba's default thread pool aborts when two threads launch parallel loops at
once. nogil, so those threads really run side by side.

The kernels come as one quadrant of their spectra (PlaneStack._kernels): a spectrum is even in both axes,
K[i, j] = K[P0 - i, j] = K[i, P1 - j], so the loop mirrors the pixel index. Every (t, s) pair is stored, not only
the N(N+1)/2 distinct ones: looking the pair up in the inner loop made it 10-35 % slower; this way it runs as fast
as with full kernels (6 layers, 462 x 528: 16.1 ms against 15.2).

Imported only when numba is installed (the plugin's requirements have it, a plain CLI install may not):
peec.py falls back to numpy."""
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
