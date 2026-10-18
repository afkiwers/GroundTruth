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
| Pozar 2012 | D. M. Pozar, *Microwave Engineering*, 4th ed. Hoboken, NJ: Wiley, 2012. | Dielectric loss tangent (ε = ε′(1 − j tan δ)); parallel-plate capacitance; cavity resonances and their Q (conductor and dielectric loss add as 1/Q); a parallel-plate line's inductance μh per square; the input impedance of a terminated lossless line and the voltage and current along it. |
| Paul 2008 | C. R. Paul, *Analysis of Multiconductor Transmission Lines*, 2nd ed. Hoboken, NJ: Wiley–IEEE Press, 2008. | Per-unit-length L and C of a cross-section; L = μ0 ε0 C0⁻¹ for a homogeneous-equivalent TEM line; Z0, εeff; differential and common modes of a pair. |
| Johnson 1993 | H. W. Johnson, M. Graham, *High-Speed Digital Design: A Handbook of Black Magic*. Englewood Cliffs, NJ: Prentice Hall, 1993. | Near- and far-end crosstalk of coupled lines, the near-end saturating at 2 T_d ≥ t_r. |
| Hall 2000 | S. H. Hall, G. W. Hall, J. A. McCall, *High-Speed Digital System Design*. New York: Wiley, 2000. | Backward and forward crosstalk coefficients K_b = (L_m/L + C_m/C)/4, K_f = (L_m/L − C_m/C)/2. |
| CODATA 2018 | E. Tiesinga, P. J. Mohr, D. B. Newell, B. N. Taylor, "CODATA recommended values of the fundamental physical constants: 2018," *Rev. Mod. Phys.*, vol. 93, 025010, 2021. | ε0 = 8.8541878128 × 10⁻¹² F/m (μ0 is taken as 4π × 10⁻⁷ H/m, 5.5 × 10⁻¹⁰ relative from the 2018 value 1.25663706212 × 10⁻⁶ H/m). |
| IEC 60028 | IEC 60028:1925, *International standard of resistance for copper*. | Annealed copper at 20 °C: ρ = 1/58 Ω·mm²/m (σ = 5.8 × 10⁷ S/m), temperature coefficient 0.00393 /K. |

### Transmission lines, circuits and EMC

| Key | Source | Used for |
|---|---|---|
| Hammerstad 1980 | E. Hammerstad, Ø. Jensen, "Accurate models for microstrip computer-aided design," *IEEE MTT-S Int. Microwave Symp. Digest*, 1980, pp. 407–409. | Microstrip Z0 and εeff with the strip-thickness corrections (`spectrum.microstrip`). |
| Wheeler 1978 | H. A. Wheeler, "Transmission-line properties of a strip line between parallel planes," *IEEE Trans. Microw. Theory Tech.*, vol. 26, no. 11, pp. 866–876, 1978. | Symmetric stripline Z0 with thickness (`spectrum.stripline`). |
| Cohn 1954 | S. B. Cohn, "Characteristic impedance of the shielded-strip transmission line," *IRE Trans. Microw. Theory Tech.*, vol. 2, no. 2, pp. 52–57, 1954. | The exact zero-thickness stripline Z0 (the cross-section solver's check). |
| Wadell 1991 | B. C. Wadell, *Transmission Line Design Handbook*. Norwood, MA: Artech House, 1991. | The stripline formulas as collected there; the offset stripline as the combination of the two symmetric lines it lies between (`spectrum.offset_stripline`). |
| Desoer 1969 | C. A. Desoer, E. S. Kuh, *Basic Circuit Theory*. New York: McGraw-Hill, 1969. | Superposition, Thévenin sources, Tellegen's theorem in linear networks. |
| Tellegen 1952 | B. D. H. Tellegen, "A general network theorem, with applications," *Philips Res. Rep.*, vol. 7, pp. 259–269, 1952. | Σ V·I* over all branches = 0: the power a solution dissipates equals the power fed in (DC loss and energy checks). |
| Paul 2006 | C. R. Paul, *Introduction to Electromagnetic Compatibility*, 2nd ed. Hoboken, NJ: Wiley, 2006. | The spectrum of a trapezoidal pulse train and its two-corner envelope; Fourier coefficients by differentiating to impulses; the differential-mode radiation of a small loop, |E| = 1.316 × 10⁻¹⁴ f² |m| / r; the LISN's role. |
| Balanis 2016 | C. A. Balanis, *Antenna Theory: Analysis and Design*, 4th ed. Hoboken, NJ: Wiley, 2016. | The fields of a small loop (magnetic dipole), with the induction term: |E| ∝ k² |m| (1 + 1/(kr)²)^½ / r [§5.2]. |
| Haykin 2001 | S. Haykin, *Communication Systems*, 4th ed. New York: Wiley, 2001. | Sinusoidal FM: line amplitudes as Bessel functions J_n(β) (the spread receiver's check). |
| Hardin 1994 | K. B. Hardin, J. T. Fessler, D. R. Bush, "Spread spectrum clock generation for the reduction of radiated emissions," *Proc. IEEE Int. Symp. EMC*, 1994, pp. 227–231. | Spread-spectrum modulation of a clock or switching frequency and what it does to a receiver's reading. |
| Papoulis 2002 | A. Papoulis, S. U. Pillai, *Probability, Random Variables and Stochastic Processes*, 4th ed. New York: McGraw-Hill, 2002. | The envelope of a complex Gaussian signal is Rayleigh distributed; the largest of N such samples grows as σ·√(2 ln N). |
| Middlebrook 1976 | R. D. Middlebrook, "Input filter considerations in design and application of switching regulators," *Proc. IEEE Industry Applications Society Annual Meeting*, 1976, pp. 366–382. | Input-filter stability: the filter's output impedance against the converter's negative input impedance. |

### Standards

| Key | Standard | Used for |
|---|---|---|
| CISPR 32 | CISPR 32:2015, *Electromagnetic compatibility of multimedia equipment – Emission requirements*, Ed. 2.0 (with Amd. 1:2019). | Radiated limits class A / B, 30 MHz–6 GHz, and their distances; conducted limits at the AC mains port (average detector) [Annex A]. |
| CISPR 16-1-1 | CISPR 16-1-1, *Specification for radio disturbance and immunity measuring apparatus and methods – Part 1-1: Measuring apparatus*. | Receiver resolution bandwidths (200 Hz band A, 9 kHz band B, 120 kHz bands C/D, 1 MHz above 1 GHz, at −6 dB); the detectors, calibrated to read a CW signal's RMS value alike. |
| CISPR 16-1-2 | CISPR 16-1-2, *… Part 1-2: Coupling devices for conducted disturbance measurements*. | The V-network (AMN) 50 Ω ∥ (50 µH + 5 Ω). |
| CISPR 25 | CISPR 25, *Vehicles, boats and internal combustion engines – Radio disturbance characteristics – Limits and methods of measurement for the protection of on-board receivers*. | The 5 µH artificial network; the voltage-method limits by class. The edition the limit table follows is not verified [A10]. |
| MIL-STD-461G | MIL-STD-461G, *Requirements for the Control of Electromagnetic Interference Characteristics of Subsystems and Equipment*, Department of Defense, 11 December 2015. | CE101 (Figures CE101-1, -4), CE102, RE102 (Figures RE102-1 to -4), the LISN (Figure 6), Table II (bandwidths, dwell times). |
| MIL-STD-461H | MIL-STD-461H, Department of Defense, 17 April 2026 (supersedes 461G). | RE102-3's helicopter and space curves from 10 kHz. Where the code follows 461G instead (CE101 to 10 kHz, where 461H's CE101-1 goes to 20 kHz), the place says so. |
| IPC-2221B | IPC-2221B, *Generic Standard on Printed Board Design*, IPC, 2012. | Conductor current capacity, I = k·ΔT^0.44·A^0.725 (k = 0.048 external, 0.024 internal; A in mil²) for vias and tracks. |

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
| A6 | A signal's spectrum is evaluated to a decade above its second corner 1/(π t_r), at most 6 GHz (`analysis_band`). | Above the second corner the trapezoid's envelope falls 40 dB/decade [Paul 2006]; a decade on, harmonics are 40 dB under the corner. 6 GHz is CISPR 32's top. |
| A7 | A ground-plane reflection adds up to +6 dB to the radiated field (on by default). | The direct and reflected waves in phase double the field, as over an OATS or semi-anechoic floor; the worst case of the height scan. |
| A8 | The receiver's resolution filter is Gaussian with its −6 dB width at the RBW. | CISPR 16-1-1 specifies the −6 dB bandwidth and a selectivity mask, not the exact shape; the Gaussian is a common model of it. Checked against sinusoidal FM, slow sweeps and random hopping (tests/test_spread.py). |
| A9 | A signal path's tracks are lossless lines (series R and dielectric loss left out). | On board-length traces the loss is a fraction of a dB at the frequencies that matter for the ranking; the parts in the path keep their own losses. |
| A10 | The CISPR 25 voltage-method limit table (class 5 and the step per class) as commonly published; the edition is not verified. | The module says to check it against the edition the test plan cites. |
| A11 | Emission is differential mode only: the loop's own field, no common-mode cable currents, no switch-node dV/dt through stray capacitance. | The estimate ranks layouts; real emissions are often dominated by common mode (README, ARCHITECTURE). |
| A12 | Harmonics within one receiver bandwidth add as magnitudes (in phase) in a peak reading (`receiver_sum`). | Their phases are locked but the receiver's filter is not modelled per harmonic there; the in-phase sum is the upper bound, at most 20·log10(RBW/f0) dB above one harmonic. |
