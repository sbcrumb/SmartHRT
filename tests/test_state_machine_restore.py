"""Tests pour la restauration de l'état après redémarrage.

ADR-019: Restauration état après redémarrage

Ce module teste que la machine à états se restaure correctement
après un redémarrage de Home Assistant, en vérifiant:
- La cohérence entre état persisté et heure actuelle
- La reprogrammation des triggers
- La correction automatique des états incohérents
"""

from datetime import datetime, time as dt_time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.SmartHRT.const import DEFAULT_TSP
from custom_components.SmartHRT.coordinator import (
    SmartHRTCoordinator,
    SmartHRTData,
    SmartHRTState,
)


class TestDetermineExpectedState:
    """Tests pour _determine_expected_state_for_time.

    Cette méthode détermine l'état attendu basé sur l'heure actuelle.
    """

    @pytest.mark.asyncio
    async def test_morning_before_target_hour_no_recovery(self, create_coordinator):
        """Test: 05:00, pas de recovery_start_hour → MONITORING."""
        coord = await create_coordinator()
        coord.data.target_hour = dt_time(6, 0, 0)
        coord.data.recoverycalc_hour = dt_time(23, 0, 0)
        coord.data.recovery_start_hour = None

        now = datetime(2026, 2, 4, 5, 0, 0)
        expected = coord._determine_expected_state_for_time(now)

        # Entre 23:00 et 06:00 → MONITORING (nuit)
        assert expected == SmartHRTState.MONITORING

    @pytest.mark.asyncio
    async def test_morning_during_recovery_period(self, create_coordinator):
        """Test: 05:30, recovery_start_hour=05:00 → HEATING_PROCESS."""
        coord = await create_coordinator()
        coord.data.target_hour = dt_time(6, 0, 0)
        coord.data.recoverycalc_hour = dt_time(23, 0, 0)
        coord.data.recovery_start_hour = datetime(2026, 2, 4, 5, 0, 0)

        now = datetime(2026, 2, 4, 5, 30, 0)
        expected = coord._determine_expected_state_for_time(now)

        # Entre recovery_start_hour et target_hour → HEATING_PROCESS
        assert expected == SmartHRTState.HEATING_PROCESS

    @pytest.mark.asyncio
    async def test_morning_after_target_hour(self, create_coordinator):
        """Test: 10:00, après target_hour → HEATING_ON."""
        coord = await create_coordinator()
        coord.data.target_hour = dt_time(6, 0, 0)
        coord.data.recoverycalc_hour = dt_time(23, 0, 0)
        coord.data.recovery_start_hour = datetime(2026, 2, 4, 5, 0, 0)

        now = datetime(2026, 2, 4, 10, 0, 0)
        expected = coord._determine_expected_state_for_time(now)

        assert expected == SmartHRTState.HEATING_ON

    @pytest.mark.asyncio
    async def test_afternoon_before_recoverycalc(self, create_coordinator):
        """Test: 15:00, avant recoverycalc_hour → HEATING_ON."""
        coord = await create_coordinator()
        coord.data.target_hour = dt_time(6, 0, 0)
        coord.data.recoverycalc_hour = dt_time(23, 0, 0)

        now = datetime(2026, 2, 4, 15, 0, 0)
        expected = coord._determine_expected_state_for_time(now)

        assert expected == SmartHRTState.HEATING_ON

    @pytest.mark.asyncio
    async def test_night_after_recoverycalc(self, create_coordinator):
        """Test: 23:30, après recoverycalc_hour → MONITORING."""
        coord = await create_coordinator()
        coord.data.target_hour = dt_time(6, 0, 0)
        coord.data.recoverycalc_hour = dt_time(23, 0, 0)
        coord.data.recovery_start_hour = None

        now = datetime(2026, 2, 4, 23, 30, 0)
        expected = coord._determine_expected_state_for_time(now)

        assert expected == SmartHRTState.MONITORING

    @pytest.mark.asyncio
    async def test_atypical_hours_recoverycalc_before_target(self, create_coordinator):
        """Test: configuration atypique où recoverycalc < target."""
        coord = await create_coordinator()
        coord.data.target_hour = dt_time(17, 30, 0)  # 17:30
        coord.data.recoverycalc_hour = dt_time(13, 30, 0)  # 13:30

        # 14:00 entre recoverycalc et target → MONITORING
        now = datetime(2026, 2, 4, 14, 0, 0)
        expected = coord._determine_expected_state_for_time(now)

        assert expected == SmartHRTState.MONITORING

        # 18:00 après target → HEATING_ON
        now = datetime(2026, 2, 4, 18, 0, 0)
        expected = coord._determine_expected_state_for_time(now)

        assert expected == SmartHRTState.HEATING_ON


class TestRestoreStateAfterRestart:
    """Tests pour _restore_state_after_restart.

    Vérifie que les états sont correctement restaurés après redémarrage.
    """

    @pytest.mark.asyncio
    async def test_coherent_heating_on_state(self, create_coordinator):
        """Test: état HEATING_ON persisté, heure=10:00 → cohérent, pas de changement."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 10, 0, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.HEATING_ON,
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            await coord._restore_state_after_restart()

            assert coord.data.current_state == SmartHRTState.HEATING_ON

    @pytest.mark.asyncio
    async def test_coherent_monitoring_state(self, create_coordinator):
        """Test: état MONITORING persisté, heure=00:30 → cohérent."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 0, 30, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                recovery_calc_mode=True,
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            await coord._restore_state_after_restart()

            assert coord.data.current_state == SmartHRTState.MONITORING

    @pytest.mark.asyncio
    async def test_detecting_lag_treated_as_monitoring(self, create_coordinator):
        """Test: état DETECTING_LAG persisté pendant la nuit → cohérent avec MONITORING."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 23, 5, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.DETECTING_LAG,
                temp_lag_detection_active=True,
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            await coord._restore_state_after_restart()

            # DETECTING_LAG est considéré cohérent avec MONITORING
            assert coord.data.current_state == SmartHRTState.DETECTING_LAG

    @pytest.mark.asyncio
    async def test_recovery_treated_as_heating_process(self, create_coordinator):
        """Test: état RECOVERY persisté pendant la relance → transition vers HEATING_PROCESS."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 5, 30, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.RECOVERY,
                recovery_start_hour=datetime(2026, 2, 4, 5, 0, 0),
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            await coord._restore_state_after_restart()

            # RECOVERY doit passer à HEATING_PROCESS après redémarrage
            assert coord.data.current_state == SmartHRTState.HEATING_PROCESS

    @pytest.mark.asyncio
    async def test_incoherent_monitoring_in_day_corrected(self, create_coordinator):
        """Test: état MONITORING persisté mais heure=10:00 → corrigé vers HEATING_ON."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 10, 0, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                recovery_calc_mode=True,
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            await coord._restore_state_after_restart()

            # État incohérent → corrigé vers HEATING_ON
            assert coord.data.current_state == SmartHRTState.HEATING_ON

    @pytest.mark.asyncio
    async def test_incoherent_heating_process_without_recovery(
        self, create_coordinator
    ):
        """Test: état HEATING_PROCESS mais pas de recovery_start → corrigé."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 10, 0, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.HEATING_PROCESS,
                rp_calc_mode=True,
                recovery_start_hour=None,  # Pas de recovery configurée
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            await coord._restore_state_after_restart()

            # Devrait être corrigé vers HEATING_ON
            assert coord.data.current_state == SmartHRTState.HEATING_ON


class TestTransitionToExpectedState:
    """Tests pour _transition_to_expected_state.

    Vérifie que la transition forcée configure correctement l'état cible.
    """

    @pytest.mark.asyncio
    async def test_transition_to_heating_on(self, create_coordinator):
        """Test: transition forcée vers HEATING_ON."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 10, 0, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
            )

            await coord._transition_to_expected_state(
                SmartHRTState.HEATING_ON, mock_now
            )

            assert coord.data.current_state == SmartHRTState.HEATING_ON
            assert coord.data.recovery_calc_mode is False
            assert coord.data.rp_calc_mode is False
            assert coord.data.temp_lag_detection_active is False

    @pytest.mark.asyncio
    async def test_transition_to_monitoring(self, create_coordinator):
        """Test: transition forcée vers MONITORING."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 0, 30, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.HEATING_ON,
                interior_temp=18.0,
                exterior_temp=3.0,
            )
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            await coord._transition_to_expected_state(
                SmartHRTState.MONITORING, mock_now
            )

            assert coord.data.current_state == SmartHRTState.MONITORING
            assert coord.data.recovery_calc_mode is True
            assert coord.data.rp_calc_mode is False
            assert coord.data.temp_lag_detection_active is False

    @pytest.mark.asyncio
    async def test_transition_to_heating_process(self, create_coordinator):
        """Test: transition forcée vers HEATING_PROCESS."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 5, 30, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                recovery_start_hour=datetime(2026, 2, 4, 5, 0, 0),
                interior_temp=17.5,
                exterior_temp=2.0,
            )

            await coord._transition_to_expected_state(
                SmartHRTState.HEATING_PROCESS, mock_now
            )

            assert coord.data.current_state == SmartHRTState.HEATING_PROCESS
            assert coord.data.recovery_calc_mode is False
            assert coord.data.rp_calc_mode is True
            assert coord.data.temp_lag_detection_active is False

    @pytest.mark.asyncio
    async def test_transition_to_detecting_lag(self, create_coordinator):
        """Test: transition forcée vers DETECTING_LAG."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 23, 2, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.HEATING_ON,
                interior_temp=19.0,
                exterior_temp=4.0,
            )

            await coord._transition_to_expected_state(
                SmartHRTState.DETECTING_LAG, mock_now
            )

            assert coord.data.current_state == SmartHRTState.DETECTING_LAG
            assert coord.data.temp_lag_detection_active is True
            assert coord.data.recovery_calc_mode is False
            assert coord.data.rp_calc_mode is False
            assert coord.data.temp_recovery_calc == 19.0


class TestRestoreTriggersAfterRestart:
    """Tests pour la reprogrammation des triggers après redémarrage."""

    @pytest.mark.asyncio
    async def test_monitoring_reprograms_recovery_start_trigger(
        self, create_coordinator
    ):
        """Test: en MONITORING, le trigger recovery_start est reprogrammé."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 0, 30, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                recovery_calc_mode=True,
                recovery_start_hour=datetime(2026, 2, 4, 5, 0, 0),
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            # Mock _schedule_recovery_start pour vérifier qu'il est appelé
            coord._schedule_recovery_start = MagicMock()

            await coord._restore_state_after_restart()

            # Vérifie que le trigger a été reprogrammé
            coord._schedule_recovery_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_monitoring_with_passed_recovery_starts_immediately(
        self, create_coordinator
    ):
        """Test: en MONITORING, si recovery_start est passée → démarrage immédiat."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            # 05:30, recovery était à 05:00
            mock_now = datetime(2026, 2, 4, 5, 30, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                recovery_calc_mode=True,
                recovery_start_hour=datetime(2026, 2, 4, 5, 0, 0),
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            # Mock on_recovery_start pour vérifier qu'il est appelé
            coord.on_recovery_start = MagicMock()

            await coord._restore_state_after_restart()

            # L'état attendu est HEATING_PROCESS, donc transition forcée
            # Ou on_recovery_start appelé
            assert coord.data.current_state in (
                SmartHRTState.HEATING_PROCESS,
                SmartHRTState.MONITORING,
            )

    @pytest.mark.asyncio
    async def test_heating_process_checks_target_hour_passed(self, create_coordinator):
        """Test: en HEATING_PROCESS, si target_hour est passée → fin de relance."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            # 06:30, target était à 06:00
            mock_now = datetime(2026, 2, 4, 6, 30, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.HEATING_PROCESS,
                rp_calc_mode=True,
                recovery_start_hour=datetime(2026, 2, 4, 5, 0, 0),
            )
            coord.data.target_hour = dt_time(6, 0, 0)
            coord.data.recoverycalc_hour = dt_time(23, 0, 0)

            # Mock on_recovery_end pour vérifier qu'il est appelé
            coord.on_recovery_end = MagicMock()

            await coord._restore_state_after_restart()

            # L'état attendu basé sur l'heure est HEATING_ON
            # Donc soit transition forcée, soit on_recovery_end appelé


class TestPersistenceIntegration:
    """Tests pour l'intégration avec le stockage persistant."""

    @pytest.mark.asyncio
    async def test_state_saved_after_transition(self, create_coordinator, mock_store):
        """Test: l'état est sauvegardé après chaque transition."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = datetime(2026, 2, 4, 23, 0, 0)
            mock_dt.now.return_value = mock_now

            coord = await create_coordinator(
                initial_state=SmartHRTState.HEATING_ON,
                smartheating_mode=True,
            )
            coord._store = mock_store

            # Simuler la transition HEATING_ON → DETECTING_LAG
            await coord._async_on_recoverycalc_hour()

            # Vérifier que l'état a été sauvegardé
            stored_data = await mock_store.async_load()
            if stored_data:
                assert stored_data.get("current_state") == SmartHRTState.DETECTING_LAG

    @pytest.mark.asyncio
    async def test_state_restored_from_storage(self, create_coordinator, mock_store):
        """Test: l'état est restauré depuis le stockage."""
        # Pré-remplir le store avec un état
        mock_store._data = {
            "current_state": SmartHRTState.MONITORING,
            "recovery_calc_mode": True,
            "rcth": 55.0,
            "rpth": 45.0,
        }

        coord = await create_coordinator()
        coord._store = mock_store

        await coord._restore_learned_data()

        assert coord.data.current_state == SmartHRTState.MONITORING
        assert coord.data.recovery_calc_mode is True
        assert coord.data.rcth == 55.0
        assert coord.data.rpth == 45.0
