"""Tests de performance et robustesse pour la reprogrammation des triggers.

Ce module vérifie que la correction du problème de reprogrammation
n'introduit pas de régression de performance ou d'autres bugs.
"""

import time
from datetime import datetime, time as dt_time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.SmartHRT.coordinator import (
    SmartHRTState,
)


class TestPerformanceAndRobustness:
    """Tests de performance pour la reprogrammation des triggers."""

    @pytest.mark.asyncio
    async def test_rapid_coefficient_changes_performance(self, create_coordinator):
        """Test de performance pour des changements rapides de coefficients."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        # Mock pour mesurer le nombre d'appels
        schedule_calls = []

        def mock_schedule(*args, **kwargs):
            schedule_calls.append(time.time())
            return MagicMock()

        with patch.object(coord, "_schedule_recovery_start", side_effect=mock_schedule):
            with patch.object(coord, "calculate_recovery_time"):
                coord.data.recovery_start_hour = datetime(2026, 2, 3, 21, 0, 0)

                start_time = time.time()

                # Simuler 100 changements rapides
                for i in range(100):
                    coord.set_rcth(40.0 + i * 0.1)

                end_time = time.time()
                execution_time = end_time - start_time

                # Vérifier que l'exécution reste raisonnable (< 1 seconde)
                assert (
                    execution_time < 1.0
                ), f"Performance dégradée: {execution_time:.3f}s pour 100 changements"

                # Vérifier que tous les changements ont déclenché une reprogrammation
                assert (
                    len(schedule_calls) == 100
                ), f"Nombre incorrect d'appels: {len(schedule_calls)}"

    @pytest.mark.asyncio
    async def test_memory_leak_prevention(self, create_coordinator):
        """Test pour vérifier qu'il n'y a pas de fuite mémoire avec les triggers."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        # Simuler des objets de trigger avec nettoyage
        created_triggers = []
        cleaned_triggers = []

        def mock_track_point_in_time(*args, **kwargs):
            trigger = MagicMock()
            created_triggers.append(trigger)

            def cleanup():
                cleaned_triggers.append(trigger)

            trigger.side_effect = cleanup
            return trigger

        with patch(
            "custom_components.SmartHRT.coordinator.async_track_point_in_time",
            side_effect=mock_track_point_in_time,
        ):
            with patch.object(coord, "calculate_recovery_time"):

                # Effectuer de nombreux changements
                for i in range(50):
                    coord.data.recovery_start_hour = datetime(2026, 2, 3, 21, i % 60, 0)
                    coord.set_rcth(40.0 + i)

                # Vérifier qu'il y a eu autant de nettoyages que de créations - 1
                # (le dernier trigger reste actif)
                expected_cleanups = len(created_triggers) - 1
                assert (
                    len(cleaned_triggers) == expected_cleanups
                ), f"Fuite mémoire détectée: {len(cleaned_triggers)}/{expected_cleanups} nettoyages"

    @pytest.mark.asyncio
    async def test_concurrent_modifications_safety(self, create_coordinator):
        """Test pour vérifier la sécurité en cas de modifications concurrentes."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        schedule_calls = []

        def mock_schedule(recovery_time):
            # Simuler un délai pour test de concurrence
            schedule_calls.append(recovery_time)
            return MagicMock()

        with patch.object(coord, "_schedule_recovery_start", side_effect=mock_schedule):
            with patch.object(coord, "calculate_recovery_time"):

                # Simuler des modifications concurrentes
                coord.data.recovery_start_hour = datetime(2026, 2, 3, 20, 0, 0)

                # Modification simultanée de plusieurs coefficients
                # (simulate ce qui pourrait arriver dans l'interface utilisateur)
                coord.set_rcth(45.0)
                coord.set_rpth(100.0)
                coord.set_rcth_lw(50.0)
                coord.set_rcth_hw(40.0)

                # Vérifier qu'il y a eu 4 appels (un par modification)
                assert (
                    len(schedule_calls) == 4
                ), f"Appels manqués: {len(schedule_calls)}/4"

                # Vérifier que tous les appels ont la même heure de référence
                # (car calculate_recovery_time est mocké)
                expected_time = datetime(2026, 2, 3, 20, 0, 0)
                for call_time in schedule_calls:
                    assert call_time == expected_time, f"Temps incohérent: {call_time}"

    @pytest.mark.asyncio
    async def test_error_handling_during_rescheduling(self, create_coordinator):
        """Test de la gestion d'erreur pendant la reprogrammation."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        # Simuler une erreur lors de la programmation
        def failing_schedule(*args, **kwargs):
            raise Exception("Erreur de programmation simulée")

        with patch.object(
            coord, "_schedule_recovery_start", side_effect=failing_schedule
        ):
            with patch.object(coord, "calculate_recovery_time"):
                coord.data.recovery_start_hour = datetime(2026, 2, 3, 21, 0, 0)

                # L'erreur ne doit pas empêcher la modification du coefficient
                try:
                    coord.set_rcth(45.0)
                    # Si on arrive ici, c'est que l'erreur a été gérée
                    assert coord.data.rcth == 45.0
                except Exception as e:
                    # L'exception ne doit pas remonter jusqu'ici
                    pytest.fail(f"Exception non gérée: {e}")

    @pytest.mark.asyncio
    async def test_state_consistency_after_multiple_changes(self, create_coordinator):
        """Test de la cohérence d'état après multiples changements."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        # Enregistrer tous les états de recovery_start_hour
        recovery_times = []

        def track_calculate():
            # Simuler un nouveau temps à chaque calcul (utiliser des minutes différentes pour rester < 24h)
            new_time = datetime(2026, 2, 3, 20, len(recovery_times) * 5, 0)
            coord.data.recovery_start_hour = new_time
            recovery_times.append(new_time)

        with patch.object(
            coord, "calculate_recovery_time", side_effect=track_calculate
        ):
            with patch.object(coord, "_schedule_recovery_start") as mock_schedule:

                # Série de modifications
                modifications = [
                    ("rcth", 45.0),
                    ("rpth", 100.0),
                    ("rcth_lw", 50.0),
                    ("rcth_hw", 40.0),
                    ("rpth_lw", 105.0),
                    ("rpth_hw", 95.0),
                    ("tsp", 20.0),
                ]

                for param, value in modifications:
                    getattr(coord, f"set_{param}")(value)

                    # Vérifier que la valeur a été correctement assignée
                    assert getattr(coord.data, param) == value

                # Vérifier que chaque modification a déclenché un recalcul et une reprogrammation
                assert len(recovery_times) == len(modifications)
                assert mock_schedule.call_count == len(modifications)

                # Vérifier que la dernière heure programmée correspond à la dernière calculée
                last_call = mock_schedule.call_args_list[-1]
                last_scheduled_time = last_call[0][
                    0
                ]  # Premier argument de _schedule_recovery_start

                assert (
                    last_scheduled_time == recovery_times[-1]
                ), f"Incohérence: programmé {last_scheduled_time}, calculé {recovery_times[-1]}"
