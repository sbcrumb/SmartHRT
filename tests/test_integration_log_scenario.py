"""Test d'intégration pour reproduire le problème exact identifié dans les logs.

Ce test simule le scénario exact de SmartHRT Chambre#01KGJBGC où:
1. L'heure de relance était calculée à 19h26 à l'initialisation
2. Les modifications successives des coefficients l'ont fait évoluer à 21h08
3. Mais le trigger n'était pas reprogrammé, causant un déclenchement à 19h26

Le test vérifie que le problème est désormais corrigé.
"""

from datetime import datetime, time as dt_time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.SmartHRT.coordinator import (
    SmartHRTCoordinator,
    SmartHRTData,
    SmartHRTState,
)


class TestIntegrationLogScenario:
    """Test d'intégration reproduisant le scénario exact des logs."""

    @pytest.mark.asyncio
    async def test_log_scenario_smarthrt_chambre_01kgjbgc_regression(
        self, create_coordinator
    ):
        """Reproduit exactement le scénario problématique des logs.

        Séquence des événements extraite des logs:
        - 19:16:17 - Initialisation, recovery_time calculé à 19:26:18
        - 19:16:37 - RCth = 43.97, recovery_time = 19:26:38
        - 19:16:43 - RCth LW = 49.64, recovery_time = 19:26:44
        - 19:16:50 - RCth HW = 37.88, recovery_time = 19:26:51
        - 19:16:55 - RPth = 97.0, recovery_time = 19:26:56
        - 19:17:02 - RPth LW = 104.0, recovery_time = 21:08:08 ← changement majeur
        - 19:17:12 - RPth HW = 54.0, recovery_time = 21:08:40 ← heure finale
        - 19:26:18 - PROBLÈME: trigger se déclenche à l'ancienne heure!
        """

        # Mock du calcul pour contrôler les heures de relance exactes
        recovery_times = []

        def mock_calculate_recovery_time(coord):
            # Simuler l'évolution des heures de relance selon les logs
            if len(recovery_times) == 0:
                coord.data.recovery_start_hour = datetime(
                    2026, 2, 3, 19, 26, 18
                )  # Initial
            elif len(recovery_times) == 1:
                coord.data.recovery_start_hour = datetime(
                    2026, 2, 3, 19, 26, 38
                )  # RCth change
            elif len(recovery_times) == 2:
                coord.data.recovery_start_hour = datetime(
                    2026, 2, 3, 19, 26, 44
                )  # RCth LW
            elif len(recovery_times) == 3:
                coord.data.recovery_start_hour = datetime(
                    2026, 2, 3, 19, 26, 51
                )  # RCth HW
            elif len(recovery_times) == 4:
                coord.data.recovery_start_hour = datetime(
                    2026, 2, 3, 19, 26, 56
                )  # RPth
            elif len(recovery_times) == 5:
                coord.data.recovery_start_hour = datetime(
                    2026, 2, 3, 21, 8, 8
                )  # RPth LW ← CHANGEMENT MAJEUR
            elif len(recovery_times) == 6:
                coord.data.recovery_start_hour = datetime(
                    2026, 2, 3, 21, 8, 40
                )  # RPth HW ← FINAL

            recovery_times.append(coord.data.recovery_start_hour)

        # Initialisation à 19h16:17 comme dans les logs
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 3, 19, 16, 17)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                smartheating_mode=True,
                interior_temp=15.9,
                exterior_temp=8.1,
                wind_speed=4.33,  # 15.6 km/h / 3.6
                data_overrides={
                    "tsp": 19.0,
                    "target_hour": dt_time(23, 0, 0),
                    "recoverycalc_hour": dt_time(23, 30, 0),
                },
            )

        # Tracker de tous les appels de programmation de trigger
        scheduled_times = []

        def track_scheduling(*args, **kwargs):
            if len(args) >= 3:  # async_track_point_in_time(hass, callback, time)
                scheduled_times.append(args[2])
            return MagicMock()

        with patch(
            "custom_components.SmartHRT.coordinator.async_track_point_in_time",
            side_effect=track_scheduling,
        ):
            with patch.object(
                coord,
                "calculate_recovery_time",
                lambda: mock_calculate_recovery_time(coord),
            ):

                # État initial - trigger programmé à 19h26:18
                # Simuler l'appel initial de calculate_recovery_time qui définit la première heure
                mock_calculate_recovery_time(coord)  # Ceci incrémente recovery_times
                coord._schedule_recovery_start(coord.data.recovery_start_hour)

                # Séquence exacte des changements des logs
                # 19:16:37 - RCth changed to: 43.97
                coord.set_rcth(43.97)

                # 19:16:43 - RCth LW changed to: 49.64
                coord.set_rcth_lw(49.64)

                # 19:16:50 - RCth HW changed to: 37.88
                coord.set_rcth_hw(37.88)

                # 19:16:55 - RPth changed to: 97.0
                coord.set_rpth(97.0)

                # 19:17:02 - RPth LW changed to: 104.0 → Recovery time: 21:08:08
                coord.set_rpth_lw(104.0)

                # 19:17:12 - RPth HW changed to: 54.0 → Recovery time: 21:08:40
                coord.set_rpth_hw(54.0)

        # Vérifications critiques
        assert (
            len(scheduled_times) >= 7
        ), f"Pas assez de programmations: {len(scheduled_times)}"

        # Le trigger final doit être programmé à 21h08:40, pas 19h26:18
        final_scheduled_time = scheduled_times[-1]
        assert (
            final_scheduled_time.hour == 21
        ), f"Heure incorrecte: {final_scheduled_time.hour}, attendu: 21"
        assert (
            final_scheduled_time.minute == 8
        ), f"Minute incorrecte: {final_scheduled_time.minute}, attendu: 8"

        # Vérification de l'évolution progressive des heures programmées
        # Toutes les heures jusqu'à l'index 4 doivent être autour de 19h26
        for i in range(5):  # indices 0-4 correspondent à 19h26
            scheduled_time = scheduled_times[i]
            assert (
                scheduled_time.hour == 19
            ), f"Heure incorrecte à l'index {i}: {scheduled_time}"
            assert (
                scheduled_time.minute == 26
            ), f"Minute incorrecte à l'index {i}: {scheduled_time}"

        # Les deux dernières programmations (indices 5-6) doivent être à 21h08
        for i in range(5, len(scheduled_times)):
            scheduled_time = scheduled_times[i]
            assert (
                scheduled_time.hour == 21
            ), f"Heure finale incorrecte à l'index {i}: {scheduled_time}"
            assert (
                scheduled_time.minute == 8
            ), f"Minute finale incorrecte à l'index {i}: {scheduled_time}"

    @pytest.mark.asyncio
    async def test_trigger_correctly_cancelled_between_reschedules(
        self, create_coordinator
    ):
        """Vérifie que chaque reprogrammation annule le trigger précédent."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        cancellation_calls = []

        def mock_track_point_in_time(*args, **kwargs):
            mock_unsubscribe = MagicMock()
            # Enregistrer les appels d'annulation
            mock_unsubscribe.side_effect = lambda: cancellation_calls.append(
                "cancelled"
            )
            return mock_unsubscribe

        # Simuler une série de changements de coefficients
        with patch(
            "custom_components.SmartHRT.coordinator.async_track_point_in_time",
            side_effect=mock_track_point_in_time,
        ):

            # Premier trigger
            coord.data.recovery_start_hour = datetime(2026, 2, 3, 20, 0, 0)
            coord._schedule_recovery_start(coord.data.recovery_start_hour)

            # Série de changements (chacun doit annuler le précédent)
            times = [
                datetime(2026, 2, 3, 20, 30, 0),
                datetime(2026, 2, 3, 21, 0, 0),
                datetime(2026, 2, 3, 21, 30, 0),
            ]

            for new_time in times:
                coord.data.recovery_start_hour = new_time
                coord._schedule_recovery_start(new_time)

            # Vérifier qu'il y a eu le bon nombre d'annulations
            # 3 changements = 3 annulations du trigger précédent
            assert (
                len(cancellation_calls) == 3
            ), f"Annulations incorrectes: {len(cancellation_calls)}"

    @pytest.mark.asyncio
    async def test_no_trigger_leak_after_multiple_changes(self, create_coordinator):
        """Vérifie qu'il n'y a pas de fuite de triggers après plusieurs changements."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        active_triggers = []

        def mock_track_point_in_time(*args, **kwargs):
            mock_unsub = MagicMock()
            active_triggers.append(mock_unsub)

            def cancel():
                if mock_unsub in active_triggers:
                    active_triggers.remove(mock_unsub)

            mock_unsub.side_effect = cancel
            return mock_unsub

        with patch(
            "custom_components.SmartHRT.coordinator.async_track_point_in_time",
            side_effect=mock_track_point_in_time,
        ):

            # Simuler de multiples changements de coefficients
            coefficients = [45.0, 50.0, 40.0, 55.0, 35.0]

            for coeff in coefficients:
                coord.data.recovery_start_hour = datetime(2026, 2, 3, 21, 0, 0)
                with patch.object(coord, "calculate_recovery_time"):
                    coord.set_rcth(coeff)

            # À la fin, il ne doit y avoir qu'un seul trigger actif
            assert (
                len(active_triggers) == 1
            ), f"Fuite de triggers détectée: {len(active_triggers)} triggers actifs"
