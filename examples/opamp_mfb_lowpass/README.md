# Multiple-feedback low-pass (drawn in KiCad)

A 2nd-order multiple-feedback low-pass around an OPA376, the design of TI's OPA376 datasheet (SBOS406G, §8.2,
"Typical Application"): R1 590 Ω, R3 499 Ω, R4 2.94 kΩ, C2 39 nF, C5 1 nF; inverting gain 5, 25 kHz cutoff, a
Chebyshev response with 3 dB of peaking. Unlike the other examples it is a real KiCad project (schematic and board),
not generated, and it is the reference for `tests/test_example_mfb.py`.

    groundtruth bode examples/opamp_mfb_lowpass/opamp_path_lp.kicad_pcb --to 1MHz

GroundTruth reads the circuit from the board and finds the path J1.2 → R1 → R3 → U1 → J2.2 (the op-amp recognised by
its pin names; its + input on GND, so the signal enters at the − input). Its ideal transfer function, in the parts'
names, is exactly TI's Eq. 1:

    H(s) = −1/(R1·R3·C2·C5) / (s² + (s/C2)·(1/R1 + 1/R3 + 1/R4) + 1/(R3·R4·C2·C5))

DC gain −R4/R1 = −4.98, f0 = 1/(2π·√(R3·R4·C2·C5)) = 21.0 kHz. With the board's tracks and the op-amp's macromodel
the Bode plot shows +13.6 dB (4.78×), 2.9 dB of peaking at 17 kHz and −3 dB at 28 kHz: the gain is 4.78, not 4.98,
because the settings give the source 25 Ω (`analysis.driver_ohms`), in series with R1 (2940 / 615).
