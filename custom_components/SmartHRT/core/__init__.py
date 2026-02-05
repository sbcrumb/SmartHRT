"""Module core SmartHRT - Modèle thermique Pure Python.

Ce module implémente ADR-026: Extraction du modèle thermique en Pure Python.

Le code est organisé en:
- types.py: Dataclasses pour l'état thermique et les coefficients
- thermal.py: ThermalSolver avec les calculs de physique thermique

Aucune dépendance à Home Assistant dans ce module.
"""

from .types import ThermalState, ThermalCoefficients, ThermalConfig
from .thermal import ThermalSolver

__all__ = [
    "ThermalState",
    "ThermalCoefficients",
    "ThermalConfig",
    "ThermalSolver",
]
