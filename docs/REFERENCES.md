# References

Every formula, method and theorem GroundTruth calculates with, and where it comes from. The code cites the same keys
at the place it uses them, in square brackets: `[Grover 1946]` in a docstring or comment means "this line applies
that source". A key with a location narrows it down: `[Jackson 1999, §5.6]`.

Rules this list follows:
- A source is cited only for what it contains. A section or equation number is given only where it was checked;
  otherwise the citation names the source alone.
- What has no published source behind it (a modelling choice, a fitted constant, a default) is an **assumption**:
  it has an id `[A1]`, `[A2]`… in the assumptions table below, with the reason and, where one exists, the evidence
  (a test, a measurement in ARCHITECTURE.md or VALIDATION_AUDIT.md).
- Standards are cited by edition. Where a newer edition exists and the code follows the older one, the entry says so.

## Sources

### Electromagnetics and partial inductance

| Key | Source | Used for |
|---|---|---|
| Ruehli 1972 | A. E. Ruehli, "Inductance calculations in a complex integrated circuit environment," *IBM J. Res. Develop.*, vol. 16, no. 5, pp. 470–481, 1972. | Partial inductance: each conductor piece's inductance with the return left open, loops as sums of partials. |
| Ruehli 1974 | A. E. Ruehli, "Equivalent circuit models for three-dimensional multiconductor systems," *IEEE Trans. Microw. Theory Tech.*, vol. 22, no. 3, pp. 216–221, 1974. | The PEEC method: conductors as cells with partial inductances, capacitances and resistances, solved as a circuit. |
| Paul 2010 | C. R. Paul, *Inductance: Loop and Partial*. Hoboken, NJ: Wiley–IEEE Press, 2010. | Partial self and mutual inductance of filaments, wires and bars; Neumann's integral; loop inductance from partials. |
| Grover 1946 | F. W. Grover, *Inductance Calculations: Working Formulas and Tables*. New York: Van Nostrand, 1946 (Dover reprint). | Mutual inductance of parallel filaments in closed form; the geometric mean distance (GMD) method, a rectangle's GMD ≈ 0.2235 (a + b), a thin tube's GMD = its radius. |
| Jackson 1999 | J. D. Jackson, *Classical Electrodynamics*, 3rd ed. New York: Wiley, 1999. | The magnetic dipole moment of a current distribution, m = ½ ∫ r × J dV [§5.6]. |
| Ramo 1994 | S. Ramo, J. R. Whinnery, T. Van Duzer, *Fields and Waves in Communication Electronics*, 3rd ed. New York: Wiley, 1994. | Skin depth δ = 1/√(π f μ0 σ) and the surface resistance of a good conductor. |
| Pozar 2012 | D. M. Pozar, *Microwave Engineering*, 4th ed. Hoboken, NJ: Wiley, 2012. | Dielectric loss tangent (ε = ε′(1 − j tan δ)); parallel-plate capacitance; cavity resonances and their Q (conductor and dielectric loss add as 1/Q). |
| Paul 2008 | C. R. Paul, *Analysis of Multiconductor Transmission Lines*, 2nd ed. Hoboken, NJ: Wiley–IEEE Press, 2008. | Per-unit-length L and C of a cross-section; L = μ0 ε0 C0⁻¹ for a homogeneous-equivalent TEM line; Z0, εeff; differential and common modes of a pair. |
| Johnson 1993 | H. W. Johnson, M. Graham, *High-Speed Digital Design: A Handbook of Black Magic*. Englewood Cliffs, NJ: Prentice Hall, 1993. | Near- and far-end crosstalk of coupled lines, the near-end saturating at 2 T_d ≥ t_r. |
| Hall 2000 | S. H. Hall, G. W. Hall, J. A. McCall, *High-Speed Digital System Design*. New York: Wiley, 2000. | Backward and forward crosstalk coefficients K_b = (L_m/L + C_m/C)/4, K_f = (L_m/L − C_m/C)/2. |
| CODATA 2018 | E. Tiesinga, P. J. Mohr, D. B. Newell, B. N. Taylor, "CODATA recommended values of the fundamental physical constants: 2018," *Rev. Mod. Phys.*, vol. 93, 025010, 2021. | ε0 = 8.8541878128 × 10⁻¹² F/m (μ0 is taken as 4π × 10⁻⁷ H/m, 5.5 × 10⁻¹⁰ relative from the 2018 value 1.25663706212 × 10⁻⁶ H/m). |
| IEC 60028 | IEC 60028:1925, *International standard of resistance for copper*. | Annealed copper at 20 °C: ρ = 1/58 Ω·mm²/m (σ = 5.8 × 10⁷ S/m), temperature coefficient 0.00393 /K. |

### Numerical methods

| Key | Source | Used for |
|---|---|---|
| Ho 1975 | C.-W. Ho, A. E. Ruehli, P. A. Brennan, "The modified nodal approach to network analysis," *IEEE Trans. Circuits Syst.*, vol. 22, no. 6, pp. 504–509, 1975. | The system's form: branch equations (KVL) with node potentials, KCL at the nodes. |
| Benzi 2005 | M. Benzi, G. H. Golub, J. Liesen, "Numerical solution of saddle point problems," *Acta Numerica*, vol. 14, pp. 1–137, 2005. | The saddle-point system [Z −A; Aᵀ 0] and its Schur-complement preconditioner Aᵀ D⁻¹ A. |
| Saad 1986 | Y. Saad, M. H. Schultz, "GMRES: A generalized minimal residual algorithm for solving nonsymmetric linear systems," *SIAM J. Sci. Stat. Comput.*, vol. 7, no. 3, pp. 856–869, 1986. | GMRES. |
| Saad 2003 | Y. Saad, *Iterative Methods for Sparse Linear Systems*, 2nd ed. Philadelphia: SIAM, 2003. | Restarted, right-preconditioned GMRES; the Hessenberg least-squares problem kept triangular with Givens rotations. |
| Giraud 2005 | L. Giraud, J. Langou, M. Rozložník, "The loss of orthogonality in the Gram-Schmidt orthogonalization process," *Comput. Math. Appl.*, vol. 50, pp. 1069–1075, 2005. | Classical Gram-Schmidt run twice ("twice is enough") keeps the Krylov basis orthogonal. |
| Woodbury 1950 | M. A. Woodbury, "Inverting modified matrices," Memorandum Rept. 42, Statistical Research Group, Princeton University, 1950. | The low-rank update of a factorised system when a few branches (candidate vias, parts) are added. |
| Demmel 1999 | J. W. Demmel, S. C. Eisenstat, J. R. Gilbert, X. S. Li, J. W. H. Liu, "A supernodal approach to sparse partial pivoting," *SIAM J. Matrix Anal. Appl.*, vol. 20, no. 3, pp. 720–755, 1999. | SuperLU, the sparse LU of the Schur complement (through SciPy). |
| Liu 1985 | J. W. H. Liu, "Modification of the minimum-degree algorithm by multiple elimination," *ACM Trans. Math. Softw.*, vol. 11, no. 2, pp. 141–153, 1985. | The multiple minimum degree ordering (SuperLU's `MMD_AT_PLUS_A`). |
| Oppenheim 2010 | A. V. Oppenheim, R. W. Schafer, *Discrete-Time Signal Processing*, 3rd ed. Upper Saddle River, NJ: Pearson, 2010. | Linear convolution by FFT: zero padding to at least 2n − 1 points. |
| Eymard 2000 | R. Eymard, T. Gallouët, R. Herbin, "Finite volume methods," in *Handbook of Numerical Analysis*, vol. VII, P. G. Ciarlet, J. L. Lions (eds.). Amsterdam: North-Holland, 2000, pp. 713–1020. | The finite-volume discretisation of ∇·(ε∇φ) = 0 in the cross-section solver; the flux out of a dual cell as its charge. |
| Abramowitz 1964 | M. Abramowitz, I. A. Stegun (eds.), *Handbook of Mathematical Functions*. Washington, DC: NBS, 1964. | Gauss–Legendre quadrature [§25.4]. |

## Assumptions

| Id | Assumption | Why, and the evidence |
|---|---|---|
| A1 | Quasi-static: within each model, retardation is neglected (partial inductances and capacitances are static). | Valid while the model is small against the wavelength; above that the results are estimates (ARCHITECTURE "Board-wide resonances", VALIDATION_AUDIT SP-19). |
| A2 | The current in a conductor of thickness t at frequency f flows in an effective thickness δ(1 − e^(−t/δ)). | The integral of an exponential current decay e^(−z/δ) over the thickness; t at low frequencies, δ once δ ≪ t. An engineering approximation (one-sided decay; a foil carrying current on both faces is not resolved). |
| A3 | A plated via's barrel is 25 µm of copper (`VIA_PLATING`), its inductance a filament's at the barrel radius. | 25 µm is a common plating thickness (the user can set `via_plating_um`); the filament at the radius is the thin tube's GMD [Grover 1946]. |
| A4 | A strip narrower than the pitch keeps its mutual inductance to every strip but its side-by-side neighbours at the full-cell value; those neighbours are corrected up to 2 strips along (`NEAR_AXIAL`). | The mutual depends on the centres' distance; near neighbours are where width and centre matter. Evidence: SP-27 (two touching half strips = one strip within 1 %; a neck on and off the cell rows within 1 %). |
| A5 | A line's cross-section is quasi-TEM: per-unit-length C from electrostatics, L from the same cut in air. | Exact for TEM, a good approximation for microstrip and offset stripline at board frequencies; the cuts are checked against Cohn's exact stripline and other exact results (tests/test_xsection.py). |
