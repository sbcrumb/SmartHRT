"""Module core SmartHRT - Modèle thermique Pure Python.

Ce module implémente ADR-026: Extraction du modèle thermique en Pure Python.

Le code est organisé en:
- types.py: Dataclasses pour l'état thermique et les coefficients
- thermal.py: ThermalSolver avec les calculs de physique thermique

Aucune dépendance à Home Assistant dans ce module.
"""

from .types import (
    ThermalState,
    ThermalCoefficients,
    ThermalConfig,
    Action,
    StateTransitionResult,
    CoolSmartHRTState,
    CoolThermalCoefficients,
)
from .thermal import ThermalSolver
from .state_machine import (
    SmartHRTState,
    SmartHRTStateMachine,
    VALID_TRANSITIONS,
    TRANSITION_ACTIONS,
    STATE_FLAGS,
    get_state_flags,
    COOL_VALID_TRANSITIONS,
    COOL_TRANSITION_ACTIONS,
)

__all__ = [
    "ThermalState",
    "ThermalCoefficients",
    "ThermalConfig",
    "ThermalSolver",
    "Action",
    "StateTransitionResult",
    "SmartHRTState",
    "SmartHRTStateMachine",
    "VALID_TRANSITIONS",
    "TRANSITION_ACTIONS",
    "STATE_FLAGS",
    "get_state_flags",
    # Cool recovery
    "CoolSmartHRTState",
    "CoolThermalCoefficients",
    "COOL_VALID_TRANSITIONS",
    "COOL_TRANSITION_ACTIONS",
]
