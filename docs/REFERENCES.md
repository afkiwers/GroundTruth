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
| Johnson 1993 | H. W. Johnson, M. Graham, *High-Speed Digital Design: A Handbook of Black Magic*. Englewood Cliffs, NJ: Prentice Hall, 1993. | Near- and far-end crosstalk of coupled lines, the near-end saturating at 2 T_d ≥ t_r; the return current's spread over a ground plane under a trace, ∝ 1/(1 + (x/h)²). |
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

### Power electronics

| Key | Source | Used for |
|---|---|---|
| Erickson 2001 | R. W. Erickson, D. Maksimović, *Fundamentals of Power Electronics*, 2nd ed. Norwell, MA: Kluwer, 2001. | Buck and boost in steady state (volt-second and charge balance, duty, ripple, CCM), their small-signal models and the right-half-plane zero, current-programmed control [ch. 12], input-filter design with Middlebrook's criterion, loss models. |
| Ridley 1991 | R. B. Ridley, "A new, continuous-time model for current-mode control," *IEEE Trans. Power Electron.*, vol. 6, no. 2, pp. 271–280, 1991. | The peak-current-mode plant with the sampling double pole at f_sw/2, Q_p = 1/(π(m_c D′ − ½)). |
| McMurray 1972 | W. McMurray, "Optimum snubbers for power semiconductors," *IEEE Trans. Ind. Appl.*, vol. IA-8, no. 5, pp. 593–600, 1972. | RC snubber design around the ringing loop's characteristic impedance √(L/C). |
| Ogata 2010 | K. Ogata, *Modern Control Engineering*, 5th ed. Upper Saddle River, NJ: Prentice Hall, 2010. | The second-order step response: overshoot exp(−πζ/√(1 − ζ²)); Bode, crossover, phase and gain margins. |
| Smith 1999 | L. D. Smith, R. E. Anderson, D. W. Forehand, T. J. Pelc, T. Roy, "Power distribution system design methodology and capacitor selection for modern CMOS technology," *IEEE Trans. Adv. Packag.*, vol. 22, no. 3, pp. 284–291, 1999. | The target impedance Z_t = V·ripple / I_step of a supply rail. |
| Gustavsen 1999 | B. Gustavsen, A. Semlyen, "Rational approximation of frequency domain responses by vector fitting," *IEEE Trans. Power Del.*, vol. 14, no. 3, pp. 1052–1061, 1999. | Vector fitting a rail's or a port's impedance as a rational function. |
| Gustavsen 2006 | B. Gustavsen, "Improving the pole relocating properties of vector fitting," *IEEE Trans. Power Del.*, vol. 21, no. 3, pp. 1587–1592, 2006. | The relaxed vector fitting (a free constant in σ(s)). |
| Krishnan 2010 | R. Krishnan, *Permanent Magnet Synchronous and Brushless DC Motor Drives*. Boca Raton, FL: CRC Press, 2010. | The six-step (Hall-commutated) drive of a three-phase BLDC motor, its phase equations and the star equivalent of a delta winding. |

### Standards

| Key | Standard | Used for |
|---|---|---|
| CISPR 32 | CISPR 32:2015, *Electromagnetic compatibility of multimedia equipment – Emission requirements*, Ed. 2.0 (with Amd. 1:2019). | Radiated limits class A / B, 30 MHz–6 GHz, and their distances; conducted limits at the AC mains port (average detector) [Annex A]. |
| CISPR 16-1-1 | CISPR 16-1-1, *Specification for radio disturbance and immunity measuring apparatus and methods – Part 1-1: Measuring apparatus*. | Receiver resolution bandwidths (200 Hz band A, 9 kHz band B, 120 kHz bands C/D, 1 MHz above 1 GHz, at −6 dB); the detectors, calibrated to read a CW signal's RMS value alike. |
| CISPR 16-1-2 | CISPR 16-1-2, *… Part 1-2: Coupling devices for conducted disturbance measurements*. | The V-network (AMN) 50 Ω ∥ (50 µH + 5 Ω). |
| CISPR 25 | CISPR 25, *Vehicles, boats and internal combustion engines – Radio disturbance characteristics – Limits and methods of measurement for the protection of on-board receivers*. | The 5 µH artificial network; the voltage-method limits by class. The edition the limit table follows is not verified [A10]. |
| MIL-STD-461G | MIL-STD-461G, *Requirements for the Control of Electromagnetic Interference Characteristics of Subsystems and Equipment*, Department of Defense, 11 December 2015. | CE101 (Figures CE101-1, -4), CE102, RE102 (Figures RE102-1 to -4), the LISN (Figure 6), Table II (bandwidths, dwell times). |
| MIL-STD-461H | MIL-STD-461H, Department of Defense, 17 April 2026 (supersedes 461G). | RE102-3's helicopter and space curves from 10 kHz. Where the code follows 461G instead (CE101 to 10 kHz, where 461H's CE101-1 goes to 20 kHz), the place says so. |
| IPC-A-610 | IPC-A-610, *Acceptability of Electronic Assemblies*, IPC. | The minimum vertical solder fill of a plated through hole (75 %). |
| CDA | Copper Development Association, alloy data (copper.org): C26000, C51000, C17200, C14500. | The electrical conductivity (% IACS) of brass, phosphor bronze, beryllium copper and tellurium copper pins. |
| IPC-2221B | IPC-2221B, *Generic Standard on Printed Board Design*, IPC, 2012. | Conductor current capacity, I = k·ΔT^0.44·A^0.725 (k = 0.048 external, 0.024 internal; A in mil²) for vias and tracks; the electrical conductor spacing by voltage [Table 6-1] for the clearance check. |

### Heat transfer

| Key | Source | Used for |
|---|---|---|
| Incropera 2011 | T. L. Bergman, A. S. Lavine, F. P. Incropera, D. P. DeWitt, *Fundamentals of Heat and Mass Transfer*, 7th ed. Hoboken, NJ: Wiley, 2011. | Natural convection from horizontal plates (Nu = 0.54 Ra^¼, 0.15 Ra^⅓ above; 0.52 Ra^⅕ below), forced convection over a flat plate (0.664 Re^½ Pr^⅓; mixed 0.037 Re^⅘ − 871), linearised radiation, the two-surface enclosure's effective emissivity. |
| Churchill 1975 | S. W. Churchill, H. H. S. Chu, "Correlating equations for laminar and turbulent free convection from a vertical plate," *Int. J. Heat Mass Transf.*, vol. 18, no. 11, pp. 1323–1329, 1975. | Natural convection from a vertical board, the full-range correlation. |
| Churchill 1977 | S. W. Churchill, "A comprehensive correlating equation for laminar, assisting, forced and free convection," *AIChE J.*, vol. 23, no. 1, pp. 10–16, 1977. | Mixed convection: Nu³ = Nu_forced³ + Nu_natural³. |
| Sutherland 1893 | W. Sutherland, "The viscosity of gases and molecular force," *Phil. Mag.*, ser. 5, vol. 36, pp. 507–531, 1893. | Air's viscosity against temperature. |
| White 2006 | F. M. White, *Viscous Fluid Flow*, 3rd ed. New York: McGraw-Hill, 2006. | Sutherland's constants for air (μ0 = 1.716 × 10⁻⁵ Pa·s at 273 K, S = 110.4 K). |
| Patankar 1980 | S. V. Patankar, *Numerical Heat Transfer and Fluid Flow*. Washington, DC: Hemisphere, 1980. | The finite-volume discretisation of conduction (heat and DC current) with face conductances between cells. |

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
| Kabsch 1976 | W. Kabsch, "A solution for the best rotation to relate two sets of vectors," *Acta Crystallogr.*, vol. A32, pp. 922–923, 1976. | The least-squares rigid transform that lays a stacked board's connector pins onto ours (SVD of the cross-covariance). |
| Rosenbrock 1970 | H. H. Rosenbrock, *State-Space and Multivariable Theory*. London: Nelson, 1970. | A rational function's zeros as the finite generalised eigenvalues of its system pencil. |
| Press 2007 | W. H. Press, S. A. Teukolsky, W. T. Vetterling, B. P. Flannery, *Numerical Recipes: The Art of Scientific Computing*, 3rd ed. Cambridge: Cambridge University Press, 2007. | Newton's method for nonlinear equations (the motor drive's periodic steady state); root bracketing. |
| Oppenheim 1997 | A. V. Oppenheim, A. S. Willsky, S. H. Nawab, *Signals and Systems*, 2nd ed. Upper Saddle River, NJ: Prentice Hall, 1997. | Fourier series of periodic waveforms and Parseval's relation (a mean square from the line spectrum). |
| Dijkstra 1959 | E. W. Dijkstra, "A note on two problems in connexion with graphs," *Numer. Math.*, vol. 1, pp. 269–271, 1959. | The shortest copper path from a driver to its receiver (through SciPy). |
| Hestenes 1952 | M. R. Hestenes, E. Stiefel, "Methods of conjugate gradients for solving linear systems," *J. Res. Natl. Bur. Stand.*, vol. 49, no. 6, pp. 409–436, 1952. | Conjugate gradients for the symmetric positive definite thermal and DC systems. |
| Vanek 1996 | P. Vaněk, J. Mandel, M. Brezina, "Algebraic multigrid by smoothed aggregation for second and fourth order elliptic problems," *Computing*, vol. 56, pp. 179–196, 1996. | The smoothed-aggregation AMG preconditioner. |
| Ruge 1987 | J. W. Ruge, K. Stüben, "Algebraic multigrid," in *Multigrid Methods*, S. F. McCormick (ed.). Philadelphia: SIAM, 1987, pp. 73–130. | The classical (Ruge-Stüben) AMG, the fallback. |
| Bell 2022 | N. Bell, L. N. Olson, J. Schroder, "PyAMG: Algebraic multigrid solvers in Python," *J. Open Source Softw.*, vol. 7, no. 72, 4142, 2022. | The AMG implementation used. |
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
| A13 | Converters run in continuous conduction (CCM) in the loop and loss models. | The usual operating point at rated load; below half the ripple current the model says so in a note. |
| A14 | The hot loop's overshoot is at least L·I/t_r: the loop's inductance carrying the pulse current's change over the edge time. | The inductive kick of the commutation; the ringing estimate (C_oss) is reported beside it and the larger one is judged. |
| A15 | The copper's heat follows its temperature (ρ(T), first order); the current pattern is not re-solved with temperature-dependent conductivity. | The current distribution changes little with a few tens of kelvin of rise; the heat (∝ ρ) changes most. |
| A16 | A diode-rectified stage loses ½·Q_oss·V per hard edge (its switch's own output energy), a synchronous one Q_oss·V. | Synchronous: the switch's charge plus the rectifier FET's charged through it; a diode's own capacitance is small. An estimate where datasheets give only Q_oss. |
| A17 | A via barrel's current capacity is that of an internal IPC-2221 conductor of the barrel wall's cross-section. | IPC-2221's chart is for traces; a barrel through the board is cooled like an internal conductor. The older chart fit is the conservative one (as PCB Thermal uses it). |
| A18 | A pin's solder is SAC305 at ~13 % IACS (1.3 × 10⁻⁷ Ω·m), and a lead is 0.25 mm thinner than its hole when no pin diameter is given. | Typical values; both can be set per part (`tht:` settings, GT fields). |
| A19 | A rail's impedance is checked against its target up to 100 MHz by default (`check_to`). | Above it the package and the die's own capacitance usually decouple; settable per rail. |
| A20 | A net's loop inductance, its ideal reference and the board-wide offset are taken at f_ref = max(f0, 1 MHz). | Below ~1 MHz a plane's return current still follows resistance more than inductance; emissions come from the edges, well above. |
| A21 | A lumped part gets 0.3 nH of pad and escape inductance (`MOUNT_L`) on top of its own model; vias are modelled explicitly. | A typical value for a small SMD part's pads and short escapes. |
| A22 | Between solved frequencies, transfers and loop moments are interpolated linearly in log-log. | Exact for power laws, the usual shape between resonances; the solve frequencies are refined where the values bend. |
| A23 | A motor bridge's copper heat leaves out the motor's PWM ripple current (out through the motor pin). | It is small next to the switching current in the hot loops; the six-step currents and the switching currents are included. |
| A24 | A capacitor is a series R-L-C (ESR, ESL, C, optionally C at its DC bias); an inductor and a ferrite bead a series R-L. | The first-order model of each part; a ferrite's lossy resistance peak is not modelled (its impedance rating sets L at the rated frequency). |
| A25 | Models suggested from a part's reference, value and package (ESR, ESL, a ferrite's ~600 Ω at 100 MHz when unknown) are typical figures. | Replaced by the part's fields or the user's settings; a note marks each assumed value. |
| A26 | The Tier 1 rule limits: edge_factor 3·h from a plane's edge, a stitching via within 2 mm of a layer change, a decap within 5 mm, gaps from 0.1 mm. | Rules of thumb for where the return current is affected; all are settings (`rules:`). The 3·h follows the return's spread over a wide plane [Johnson 1993]. |
| A27 | Material properties are typical published values: copper k = 390 W/(m·K) (electrodeposited and rolled foil, below pure copper's ~400), FR4 0.81 / 0.29 W/(m·K) in plane / through plane, and the other laminates in `thermal/materials.py`. | Laminates vary by resin and glass; a board's own values can be set. |
| A28 | Air's conductivity as 0.0241·(T/273 K)^0.81 W/(m·K). | A fit to tabulated dry-air values over the board's temperature range. |
| A29 | A partly covered cell conducts with an effective conductivity from its copper coverage, and between cells in proportion to the shared face's open (copper) fraction (cut-cell). | Keeps a trace's resistance at any angle when it is only a few cells wide; checked against straight, diagonal and angled traces (tests/thermal/test_validation.py). |
