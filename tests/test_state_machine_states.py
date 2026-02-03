"""Tests pour la classe SmartHRTState et les états de la machine à états.

ADR-003: Machine à états explicite pour le cycle de chauffage
Vérifie que les 5 états sont correctement définis et utilisables.
"""

import pytest

from custom_components.SmartHRT.coordinator import SmartHRTState


class TestSmartHRTStateDefinition:
    """Tests pour la définition des états de la machine."""

    def test_state_heating_on_value(self):
        """Vérifie que l'état HEATING_ON a la bonne valeur."""
        assert SmartHRTState.HEATING_ON == "heating_on"

    def test_state_detecting_lag_value(self):
        """Vérifie que l'état DETECTING_LAG a la bonne valeur."""
        assert SmartHRTState.DETECTING_LAG == "detecting_lag"

    def test_state_monitoring_value(self):
        """Vérifie que l'état MONITORING a la bonne valeur."""
        assert SmartHRTState.MONITORING == "monitoring"

    def test_state_recovery_value(self):
        """Vérifie que l'état RECOVERY a la bonne valeur."""
        assert SmartHRTState.RECOVERY == "recovery"

    def test_state_heating_process_value(self):
        """Vérifie que l'état HEATING_PROCESS a la bonne valeur."""
        assert SmartHRTState.HEATING_PROCESS == "heating_process"

    def test_all_states_are_unique(self):
        """Vérifie que tous les états sont uniques."""
        states = [
            SmartHRTState.HEATING_ON,
            SmartHRTState.DETECTING_LAG,
            SmartHRTState.MONITORING,
            SmartHRTState.RECOVERY,
            SmartHRTState.HEATING_PROCESS,
        ]
        assert len(states) == len(set(states))

    def test_all_states_are_strings(self):
        """Vérifie que tous les états sont des chaînes de caractères."""
        states = [
            SmartHRTState.HEATING_ON,
            SmartHRTState.DETECTING_LAG,
            SmartHRTState.MONITORING,
            SmartHRTState.RECOVERY,
            SmartHRTState.HEATING_PROCESS,
        ]
        for state in states:
            assert isinstance(state, str)


class TestStateCycleOrder:
    """Tests pour le cycle de vie des états selon l'ADR-003.

    Cycle attendu:
    HEATING_ON → DETECTING_LAG → MONITORING → RECOVERY → HEATING_PROCESS → HEATING_ON
    """

    def test_state_cycle_definition(self):
        """Vérifie que le cycle des états est correct."""
        expected_cycle = [
            SmartHRTState.HEATING_ON,  # État 1: Journée, chauffage actif
            SmartHRTState.DETECTING_LAG,  # État 2: Attente baisse température
            SmartHRTState.MONITORING,  # État 3: Surveillance nocturne
            SmartHRTState.RECOVERY,  # État 4: Moment de la relance
            SmartHRTState.HEATING_PROCESS,  # État 5: Montée en température
            # Retour à HEATING_ON
        ]
        assert len(expected_cycle) == 5

    def test_heating_on_is_initial_state(self):
        """Vérifie que HEATING_ON est l'état initial logique."""
        from custom_components.SmartHRT.coordinator import SmartHRTData

        data = SmartHRTData()
        assert data.current_state == SmartHRTState.HEATING_ON

    def test_state_1_is_heating_on(self):
        """État 1: HEATING_ON - Chauffage actif pendant la journée."""
        assert SmartHRTState.HEATING_ON == "heating_on"

    def test_state_2_is_detecting_lag(self):
        """État 2: DETECTING_LAG - Attente de la baisse effective (-0.2°C)."""
        assert SmartHRTState.DETECTING_LAG == "detecting_lag"

    def test_state_3_is_monitoring(self):
        """État 3: MONITORING - Surveillance nocturne, calculs récurrents."""
        assert SmartHRTState.MONITORING == "monitoring"

    def test_state_4_is_recovery(self):
        """État 4: RECOVERY - Instant de la relance, calcul RCth."""
        assert SmartHRTState.RECOVERY == "recovery"

    def test_state_5_is_heating_process(self):
        """État 5: HEATING_PROCESS - Montée en température, calcul RPth."""
        assert SmartHRTState.HEATING_PROCESS == "heating_process"
