"""Heat transfer coefficients from standard correlations [Incropera 2011].

All functions take SI units and temperatures in °C unless noted. Source keys in square brackets: docs/REFERENCES.md.
"""

from __future__ import annotations


from ..materials import STEFAN_BOLTZMANN

G = 9.81
MIN_DT = 0.5  # K, keeps natural-convection coefficients finite near ambient


def air(t_film_c: float):
    """Dry air at 1 atm: (k, nu, Pr, beta)."""
    # viscosity from Sutherland's law [Sutherland 1893] (air's constants [White 2006]), density from the ideal gas
    # law, conductivity as a power law in T [A28]; beta = 1/T (ideal gas)
    T = t_film_c + 273.15
    mu = 1.716e-5 * (T / 273.15) ** 1.5 * (273.15 + 110.4) / (T + 110.4)  # Sutherland
    rho = 101325.0 / (287.05 * T)
    k = 0.0241 * (T / 273.15) ** 0.81
    cp = 1006.0
    return k, mu / rho, mu * cp / k, 1.0 / T


def _rayleigh(dT, L, t_film):
    """Rayleigh number Ra = g beta dT L^3 / (nu alpha) (alpha = nu / Pr), with air's k and Pr. dT is at least
    MIN_DT, so h stays finite (not zero) for a surface at ambient."""
    k, nu, pr, beta = air(t_film)
    ra = G * beta * max(abs(dT), MIN_DT) * L ** 3 / (nu * nu / pr)
    return ra, k, pr


def h_horizontal(dT: float, L: float, t_film: float, upper: bool) -> float:
    """Horizontal plate, L = area/perimeter. ``upper`` = surface faces up."""
    # the classic correlations [Incropera 2011]: Nu = 0.54 Ra^1/4 (laminar) or 0.15 Ra^1/3 (turbulent) where a plume
    # rises off the face, Nu = 0.52 Ra^1/5 where the face blocks it (hot face down)
    ra, k, _ = _rayleigh(dT, L, t_film)
    hot_up = (dT > 0) == upper  # hot face up or cold face down: buoyant plume forms
    if hot_up:
        nu = 0.54 * ra ** 0.25 if ra < 1e7 else 0.15 * ra ** (1 / 3)
    else:
        nu = 0.52 * ra ** 0.2
    return max(nu, 1.0) * k / L


def h_vertical(dT: float, H: float, t_film: float) -> float:
    """Vertical plate of height H (Churchill & Chu, full range [Churchill 1975])."""
    ra, k, pr = _rayleigh(dT, H, t_film)
    nu = (0.825 + 0.387 * ra ** (1 / 6) / (1 + (0.492 / pr) ** (9 / 16)) ** (8 / 27)) ** 2
    return nu * k / H


def h_forced(v: float, L: float, t_film: float) -> float:
    """Flat plate in parallel flow, average over length L (laminar / mixed)."""
    # Nu = 0.664 Re^1/2 Pr^1/3 for a laminar boundary layer; above the transition Reynolds number 5e5 the
    # mixed laminar-turbulent average (0.037 Re^4/5 - 871) Pr^1/3 [Incropera 2011]
    k, nu, pr, _ = air(t_film)
    re = max(v, 1e-6) * L / nu
    if re < 5e5:
        n = 0.664 * re ** 0.5 * pr ** (1 / 3)
    else:
        n = (0.037 * re ** 0.8 - 871) * pr ** (1 / 3)
    return n * k / L


def h_radiation(t_c, t_surr_c, eps):
    """Linearised radiation coefficient (W/m²K); works with numpy arrays."""
    # eps sigma (T^4 - Ts^4) = h_rad (T - Ts) with h_rad = eps sigma (T^2 + Ts^2)(T + Ts) [Incropera 2011]: exact, but
    # h_rad depends on T, so the coupled loop updates it each iteration
    T = t_c + 273.15
    Ts = t_surr_c + 273.15
    return eps * STEFAN_BOLTZMANN * (T * T + Ts * Ts) * (T + Ts)


def board_h(env, side: str, t_surf: float, t_air: float, length: float, width: float,
            natural_only: bool = False) -> float:
    """Convection coefficient for one face of a board. length/width in m."""
    if env.convection == "fixed" and not natural_only:
        return env.h_top if side == "top" else env.h_bottom
    dT = t_surf - t_air
    tf = 0.5 * (t_surf + t_air)
    if env.orientation == "vertical":
        hn = h_vertical(dT, width, tf)
    else:                       # characteristic length of a horizontal plate: area / perimeter
        L = (length * width) / (2 * (length + width))
        hn = h_horizontal(dT, L, tf, upper=(side == "top"))
    if env.convection == "forced" and not natural_only:
        hf = h_forced(env.airspeed, length, tf)
        return (hf ** 3 + hn ** 3) ** (1 / 3)  # Churchill mixed-convection blend [Churchill 1977]
    return hn


def box_outer_h(env, t_surf: float, t_amb: float, L: float, W: float, H: float) -> float:
    """Area-averaged convection coefficient for the outside of a box (m)."""
    dT = t_surf - t_amb
    tf = 0.5 * (t_surf + t_amb)
    if env.convection == "forced":
        return h_forced(env.airspeed, L, tf)
    if env.convection == "fixed":
        return env.h_top
    a_top = L * W
    a_side = 2 * (L + W) * H
    lc = a_top / (2 * (L + W))
    h_top = h_horizontal(dT, lc, tf, upper=True)
    h_bot = h_horizontal(dT, lc, tf, upper=False)
    h_side = h_vertical(dT, H, tf)
    return (a_top * (h_top + h_bot) + a_side * h_side) / (2 * a_top + a_side)
