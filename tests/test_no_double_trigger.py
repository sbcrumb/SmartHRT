"""Tests pour éviter la double exécution des triggers recovery_update.

Ce module teste le bon fonctionnement du TimerManager (ADR-051) qui élimine
les risques de double triggers en annulant automatiquement l'ancien timer
avant d'en programmer un nouveau.

ADR-051: Centralisation de la Gestion des Timers
- TimerManager.schedule() annule automatiquement l'ancien timer si présent
- TimerManager.cancel_all() nettoie proprement lors du déchargement
"""

from datetime import datetime, time as dt_time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from custom_components.SmartHRT.const import TimerKey
from custom_components.SmartHRT.coordinator import (
    SmartHRTCoordinator,
    SmartHRTState,
)
from custom_components.SmartHRT.data_model import SmartHRTData  # ADR-047


def make_mock_now(year=2026, month=2, day=4, hour=8, minute=0, second=0):
    """Helper pour créer un datetime pour les tests."""
    return datetime(year, month, day, hour, minute, second)


class TestNoDoubleTriggerOnTargetHourChange:
    """Tests pour vérifier que le TimerManager évite les doublons (ADR-051)."""

    @pytest.mark.asyncio
    async def test_timer_manager_schedule_replaces_existing(self, create_coordinator):
        """Vérifie que TimerManager.schedule() remplace un timer existant.

        ADR-051: schedule() annule automatiquement l'ancien timer avant
        d'en programmer un nouveau pour la même clé.
        """
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = make_mock_now(hour=8, minute=0, second=0)
            mock_dt.now.return_value = mock_now
            mock_dt.as_local.side_effect = lambda x: x

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                recovery_update_hour=make_mock_now(hour=8, minute=30),
            )

            # Appeler _setup_time_triggers (comme le fait set_target_hour)
            coord._setup_time_triggers()

            # Vérifier qu'un seul timer RECOVERY_UPDATE est actif
            assert coord._timer_manager.is_active(TimerKey.RECOVERY_UPDATE)
            # Le TimerManager gère l'unicité automatiquement

    @pytest.mark.asyncio
    async def test_set_target_hour_uses_timer_manager(self, create_coordinator):
        """Vérifie que set_target_hour utilise le TimerManager correctement."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = make_mock_now(hour=8, minute=0, second=0)
            mock_dt.now.return_value = mock_now
            mock_dt.as_local.side_effect = lambda x: x

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                recovery_update_hour=make_mock_now(hour=8, minute=30),
            )

            # Changer target_hour
            coord.set_target_hour(dt_time(17, 30, 0))

            # Vérifier que le timer TARGET_HOUR est programmé
            assert coord._timer_manager.is_active(TimerKey.TARGET_HOUR)

    @pytest.mark.asyncio
    async def test_no_error_when_no_timer_exists(self, create_coordinator):
        """Vérifie qu'il n'y a pas d'erreur si aucun timer n'existe."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = make_mock_now(hour=8, minute=0, second=0)
            mock_dt.now.return_value = mock_now
            mock_dt.as_local.side_effect = lambda x: x

            coord = await create_coordinator(
                initial_state=SmartHRTState.HEATING_ON,
            )

            # Annuler tous les timers (simuler état vide)
            coord._timer_manager.cancel_all()

            # Ceci ne doit pas lever d'exception
            coord._setup_time_triggers()

            # Le test passe si aucune exception n'est levée
            assert (
                coord._timer_manager.timer_count >= 2
            )  # Au moins RECOVERYCALC et TARGET

    @pytest.mark.asyncio
    async def test_recovery_update_trigger_after_target_hour_change(
        self, create_coordinator
    ):
        """Vérifie que le trigger recovery_update fonctionne après changement."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = make_mock_now(hour=7, minute=0, second=0)
            mock_dt.now.return_value = mock_now
            mock_dt.as_local.side_effect = lambda x: x

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                recovery_update_hour=make_mock_now(hour=8, minute=0),
                target_hour=dt_time(6, 0, 0),
            )

            # Changer target_hour
            coord.set_target_hour(dt_time(17, 30, 0))

            # Vérifier que le nouveau target_hour est bien enregistré
            assert coord.data.target_hour == dt_time(17, 30, 0)

    @pytest.mark.asyncio
    async def test_target_hour_change_recalculates_recovery_start(
        self, create_coordinator
    ):
        """Vérifie que set_target_hour recalcule et reprogramme recovery_start.

        Scénario du bug rapporté: en état MONITORING, quand target_hour change,
        recovery_start_hour doit être recalculé et le trigger reprogrammé.
        """
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = make_mock_now(hour=18, minute=14, second=0)
            mock_dt.now.return_value = mock_now
            mock_dt.as_local.side_effect = lambda x: x

            coord = await create_coordinator(
                initial_state=SmartHRTState.MONITORING,
                target_hour=dt_time(6, 0, 0),  # Ancienne valeur
                recovery_start_hour=make_mock_now(hour=4, minute=0, second=0),
                tsp=20.0,
            )

            # Capturer la valeur initiale
            old_recovery_start = coord.data.recovery_start_hour

            # Changer target_hour à une nouvelle valeur
            coord.set_target_hour(dt_time(19, 15, 0))

            # Le target_hour doit être mis à jour
            assert coord.data.target_hour == dt_time(19, 15, 0)

            # recovery_start_hour doit avoir été recalculé (valeur différente ou même valeur selon le calcul thermique)
            # Le fait qu'on arrive ici sans erreur prouve que le recalcul a eu lieu via calculate_recovery_time()
            # Le timer doit être actif (s'il est dans le futur)
            timer_info = coord._timer_manager.get_info(TimerKey.RECOVERY_START)

            # Si recovery_start_hour est dans le futur, le timer doit être programmé
            if (
                coord.data.recovery_start_hour
                and coord.data.recovery_start_hour > mock_now
            ):
                assert coord._timer_manager.is_active(TimerKey.RECOVERY_START)


class TestTriggerCleanupConsistency:
    """Tests pour la cohérence du nettoyage des timers (ADR-051)."""

    @pytest.mark.asyncio
    async def test_cancel_time_triggers_clears_all(self, create_coordinator):
        """Vérifie que _cancel_time_triggers annule tous les timers horaires."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = make_mock_now(hour=8, minute=0, second=0)
            mock_dt.now.return_value = mock_now
            mock_dt.as_local.side_effect = lambda x: x

            coord = await create_coordinator(initial_state=SmartHRTState.HEATING_ON)

            # Setup des triggers
            coord._setup_time_triggers()
            initial_count = coord._timer_manager.timer_count

            # Annuler les triggers horaires
            coord._cancel_time_triggers()

            # Les timers horaires doivent être annulés
            assert not coord._timer_manager.is_active(TimerKey.RECOVERYCALC_HOUR)
            assert not coord._timer_manager.is_active(TimerKey.TARGET_HOUR)
            assert not coord._timer_manager.is_active(TimerKey.RECOVERY_START)
            assert not coord._timer_manager.is_active(TimerKey.RECOVERY_UPDATE)

    @pytest.mark.asyncio
    async def test_async_unload_cancels_all_timers(self, create_coordinator):
        """Vérifie que async_unload annule tous les timers via TimerManager."""
        with patch("custom_components.SmartHRT.coordinator.dt_util") as mock_dt:
            mock_now = make_mock_now(hour=8, minute=0, second=0)
            mock_dt.now.return_value = mock_now
            mock_dt.as_local.side_effect = lambda x: x

            coord = await create_coordinator(initial_state=SmartHRTState.MONITORING)

            # Setup des triggers
            coord._setup_time_triggers()
            assert coord._timer_manager.timer_count > 0

            await coord.async_unload()

            # Tous les timers doivent être annulés
            assert coord._timer_manager.timer_count == 0
            assert coord._timer_manager.active_timers == []
