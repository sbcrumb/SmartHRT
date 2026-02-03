"""Tests pour la persistance des prévisions météo.

ADR-020: Persistance des prévisions météo au redémarrage

Ce module teste que les prévisions météo (température et vent)
sont correctement sauvegardées et restaurées lors d'un redémarrage.
"""

from collections import deque
from datetime import datetime, time as dt_time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.SmartHRT.const import (
    DEFAULT_TSP,
    PERSISTED_FIELDS,
)
from custom_components.SmartHRT.coordinator import (
    SmartHRTCoordinator,
    SmartHRTData,
)


class TestWeatherForecastPersistence:
    """Tests pour la persistance des prévisions météo."""

    @pytest.mark.asyncio
    async def test_forecast_fields_in_persisted_fields(self):
        """Vérifier que les champs de prévisions sont dans PERSISTED_FIELDS."""
        field_names = [field[1] for field in PERSISTED_FIELDS]

        assert (
            "temperature_forecast_avg" in field_names
        ), "temperature_forecast_avg doit être dans PERSISTED_FIELDS"
        assert (
            "wind_speed_forecast_avg" in field_names
        ), "wind_speed_forecast_avg doit être dans PERSISTED_FIELDS"

    @pytest.mark.asyncio
    async def test_temperature_forecast_persisted_fields_config(self):
        """Vérifier la configuration du champ temperature_forecast_avg."""
        forecast_field = next(
            (f for f in PERSISTED_FIELDS if f[1] == "temperature_forecast_avg"),
            None,
        )

        assert forecast_field is not None, "temperature_forecast_avg doit être défini"
        storage_key, attr_name, default_value, field_type = forecast_field

        assert storage_key == "temperature_forecast_avg"
        assert attr_name == "temperature_forecast_avg"
        assert default_value == 0.0
        assert field_type == "float"

    @pytest.mark.asyncio
    async def test_wind_speed_forecast_persisted_fields_config(self):
        """Vérifier la configuration du champ wind_speed_forecast_avg."""
        forecast_field = next(
            (f for f in PERSISTED_FIELDS if f[1] == "wind_speed_forecast_avg"),
            None,
        )

        assert forecast_field is not None, "wind_speed_forecast_avg doit être défini"
        storage_key, attr_name, default_value, field_type = forecast_field

        assert storage_key == "wind_speed_forecast_avg"
        assert attr_name == "wind_speed_forecast_avg"
        assert default_value == 0.0
        assert field_type == "float"

    @pytest.mark.asyncio
    async def test_save_and_restore_temperature_forecast(self, create_coordinator):
        """Test: Sauvegarder et restaurer la prévision de température."""
        # Configuration initiale
        coord = await create_coordinator()
        coord.data.temperature_forecast_avg = 12.5

        # Sauvegarder
        await coord._save_learned_data()
        saved_data = await coord._store.async_load()

        assert saved_data is not None
        assert "temperature_forecast_avg" in saved_data
        assert saved_data["temperature_forecast_avg"] == 12.5

        # Créer un nouveau coordinateur et restaurer
        coord2 = await create_coordinator()
        assert coord2.data.temperature_forecast_avg == 0.0  # Valeur par défaut

        await coord2._restore_learned_data()

        assert (
            coord2.data.temperature_forecast_avg == 12.5
        ), "La température de prévision doit être restaurée à 12.5"

    @pytest.mark.asyncio
    async def test_save_and_restore_wind_speed_forecast(self, create_coordinator):
        """Test: Sauvegarder et restaurer la prévision de vitesse vent."""
        # Configuration initiale
        coord = await create_coordinator()
        coord.data.wind_speed_forecast_avg = 45.8

        # Sauvegarder
        await coord._save_learned_data()
        saved_data = await coord._store.async_load()

        assert saved_data is not None
        assert "wind_speed_forecast_avg" in saved_data
        assert saved_data["wind_speed_forecast_avg"] == 45.8

        # Créer un nouveau coordinateur et restaurer
        coord2 = await create_coordinator()
        assert coord2.data.wind_speed_forecast_avg == 0.0  # Valeur par défaut

        await coord2._restore_learned_data()

        assert (
            coord2.data.wind_speed_forecast_avg == 45.8
        ), "La vitesse vent de prévision doit être restaurée à 45.8"

    @pytest.mark.asyncio
    async def test_both_forecasts_persisted_together(self, create_coordinator):
        """Test: Les deux prévisions sont sauvegardées et restaurées ensemble."""
        # Configuration initiale
        coord = await create_coordinator()
        coord.data.temperature_forecast_avg = 8.3
        coord.data.wind_speed_forecast_avg = 32.5

        # Sauvegarder
        await coord._save_learned_data()
        saved_data = await coord._store.async_load()

        assert saved_data["temperature_forecast_avg"] == 8.3
        assert saved_data["wind_speed_forecast_avg"] == 32.5

        # Restaurer
        coord2 = await create_coordinator()
        await coord2._restore_learned_data()

        assert coord2.data.temperature_forecast_avg == 8.3
        assert coord2.data.wind_speed_forecast_avg == 32.5

    @pytest.mark.asyncio
    async def test_forecast_not_lost_on_restart_simulation(self, create_coordinator):
        """Test: Simulation d'un redémarrage ne perd pas les prévisions."""
        # Avant redémarrage: les capteurs affichent des prévisions
        coord = await create_coordinator()
        coord.data.temperature_forecast_avg = 10.2
        coord.data.wind_speed_forecast_avg = 28.4

        # Sauvegarder avant fermeture
        await coord._save_learned_data()

        # Simulation du redémarrage
        # Création d'un nouveau coordinator (comme après redémarrage)
        coord_after_restart = await create_coordinator()
        # Avant restauration, les prévisions sont 0 (défaut)
        assert coord_after_restart.data.temperature_forecast_avg == 0.0
        assert coord_after_restart.data.wind_speed_forecast_avg == 0.0

        # Restauration (appelée au setup)
        await coord_after_restart._restore_learned_data()

        # Après restauration, les prévisions sont correctes
        assert coord_after_restart.data.temperature_forecast_avg == 10.2
        assert coord_after_restart.data.wind_speed_forecast_avg == 28.4

    @pytest.mark.asyncio
    async def test_forecast_defaults_when_no_storage(self, create_coordinator):
        """Test: Si aucune donnée stockée, les prévisions par défaut = 0.0."""
        coord = await create_coordinator()
        # S'assurer que le store est vide
        assert await coord._store.async_load() is None

        # Restaurer (ne devrait rien faire puisque pas de données)
        await coord._restore_learned_data()

        # Les prévisions doivent rester 0.0
        assert coord.data.temperature_forecast_avg == 0.0
        assert coord.data.wind_speed_forecast_avg == 0.0

    @pytest.mark.asyncio
    async def test_forecast_restored_with_other_fields(self, create_coordinator):
        """Test: Prévisions restaurées en même temps que les autres champs."""
        # Configuration initiale complète
        coord = await create_coordinator()
        coord.data.rcth = 55.3
        coord.data.rpth = 45.2
        coord.data.temperature_forecast_avg = 15.0
        coord.data.wind_speed_forecast_avg = 35.0

        # Sauvegarder
        await coord._save_learned_data()

        # Restaurer dans un nouveau coordinator
        coord2 = await create_coordinator()
        await coord2._restore_learned_data()

        # Tous les champs doivent être restaurés
        assert coord2.data.rcth == 55.3
        assert coord2.data.rpth == 45.2
        assert coord2.data.temperature_forecast_avg == 15.0
        assert coord2.data.wind_speed_forecast_avg == 35.0

    @pytest.mark.asyncio
    async def test_forecast_partial_data_in_storage(self, create_coordinator):
        """Test: Si stockage ancien (sans prévisions), les défauts sont utilisés."""
        coord = await create_coordinator()

        # Simuler un ancien stockage sans prévisions (avant ADR-020)
        old_data = {
            "rcth": 52.0,
            "rpth": 48.0,
            # temperature_forecast_avg et wind_speed_forecast_avg absents
        }
        await coord._store.async_save(old_data)

        # Restaurer
        coord2 = await create_coordinator()
        await coord2._restore_learned_data()

        # Les anciens champs sont restaurés
        assert coord2.data.rcth == 52.0
        assert coord2.data.rpth == 48.0

        # Les nouveaux champs sont à leur défaut
        assert coord2.data.temperature_forecast_avg == 0.0
        assert coord2.data.wind_speed_forecast_avg == 0.0
