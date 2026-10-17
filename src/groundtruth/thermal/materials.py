"""Material properties (SI units)."""

from __future__ import annotations

from dataclasses import dataclass

STEFAN_BOLTZMANN = 5.670374419e-8


@dataclass(frozen=True)
class Conductor:
    """A conductor's electrical and thermal properties."""
    name: str
    resistivity_20c: float  # ohm*m
    alpha: float  # 1/K temperature coefficient of resistance
    k: float  # W/(m*K)
    rho_cp: float  # volumetric heat capacity J/(m^3*K)

    def resistivity(self, t_c):
        """Resistivity at t_c °C, linear in temperature about 20 °C: rho(T) = rho20 (1 + alpha (T - 20))."""
        return self.resistivity_20c * (1.0 + self.alpha * (t_c - 20.0))


@dataclass(frozen=True)
class Dielectric:
    """A laminate's thermal properties (anisotropic: in-plane and through-plane conductivity)."""
    name: str
    k_xy: float  # in-plane W/(m*K)
    k_z: float  # through-plane W/(m*K)
    rho_cp: float


COPPER = Conductor("Copper (ED/rolled)", 1.724e-8, 0.00393, 390.0, 3.45e6)

# Typical published values. In-plane conductivity of glass-reinforced laminates
# is higher than through-plane because of the woven glass.
DIELECTRICS: dict[str, Dielectric] = {
    "FR4": Dielectric("FR4", 0.81, 0.29, 1.9e6),
    "FR4 (high Tg)": Dielectric("FR4 (high Tg)", 0.8, 0.35, 1.9e6),
    "Polyimide": Dielectric("Polyimide", 0.35, 0.25, 1.6e6),
    "Rogers RO4350B": Dielectric("Rogers RO4350B", 0.69, 0.69, 1.8e6),
    "Rogers RO4003C": Dielectric("Rogers RO4003C", 0.71, 0.71, 1.8e6),
    "PTFE": Dielectric("PTFE", 0.25, 0.25, 2.2e6),
    "IMS dielectric (2 W/mK)": Dielectric("IMS dielectric (2 W/mK)", 2.0, 2.0, 2.2e6),
    "Aluminium core": Dielectric("Aluminium core", 150.0, 150.0, 2.4e6),
    "Ceramic (Al2O3)": Dielectric("Ceramic (Al2O3)", 24.0, 24.0, 3.0e6),
}

ALIASES = {
    "fr4": "FR4", "fr-4": "FR4", "fr408": "FR4 (high Tg)", "370hr": "FR4 (high Tg)", "is410": "FR4 (high Tg)",
    "polyimide": "Polyimide", "kapton": "Polyimide", "ro4350b": "Rogers RO4350B", "ro4003c": "Rogers RO4003C",
    "ptfe": "PTFE", "teflon": "PTFE", "aluminium": "Aluminium core", "aluminum": "Aluminium core",
}


def dielectric_for(material: str) -> Dielectric:
    """The dielectric for a stackup material name: an exact name, else the first alias contained in it
    ("Isola 370HR" -> FR4 (high Tg)), else FR4."""
    if material in DIELECTRICS:
        return DIELECTRICS[material]
    key = (material or "").strip().lower()
    for alias, name in ALIASES.items():
        if alias in key:
            return DIELECTRICS[name]
    return DIELECTRICS["FR4"]


# Enclosure wall materials: thermal conductivity W/(m*K)
WALL_MATERIALS = {
    "Aluminium": 167.0,
    "Steel": 50.0,
    "ABS": 0.17,
    "Polycarbonate": 0.2,
    "Nylon (PA66)": 0.25,
    "Carbon fibre": 5.0,
}
