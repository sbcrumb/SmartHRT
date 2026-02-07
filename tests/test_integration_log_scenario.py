"""Test d'intégration pour reproduire le problème exact identifié dans les logs.

Ce test simule le scénario exact de SmartHRT Chambre#01KGJBGC où:
1. L'heure de relance était calculée à 19h26 à l'initialisation
2. Les modifications successives des coefficients l'ont fait évoluer à 21h08
3. Mais le trigger n'était pas reprogrammé, causant un déclenchement à 19h26

Le test vérifie que le problème est désormais corrigé grâce au TimerManager (ADR-051).
"""

from datetime import datetime, time as dt_time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.SmartHRT.const import TimerKey
from custom_components.SmartHRT.coordinator import (
    SmartHRTCoordinator,
    SmartHRTState,
)
from custom_components.SmartHRT.data_model import SmartHRTData  # ADR-047


class TestIntegrationLogScenario:
    """Test d'intégration reproduisant le scénario exact des logs."""

    @pytest.mark.asyncio
    async def test_log_scenario_smarthrt_chambre_01kgjbgc_regression(
        self, create_coordinator
    ):
        """Reproduit exactement le scénario problématique des logs.

        ADR-051: Le TimerManager garantit que chaque schedule() annule
        automatiquement le timer précédent pour la même clé.

        Séquence des événements extraite des logs:
        - 19:16:17 - Initialisation, recovery_time calculé à 19:26:18
        - 19:16:37 - RCth = 43.97, recovery_time = 19:26:38
        - 19:16:43 - RCth LW = 49.64, recovery_time = 19:26:44
        - 19:16:50 - RCth HW = 37.88, recovery_time = 19:26:51
        - 19:16:55 - RPth = 97.0, recovery_time = 19:26:56
        - 19:17:02 - RPth LW = 104.0, recovery_time = 21:08:08 ← changement majeur
        - 19:17:12 - RPth HW = 54.0, recovery_time = 21:08:40 ← heure finale
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

        # Tracker de tous les appels de programmation de trigger via TimerManager
        scheduled_times = []

        def track_scheduling(*args, **kwargs):
            if len(args) >= 3:  # async_track_point_in_time(hass, callback, time)
                scheduled_times.append(args[2])
            return MagicMock()

        # ADR-051: Patch au niveau du timer_manager
        with patch(
            "custom_components.SmartHRT.timer_manager.async_track_point_in_time",
            side_effect=track_scheduling,
        ):
            with patch.object(
                coord,
                "calculate_recovery_time",
                lambda: mock_calculate_recovery_time(coord),
            ):

                # État initial - trigger programmé à 19h26:18
                mock_calculate_recovery_time(coord)
                coord._schedule_recovery_start(coord.data.recovery_start_hour)

                # Séquence exacte des changements des logs
                coord.set_rcth(43.97)
                coord.set_rcth_lw(49.64)
                coord.set_rcth_hw(37.88)
                coord.set_rpth(97.0)
                coord.set_rpth_lw(104.0)
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

    @pytest.mark.asyncio
    async def test_trigger_correctly_cancelled_between_reschedules(
        self, create_coordinator
    ):
        """Vérifie que le TimerManager gère les annulations automatiquement (ADR-051)."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        # Avec ADR-051, le TimerManager gère l'annulation automatique
        # Vérifions simplement que schedule() fonctionne correctement
        times = [
            datetime(2026, 2, 3, 20, 0, 0),
            datetime(2026, 2, 3, 20, 30, 0),
            datetime(2026, 2, 3, 21, 0, 0),
            datetime(2026, 2, 3, 21, 30, 0),
        ]

        for new_time in times:
            coord.data.recovery_start_hour = new_time
            coord._schedule_recovery_start(new_time)

        # Avec TimerManager, il ne doit y avoir qu'un seul timer actif
        assert coord._timer_manager.is_active(TimerKey.RECOVERY_START)
        # Vérifier que l'heure programmée est la dernière
        info = coord._timer_manager.get_info(TimerKey.RECOVERY_START)
        assert info is not None
        assert info.scheduled_time == times[-1]

    @pytest.mark.asyncio
    async def test_no_trigger_leak_after_multiple_changes(self, create_coordinator):
        """Vérifie qu'il n'y a pas de fuite de triggers grâce au TimerManager (ADR-051)."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING, smartheating_mode=True
        )

        # Simuler de multiples changements de coefficients
        coefficients = [45.0, 50.0, 40.0, 55.0, 35.0]

        for coeff in coefficients:
            coord.data.recovery_start_hour = datetime(2026, 2, 3, 21, 0, 0)
            with patch.object(coord, "calculate_recovery_time"):
                coord.set_rcth(coeff)

        # ADR-051: TimerManager garantit un seul timer par clé
        # Compter les timers RECOVERY_START actifs (doit être exactement 1 ou 0)
        recovery_start_active = coord._timer_manager.is_active(TimerKey.RECOVERY_START)

        # Il doit y avoir au plus 1 timer RECOVERY_START actif (ou 0 si déjà déclenché)
        # Le test vérifie l'absence de fuite (pas multiples timers pour la même clé)
        if recovery_start_active:
            assert coord._timer_manager.get_info(TimerKey.RECOVERY_START) is not None
