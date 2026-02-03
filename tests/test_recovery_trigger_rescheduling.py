"""Tests pour la reprogrammation des triggers de relance SmartHRT.

Ce module teste le problème critique où l'heure de relance était recalculée
mais le trigger n'était pas reprogrammé, causant un déclenchement à l'ancienne heure.

Problème corrigé dans le coordinator.py:
- Les fonctions set_rcth(), set_rpth(), set_*_lw(), set_*_hw(), etc.
  reprogramment maintenant le trigger après recalcul de l'heure de relance
- Le trigger précédent est correctement annulé avant reprogrammation
- Des logs appropriés sont générés pour traçabilité

Tests couverts:
1. Reprogrammation du trigger lors de modification des coefficients thermiques
2. Annulation du trigger précédent avant reprogrammation
3. Condition de sécurité (uniquement en état MONITORING)
4. Logs de traçabilité
5. Cas de régression du problème original
"""

from datetime import datetime, time as dt_time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from custom_components.SmartHRT.const import (
    DEFAULT_RCTH,
    DEFAULT_RPTH,
    DEFAULT_TSP,
)
from custom_components.SmartHRT.coordinator import (
    SmartHRTCoordinator,
    SmartHRTData,
    SmartHRTState,
)


class TestRecoveryTriggerRescheduling:
    """Tests pour la reprogrammation des triggers de relance."""

    @pytest.fixture
    def coordinator_monitoring(self, create_coordinator):
        """Fixture pour un coordinator en état MONITORING avec trigger actif."""

        async def _setup():
            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                smartheating_mode=True,
                interior_temp=17.0,
                exterior_temp=5.0,
            )

            # Simuler un trigger de relance déjà programmé
            coord._unsub_recovery_start = MagicMock()
            coord.data.recovery_start_hour = datetime(2026, 2, 3, 21, 0, 0)

            return coord

        return _setup

    @pytest.mark.asyncio
    async def test_rcth_change_reschedules_trigger(self, coordinator_monitoring):
        """Test que modifier RCth reprogramme le trigger de relance."""
        coord = await coordinator_monitoring()

        # Mock de la fonction de scheduling
        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            # Mock du calcul pour changer l'heure de relance
            new_recovery_time = datetime(2026, 2, 3, 20, 30, 0)
            with patch.object(coord, "calculate_recovery_time") as mock_calc:

                def side_effect():
                    coord.data.recovery_start_hour = new_recovery_time

                mock_calc.side_effect = side_effect

                # Changer RCth
                coord.set_rcth(45.0)

                # Vérifications
                mock_calc.assert_called_once()
                mock_schedule.assert_called_once_with(new_recovery_time)

    @pytest.mark.asyncio
    async def test_rpth_change_reschedules_trigger(self, coordinator_monitoring):
        """Test que modifier RPth reprogramme le trigger de relance."""
        coord = await coordinator_monitoring()

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            new_recovery_time = datetime(2026, 2, 3, 22, 0, 0)
            with patch.object(coord, "calculate_recovery_time") as mock_calc:

                def side_effect():
                    coord.data.recovery_start_hour = new_recovery_time

                mock_calc.side_effect = side_effect

                coord.set_rpth(100.0)

                mock_calc.assert_called_once()
                mock_schedule.assert_called_once_with(new_recovery_time)

    @pytest.mark.asyncio
    async def test_rcth_lw_change_reschedules_trigger(self, coordinator_monitoring):
        """Test que modifier RCth LW reprogramme le trigger de relance."""
        coord = await coordinator_monitoring()

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            new_recovery_time = datetime(2026, 2, 3, 19, 45, 0)
            with patch.object(coord, "calculate_recovery_time") as mock_calc:

                def side_effect():
                    coord.data.recovery_start_hour = new_recovery_time

                mock_calc.side_effect = side_effect

                coord.set_rcth_lw(50.0)

                mock_calc.assert_called_once()
                mock_schedule.assert_called_once_with(new_recovery_time)

    @pytest.mark.asyncio
    async def test_rcth_hw_change_reschedules_trigger(self, coordinator_monitoring):
        """Test que modifier RCth HW reprogramme le trigger de relance."""
        coord = await coordinator_monitoring()

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            new_recovery_time = datetime(2026, 2, 3, 20, 15, 0)
            with patch.object(coord, "calculate_recovery_time") as mock_calc:

                def side_effect():
                    coord.data.recovery_start_hour = new_recovery_time

                mock_calc.side_effect = side_effect

                coord.set_rcth_hw(38.0)

                mock_calc.assert_called_once()
                mock_schedule.assert_called_once_with(new_recovery_time)

    @pytest.mark.asyncio
    async def test_rpth_lw_change_reschedules_trigger(self, coordinator_monitoring):
        """Test que modifier RPth LW reprogramme le trigger de relance."""
        coord = await coordinator_monitoring()

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            new_recovery_time = datetime(2026, 2, 3, 21, 30, 0)
            with patch.object(coord, "calculate_recovery_time") as mock_calc:

                def side_effect():
                    coord.data.recovery_start_hour = new_recovery_time

                mock_calc.side_effect = side_effect

                coord.set_rpth_lw(105.0)

                mock_calc.assert_called_once()
                mock_schedule.assert_called_once_with(new_recovery_time)

    @pytest.mark.asyncio
    async def test_rpth_hw_change_reschedules_trigger(self, coordinator_monitoring):
        """Test que modifier RPth HW reprogramme le trigger de relance."""
        coord = await coordinator_monitoring()

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            new_recovery_time = datetime(2026, 2, 3, 20, 45, 0)
            with patch.object(coord, "calculate_recovery_time") as mock_calc:

                def side_effect():
                    coord.data.recovery_start_hour = new_recovery_time

                mock_calc.side_effect = side_effect

                coord.set_rpth_hw(55.0)

                mock_calc.assert_called_once()
                mock_schedule.assert_called_once_with(new_recovery_time)

    @pytest.mark.asyncio
    async def test_tsp_change_reschedules_trigger(self, coordinator_monitoring):
        """Test que modifier TSP reprogramme le trigger de relance."""
        coord = await coordinator_monitoring()

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            new_recovery_time = datetime(2026, 2, 3, 21, 15, 0)
            with patch.object(coord, "calculate_recovery_time") as mock_calc:

                def side_effect():
                    coord.data.recovery_start_hour = new_recovery_time

                mock_calc.side_effect = side_effect

                coord.set_tsp(20.0)

                mock_calc.assert_called_once()
                mock_schedule.assert_called_once_with(new_recovery_time)

    @pytest.mark.asyncio
    async def test_no_rescheduling_if_no_recovery_time(self, coordinator_monitoring):
        """Test qu'aucune reprogrammation n'a lieu si pas d'heure de relance calculée."""
        coord = await coordinator_monitoring()
        coord.data.recovery_start_hour = None  # Pas d'heure calculée

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            # Mock calculate_recovery_time pour qu'il ne définisse pas recovery_start_hour
            with patch.object(coord, "calculate_recovery_time"):
                coord.set_rcth(45.0)

                # Le trigger ne doit pas être reprogrammé
                mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_rescheduling_if_not_monitoring_state(self, create_coordinator):
        """Test qu'aucune reprogrammation n'a lieu si pas en état MONITORING."""
        coord = await create_coordinator(
            initial_state=SmartHRTState.HEATING_ON,
            smartheating_mode=True,
        )
        coord.data.recovery_start_hour = datetime(2026, 2, 3, 21, 0, 0)

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            coord.set_rcth(45.0)

            # Le trigger ne doit pas être reprogrammé car pas en état MONITORING
            mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_heating_stop_reschedules_trigger(self, coordinator_monitoring):
        """Test que on_heating_stop reprogramme le trigger si en état MONITORING."""
        coord = await coordinator_monitoring()

        with patch.object(coord, "_schedule_recovery_start") as mock_schedule:
            with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
                mock_now = datetime(2026, 2, 3, 20, 0, 0)
                mock_dt.now.return_value = mock_now

                new_recovery_time = datetime(2026, 2, 3, 21, 45, 0)
                with patch.object(coord, "calculate_recovery_time") as mock_calc:

                    def side_effect():
                        coord.data.recovery_start_hour = new_recovery_time

                    mock_calc.side_effect = side_effect

                    coord.on_heating_stop()

                    mock_calc.assert_called_once()
                    mock_schedule.assert_called_once_with(new_recovery_time)


class TestTriggerCancellationAndLogging:
    """Tests pour l'annulation des triggers précédents et les logs."""

    @pytest.fixture
    def coordinator_with_active_trigger(self, create_coordinator):
        """Fixture avec un trigger actif pour tester l'annulation."""

        async def _setup():
            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                smartheating_mode=True,
            )

            # Simuler un trigger actif
            mock_unsub = MagicMock()
            coord._unsub_recovery_start = mock_unsub
            coord.data.recovery_start_hour = datetime(2026, 2, 3, 21, 0, 0)

            return coord, mock_unsub

        return _setup

    @pytest.mark.asyncio
    async def test_previous_trigger_cancelled_before_rescheduling(
        self, coordinator_with_active_trigger
    ):
        """Test que le trigger précédent est annulé avant reprogrammation."""
        coord, mock_unsub = await coordinator_with_active_trigger()

        with patch(
            "custom_components.SmartHRT.coordinator.async_track_point_in_time"
        ) as mock_track:
            mock_track.return_value = MagicMock()

            # Programmer un nouveau trigger
            new_time = datetime(2026, 2, 3, 22, 0, 0)
            coord._schedule_recovery_start(new_time)

            # Vérifier que l'ancien trigger a été annulé
            mock_unsub.assert_called_once()
            # Vérifier qu'un nouveau trigger a été programmé
            mock_track.assert_called_once()

    @pytest.mark.asyncio
    async def test_rescheduling_logs_correctly(self, coordinator_with_active_trigger):
        """Test que les logs indiquent correctement une reprogrammation."""
        coord, mock_unsub = await coordinator_with_active_trigger()

        with patch(
            "custom_components.SmartHRT.coordinator.async_track_point_in_time"
        ) as mock_track:
            mock_track.return_value = MagicMock()

            with patch("custom_components.SmartHRT.coordinator._LOGGER") as mock_logger:
                new_time = datetime(2026, 2, 3, 22, 0, 0)
                coord._schedule_recovery_start(new_time)

                # Vérifier que le log indique une reprogrammation
                mock_logger.debug.assert_called_once()
                log_message = mock_logger.debug.call_args[0][0]
                assert "Trigger reprogrammé" in log_message

    @pytest.mark.asyncio
    async def test_initial_scheduling_logs_correctly(self, create_coordinator):
        """Test que les logs indiquent correctement un nouveau trigger."""
        coord = await create_coordinator(initial_state=SmartHRTState.MONITORING)

        with patch(
            "custom_components.SmartHRT.coordinator.async_track_point_in_time"
        ) as mock_track:
            mock_track.return_value = MagicMock()

            with patch("custom_components.SmartHRT.coordinator._LOGGER") as mock_logger:
                new_time = datetime(2026, 2, 3, 21, 0, 0)
                coord._schedule_recovery_start(new_time)

                # Vérifier que le log indique un nouveau trigger
                mock_logger.debug.assert_called_once()
                log_message = mock_logger.debug.call_args[0][0]
                assert (
                    "Nouveau" in log_message
                    and "Trigger reprogrammé" not in log_message
                )


class TestRegressionScenario:
    """Test de régression pour le problème original identifié dans les logs."""

    @pytest.mark.asyncio
    async def test_coefficients_change_scenario(self, create_coordinator):
        """Test du scénario de régression exact des logs:

        1. Initialisation avec heure de relance à 19h26
        2. Modifications successives des coefficients RCth/RPth
        3. Heure recalculée à 21h08
        4. Vérification que le trigger est reprogrammé à 21h08 (pas 19h26)
        """

        # Setup initial à 19h16 comme dans les logs
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 3, 19, 16, 17)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                smartheating_mode=True,
                interior_temp=15.9,
                exterior_temp=8.1,
            )

            # Simuler l'heure de relance initiale à 19h26 (comme dans les logs)
            initial_recovery_time = datetime(2026, 2, 3, 19, 26, 18)
            coord.data.recovery_start_hour = initial_recovery_time

            with patch(
                "custom_components.SmartHRT.coordinator.async_track_point_in_time"
            ) as mock_track:
                mock_track.return_value = MagicMock()

                # Simuler les changements de coefficients comme dans les logs
                # 19:16:37 - RCth changed to: 43.97
                coord.set_rcth(43.97)

                # 19:16:43 - RCth LW changed to: 49.64
                coord.set_rcth_lw(49.64)

                # 19:16:50 - RCth HW changed to: 37.88
                coord.set_rcth_hw(37.88)

                # 19:16:55 - RPth changed to: 97.0
                coord.set_rpth(97.0)

                # 19:17:02 - RPth LW changed to: 104.0 → Recovery time: 21:08:08
                with patch.object(coord, "calculate_recovery_time") as mock_calc:
                    final_recovery_time = datetime(2026, 2, 3, 21, 8, 8)

                    def side_effect():
                        coord.data.recovery_start_hour = final_recovery_time

                    mock_calc.side_effect = side_effect

                    coord.set_rpth_lw(104.0)

                # 19:17:12 - RPth HW changed to: 54.0 → Recovery time: 21:08:40
                with patch.object(coord, "calculate_recovery_time") as mock_calc:
                    final_recovery_time = datetime(2026, 2, 3, 21, 8, 40)

                    def side_effect():
                        coord.data.recovery_start_hour = final_recovery_time

                    mock_calc.side_effect = side_effect

                    coord.set_rpth_hw(54.0)

                # Vérifier que _schedule_recovery_start a été appelé avec la nouvelle heure
                # (et non l'ancienne heure de 19h26)
                last_call = mock_track.call_args_list[-1]
                # async_track_point_in_time(hass, callback, time) - time est le 3ème argument
                scheduled_time = last_call[0][
                    2
                ]  # 3ème argument de async_track_point_in_time

                assert scheduled_time == final_recovery_time
                # Cruciale: l'heure programmée doit être 21h08, pas 19h26
                assert scheduled_time.hour == 21
                assert scheduled_time.minute == 8

    @pytest.mark.asyncio
    async def test_trigger_fires_at_correct_time(self, create_coordinator):
        """Test que le trigger se déclenche à la bonne heure après reprogrammation."""

        coord = await create_coordinator(
            initial_state=SmartHRTState.MONITORING,
            smartheating_mode=True,
        )

        # Programmer un trigger initial
        initial_time = datetime(2026, 2, 3, 19, 26, 0)
        coord.data.recovery_start_hour = initial_time

        with patch(
            "custom_components.SmartHRT.coordinator.async_track_point_in_time"
        ) as mock_track:
            mock_callback = MagicMock()
            mock_track.return_value = mock_callback

            # Modifier un coefficient qui change l'heure de relance
            new_recovery_time = datetime(2026, 2, 3, 21, 8, 0)
            with patch.object(coord, "calculate_recovery_time") as mock_calc:

                def side_effect():
                    coord.data.recovery_start_hour = new_recovery_time

                mock_calc.side_effect = side_effect

                coord.set_rcth(45.0)

            # Vérifier que le nouveau trigger est programmé à la bonne heure
            mock_track.assert_called()
            last_call = mock_track.call_args_list[-1]
            # async_track_point_in_time(hass, callback, time)
            hass_arg, callback_func, trigger_time = last_call[0]

            assert trigger_time == new_recovery_time
            assert callback_func == coord._on_recovery_start_hour
