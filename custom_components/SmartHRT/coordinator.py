"""Coordinator pour SmartHRT - Gère la logique de chauffage intelligent.

ADR implémentées dans ce module:
- ADR-002: Sélection explicite de l'entité météo (weather_entity_id)
- ADR-003: Machine à états explicite (SmartHRTState)
- ADR-004: Stratégie hybride de persistance (_save/_restore_learned_data)
- ADR-005: Stratégie de pilotage anticipation (via core.ThermalSolver)
- ADR-006: Apprentissage continu (via core.ThermalSolver)
- ADR-007: Compensation météo interpolation vent (via core.ThermalSolver)
- ADR-008: Validation arrêt par détection lag (TEMP_DECREASE_THRESHOLD)
- ADR-009: Persistance coefficients (PERSISTED_FIELDS, Store)
- ADR-013: Historique vent pour calcul (wind_speed_history, wind_speed_avg)
- ADR-014: Format des dates (dt_util.now(), dt_util.as_local())
- ADR-018: Identification instance dans les logs (_log_prefix)
- ADR-019: Restauration état après redémarrage (_restore_state_after_restart)
- ADR-020: Initialisation météo différée (EVENT_HOMEASSISTANT_STARTED)
- ADR-021: Triggers dynamiques (_schedule_recovery_start, async_track_point_in_time)
- ADR-022: Calcul itératif anticipation (via core.ThermalSolver, 20 itérations)
- ADR-023: Protection erreurs setters (try/except dans _schedule_recovery_start)
- ADR-024: Sérialisation types (PERSISTED_FIELDS avec datetime, time, list)
- ADR-025: Fréquence dynamique recalcul (via core.ThermalSolver)
- ADR-026: Extraction modèle thermique (core.ThermalSolver, core.ThermalState)
- ADR-027: Héritage DataUpdateCoordinator (notifications automatiques, CoordinatorEntity)
- ADR-028: Modernisation StrEnum pour machine à états (SmartHRTState, VALID_TRANSITIONS)
- ADR-029: Validation données persistées avec Pydantic (models.py)
- ADR-033: Découplage logique état (SmartHRTStateMachine)
- ADR-034: Gestion centralisée effets de bord (Action handlers)
- ADR-035: Immuabilité état (transitions atomiques)
- ADR-036: Factorisation setters (_update_and_recalculate)
- ADR-037: Suppression polling minute (listener météo push)
- ADR-038: Séparation configuration/état (SmartHRTConfig, StateData, WeatherData, etc.)
- ADR-039: Simplification restauration (auto-correction, _is_state_coherent)
- ADR-040: Délégation flags à la machine à états (propriétés calculées)
- ADR-041: Sérialisation globale via as_dict/from_dict (remplace PERSISTED_FIELDS)
"""

import asyncio
import logging
from datetime import datetime, timedelta, time as dt_time
from dataclasses import dataclass, field
from typing import Any, Callable
from collections import deque

from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import (
    async_track_time_interval,
    async_track_state_change_event,
    async_track_point_in_time,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.util import dt as dt_util
from homeassistant.exceptions import ServiceNotFound

from .const import (
    DOMAIN,
    CONF_NAME,
    CONF_TARGET_HOUR,
    CONF_RECOVERYCALC_HOUR,
    CONF_SENSOR_INTERIOR_TEMP,
    CONF_WEATHER_ENTITY,
    CONF_TSP,
    DEFAULT_TSP,
    DEFAULT_RCTH,
    DEFAULT_RPTH,
    DEFAULT_RELAXATION_FACTOR,
    WIND_HIGH,
    WIND_LOW,
    FORECAST_HOURS,
    TEMP_DECREASE_THRESHOLD,
    DEFAULT_RECOVERYCALC_HOUR,
)
from .serialization import JSONEncoder

# ADR-026: Import du modèle thermique Pure Python
from .core import (
    ThermalSolver,
    ThermalState,
    ThermalCoefficients,
    ThermalConfig,
    Action,
    StateTransitionResult,
    SmartHRTState,
    SmartHRTStateMachine,
    VALID_TRANSITIONS,
    TRANSITION_ACTIONS,
    # ADR-040: get_state_flags n'est plus utilisé, flags sont des propriétés calculées
)

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ADR-038: Sous-dataclasses pour séparation des responsabilités
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SmartHRTConfig:
    """Configuration statique, modifiable via l'UI ou les services (ADR-038)."""

    name: str = "SmartHRT"
    tsp: float = DEFAULT_TSP
    target_hour: dt_time = field(default_factory=lambda: dt_time(6, 0, 0))
    recoverycalc_hour: dt_time = field(default_factory=lambda: dt_time(23, 0, 0))
    smartheating_mode: bool = True
    recovery_adaptive_mode: bool = True


@dataclass
class LearnedCoefficients:
    """Coefficients thermiques appris par le système (ADR-038)."""

    rcth: float = DEFAULT_RCTH
    rpth: float = DEFAULT_RPTH
    rcth_lw: float = DEFAULT_RCTH
    rcth_hw: float = DEFAULT_RCTH
    rpth_lw: float = DEFAULT_RPTH
    rpth_hw: float = DEFAULT_RPTH
    relaxation_factor: float = DEFAULT_RELAXATION_FACTOR


@dataclass
class SmartHRTStateData:
    """État dynamique de la machine à états (ADR-038).

    ADR-040: Les flags (recovery_calc_mode, rp_calc_mode, temp_lag_detection_active)
    ne sont plus stockés ici - ils sont calculés depuis current_state.
    """

    current_state: SmartHRTState = SmartHRTState.HEATING_ON
    # ADR-040: Flags supprimés - maintenant propriétés calculées dans SmartHRTData

    # Snapshots de référence
    time_recovery_calc: datetime | None = None
    time_recovery_start: datetime | None = None
    time_recovery_end: datetime | None = None
    temp_recovery_calc: float = 17.0
    temp_recovery_start: float = 17.0
    temp_recovery_end: float = 17.0
    text_recovery_calc: float = 0.0
    text_recovery_start: float = 0.0
    text_recovery_end: float = 0.0

    # Triggers programmés
    recovery_start_hour: datetime | None = None
    recovery_update_hour: datetime | None = None

    # Délai de lag avant baisse température
    stop_lag_duration: float = 0.0


@dataclass
class WeatherData:
    """Données météorologiques actuelles et prévisions (ADR-038)."""

    interior_temp: float | None = None
    exterior_temp: float | None = None
    wind_speed: float = 0.0  # m/s
    windchill: float | None = None
    wind_speed_avg: float = 0.0  # m/s
    wind_speed_forecast_avg: float = 0.0  # km/h
    temperature_forecast_avg: float = 0.0  # °C
    # ADR-037: Réduit à 50 samples
    wind_speed_history: deque = field(default_factory=lambda: deque(maxlen=50))


@dataclass
class DiagnosticData:
    """Données de diagnostic et métriques calculées (ADR-038)."""

    rcth_fast: float = 0.0
    rcth_calculated: float = 0.0
    rpth_calculated: float = 0.0
    last_rcth_error: float = 0.0
    last_rpth_error: float = 0.0


class SmartHRTData:
    """Données du système SmartHRT (ADR-038: composition de sous-structures).

    Structure organisée par responsabilité:
    - config: Configuration utilisateur (statique)
    - coefficients: Coefficients thermiques appris (évoluent lentement)
    - state: État dynamique de la machine à états
    - weather: Données météo actuelles
    - diagnostic: Métriques de diagnostic (lecture seule)

    Les champs restent accessibles directement (self.data.tsp) pour
    compatibilité, tout en étant organisés en groupes logiques.
    Supporte l'initialisation avec les anciens kwargs (name=, tsp=, etc.)
    pour compatibilité ascendante.
    """

    def __init__(
        self,
        *,
        # Nouveaux kwargs (sous-structures)
        config: SmartHRTConfig | None = None,
        coefficients: LearnedCoefficients | None = None,
        state: SmartHRTStateData | None = None,
        weather: WeatherData | None = None,
        diagnostic: DiagnosticData | None = None,
        # Anciens kwargs (compatibilité) - config
        name: str | None = None,
        tsp: float | None = None,
        target_hour: dt_time | None = None,
        recoverycalc_hour: dt_time | None = None,
        smartheating_mode: bool | None = None,
        recovery_adaptive_mode: bool | None = None,
    ) -> None:
        """Initialise SmartHRTData avec support des anciens et nouveaux kwargs."""
        # Crée les sous-structures avec valeurs par défaut
        self.config = config if config is not None else SmartHRTConfig()
        self.coefficients = (
            coefficients if coefficients is not None else LearnedCoefficients()
        )
        self.state = state if state is not None else SmartHRTStateData()
        self.weather = weather if weather is not None else WeatherData()
        self.diagnostic = diagnostic if diagnostic is not None else DiagnosticData()

        # Applique les anciens kwargs sur la config si fournis
        if name is not None:
            self.config.name = name
        if tsp is not None:
            self.config.tsp = tsp
        if target_hour is not None:
            self.config.target_hour = target_hour
        if recoverycalc_hour is not None:
            self.config.recoverycalc_hour = recoverycalc_hour
        if smartheating_mode is not None:
            self.config.smartheating_mode = smartheating_mode
        if recovery_adaptive_mode is not None:
            self.config.recovery_adaptive_mode = recovery_adaptive_mode

    def update(self, **kwargs: Any) -> "SmartHRTData":
        """Met à jour les attributs en place et retourne self.

        Permet de remplacer dataclasses.replace() pour SmartHRTData.
        Usage: self.data.update(current_state=X, temp=Y)
        """
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    # ─────────────────────────────────────────────────────────────────────────
    # Propriétés de compatibilité vers config
    # ─────────────────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self.config.name

    @name.setter
    def name(self, value: str) -> None:
        self.config.name = value

    @property
    def tsp(self) -> float:
        return self.config.tsp

    @tsp.setter
    def tsp(self, value: float) -> None:
        self.config.tsp = value

    @property
    def target_hour(self) -> dt_time:
        return self.config.target_hour

    @target_hour.setter
    def target_hour(self, value: dt_time) -> None:
        self.config.target_hour = value

    @property
    def recoverycalc_hour(self) -> dt_time:
        return self.config.recoverycalc_hour

    @recoverycalc_hour.setter
    def recoverycalc_hour(self, value: dt_time) -> None:
        self.config.recoverycalc_hour = value

    @property
    def smartheating_mode(self) -> bool:
        return self.config.smartheating_mode

    @smartheating_mode.setter
    def smartheating_mode(self, value: bool) -> None:
        self.config.smartheating_mode = value

    @property
    def recovery_adaptive_mode(self) -> bool:
        return self.config.recovery_adaptive_mode

    @recovery_adaptive_mode.setter
    def recovery_adaptive_mode(self, value: bool) -> None:
        self.config.recovery_adaptive_mode = value

    # ─────────────────────────────────────────────────────────────────────────
    # Propriétés de compatibilité vers coefficients
    # ─────────────────────────────────────────────────────────────────────────
    @property
    def rcth(self) -> float:
        return self.coefficients.rcth

    @rcth.setter
    def rcth(self, value: float) -> None:
        self.coefficients.rcth = value

    @property
    def rpth(self) -> float:
        return self.coefficients.rpth

    @rpth.setter
    def rpth(self, value: float) -> None:
        self.coefficients.rpth = value

    @property
    def rcth_lw(self) -> float:
        return self.coefficients.rcth_lw

    @rcth_lw.setter
    def rcth_lw(self, value: float) -> None:
        self.coefficients.rcth_lw = value

    @property
    def rcth_hw(self) -> float:
        return self.coefficients.rcth_hw

    @rcth_hw.setter
    def rcth_hw(self, value: float) -> None:
        self.coefficients.rcth_hw = value

    @property
    def rpth_lw(self) -> float:
        return self.coefficients.rpth_lw

    @rpth_lw.setter
    def rpth_lw(self, value: float) -> None:
        self.coefficients.rpth_lw = value

    @property
    def rpth_hw(self) -> float:
        return self.coefficients.rpth_hw

    @rpth_hw.setter
    def rpth_hw(self, value: float) -> None:
        self.coefficients.rpth_hw = value

    @property
    def relaxation_factor(self) -> float:
        return self.coefficients.relaxation_factor

    @relaxation_factor.setter
    def relaxation_factor(self, value: float) -> None:
        self.coefficients.relaxation_factor = value

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-040: Flags calculés depuis current_state (propriétés en lecture seule)
    # ─────────────────────────────────────────────────────────────────────────
    @property
    def current_state(self) -> SmartHRTState:
        return self.state.current_state

    @current_state.setter
    def current_state(self, value: SmartHRTState) -> None:
        self.state.current_state = value

    @property
    def recovery_calc_mode(self) -> bool:
        """True si en état MONITORING (calculs de refroidissement actifs)."""
        return self.state.current_state == SmartHRTState.MONITORING

    @property
    def rp_calc_mode(self) -> bool:
        """True si en état HEATING_PROCESS (calculs de relance actifs)."""
        return self.state.current_state == SmartHRTState.HEATING_PROCESS

    @property
    def temp_lag_detection_active(self) -> bool:
        """True si en état DETECTING_LAG (surveillance de baisse température)."""
        return self.state.current_state == SmartHRTState.DETECTING_LAG

    @property
    def time_recovery_calc(self) -> datetime | None:
        return self.state.time_recovery_calc

    @time_recovery_calc.setter
    def time_recovery_calc(self, value: datetime | None) -> None:
        self.state.time_recovery_calc = value

    @property
    def time_recovery_start(self) -> datetime | None:
        return self.state.time_recovery_start

    @time_recovery_start.setter
    def time_recovery_start(self, value: datetime | None) -> None:
        self.state.time_recovery_start = value

    @property
    def time_recovery_end(self) -> datetime | None:
        return self.state.time_recovery_end

    @time_recovery_end.setter
    def time_recovery_end(self, value: datetime | None) -> None:
        self.state.time_recovery_end = value

    @property
    def temp_recovery_calc(self) -> float:
        return self.state.temp_recovery_calc

    @temp_recovery_calc.setter
    def temp_recovery_calc(self, value: float) -> None:
        self.state.temp_recovery_calc = value

    @property
    def temp_recovery_start(self) -> float:
        return self.state.temp_recovery_start

    @temp_recovery_start.setter
    def temp_recovery_start(self, value: float) -> None:
        self.state.temp_recovery_start = value

    @property
    def temp_recovery_end(self) -> float:
        return self.state.temp_recovery_end

    @temp_recovery_end.setter
    def temp_recovery_end(self, value: float) -> None:
        self.state.temp_recovery_end = value

    @property
    def text_recovery_calc(self) -> float:
        return self.state.text_recovery_calc

    @text_recovery_calc.setter
    def text_recovery_calc(self, value: float) -> None:
        self.state.text_recovery_calc = value

    @property
    def text_recovery_start(self) -> float:
        return self.state.text_recovery_start

    @text_recovery_start.setter
    def text_recovery_start(self, value: float) -> None:
        self.state.text_recovery_start = value

    @property
    def text_recovery_end(self) -> float:
        return self.state.text_recovery_end

    @text_recovery_end.setter
    def text_recovery_end(self, value: float) -> None:
        self.state.text_recovery_end = value

    @property
    def recovery_start_hour(self) -> datetime | None:
        return self.state.recovery_start_hour

    @recovery_start_hour.setter
    def recovery_start_hour(self, value: datetime | None) -> None:
        self.state.recovery_start_hour = value

    @property
    def recovery_update_hour(self) -> datetime | None:
        return self.state.recovery_update_hour

    @recovery_update_hour.setter
    def recovery_update_hour(self, value: datetime | None) -> None:
        self.state.recovery_update_hour = value

    @property
    def stop_lag_duration(self) -> float:
        return self.state.stop_lag_duration

    @stop_lag_duration.setter
    def stop_lag_duration(self, value: float) -> None:
        self.state.stop_lag_duration = value

    # ─────────────────────────────────────────────────────────────────────────
    # Propriétés de compatibilité vers weather
    # ─────────────────────────────────────────────────────────────────────────
    @property
    def interior_temp(self) -> float | None:
        return self.weather.interior_temp

    @interior_temp.setter
    def interior_temp(self, value: float | None) -> None:
        self.weather.interior_temp = value

    @property
    def exterior_temp(self) -> float | None:
        return self.weather.exterior_temp

    @exterior_temp.setter
    def exterior_temp(self, value: float | None) -> None:
        self.weather.exterior_temp = value

    @property
    def wind_speed(self) -> float:
        return self.weather.wind_speed

    @wind_speed.setter
    def wind_speed(self, value: float) -> None:
        self.weather.wind_speed = value

    @property
    def windchill(self) -> float | None:
        return self.weather.windchill

    @windchill.setter
    def windchill(self, value: float | None) -> None:
        self.weather.windchill = value

    @property
    def wind_speed_avg(self) -> float:
        return self.weather.wind_speed_avg

    @wind_speed_avg.setter
    def wind_speed_avg(self, value: float) -> None:
        self.weather.wind_speed_avg = value

    @property
    def wind_speed_forecast_avg(self) -> float:
        return self.weather.wind_speed_forecast_avg

    @wind_speed_forecast_avg.setter
    def wind_speed_forecast_avg(self, value: float) -> None:
        self.weather.wind_speed_forecast_avg = value

    @property
    def temperature_forecast_avg(self) -> float:
        return self.weather.temperature_forecast_avg

    @temperature_forecast_avg.setter
    def temperature_forecast_avg(self, value: float) -> None:
        self.weather.temperature_forecast_avg = value

    @property
    def wind_speed_history(self) -> deque:
        return self.weather.wind_speed_history

    @wind_speed_history.setter
    def wind_speed_history(self, value: deque) -> None:
        self.weather.wind_speed_history = value

    # ─────────────────────────────────────────────────────────────────────────
    # Propriétés de compatibilité vers diagnostic
    # ─────────────────────────────────────────────────────────────────────────
    @property
    def rcth_fast(self) -> float:
        return self.diagnostic.rcth_fast

    @rcth_fast.setter
    def rcth_fast(self, value: float) -> None:
        self.diagnostic.rcth_fast = value

    @property
    def rcth_calculated(self) -> float:
        return self.diagnostic.rcth_calculated

    @rcth_calculated.setter
    def rcth_calculated(self, value: float) -> None:
        self.diagnostic.rcth_calculated = value

    @property
    def rpth_calculated(self) -> float:
        return self.diagnostic.rpth_calculated

    @rpth_calculated.setter
    def rpth_calculated(self, value: float) -> None:
        self.diagnostic.rpth_calculated = value

    @property
    def last_rcth_error(self) -> float:
        return self.diagnostic.last_rcth_error

    @last_rcth_error.setter
    def last_rcth_error(self, value: float) -> None:
        self.diagnostic.last_rcth_error = value

    @property
    def last_rpth_error(self) -> float:
        return self.diagnostic.last_rpth_error

    @last_rpth_error.setter
    def last_rpth_error(self, value: float) -> None:
        self.diagnostic.last_rpth_error = value

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-041: Sérialisation centralisée via as_dict/from_dict
    # ─────────────────────────────────────────────────────────────────────────

    # Champs persistés (ADR-041: remplace PERSISTED_FIELDS)
    _PERSISTENT_FIELDS: set[str] = {
        # Coefficients thermiques
        "rcth",
        "rpth",
        "rcth_lw",
        "rcth_hw",
        "rpth_lw",
        "rpth_hw",
        "last_rcth_error",
        "last_rpth_error",
        # État machine
        "current_state",
        "stop_lag_duration",
        # Heures configurées
        "target_hour",
        "recoverycalc_hour",
        # Snapshots de session
        "time_recovery_calc",
        "temp_recovery_calc",
        "text_recovery_calc",
        # Triggers programmés
        "recovery_start_hour",
        # Prévisions météo
        "temperature_forecast_avg",
        "wind_speed_forecast_avg",
        # Historique vent
        "wind_speed_history",
    }

    # Mapping des champs enum pour décodage
    _ENUM_FIELDS: dict[str, type] = {}

    def as_dict(self) -> dict[str, Any]:
        """Sérialise les données persistantes en dictionnaire JSON-compatible.

        ADR-041: Centralise la sérialisation, remplace PERSISTED_FIELDS.

        Returns:
            Dictionnaire avec toutes les données à persister.
        """
        result: dict[str, Any] = {}
        for field_name in self._PERSISTENT_FIELDS:
            value = getattr(self, field_name, None)
            result[field_name] = JSONEncoder.encode(value)
        return result

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        defaults: "SmartHRTData | None" = None,
        enum_types: dict[str, type] | None = None,
    ) -> "SmartHRTData":
        """Désérialise un dictionnaire en SmartHRTData.

        ADR-041: Centralise la désérialisation.

        Args:
            data: Dictionnaire JSON chargé depuis le stockage
            defaults: Instance par défaut pour les champs manquants
            enum_types: Mapping des champs enum vers leurs types

        Returns:
            Nouvelle instance SmartHRTData avec données restaurées.
        """
        if defaults is None:
            defaults = cls()
        if enum_types is None:
            enum_types = cls._ENUM_FIELDS

        # Importer SmartHRTState localement pour éviter import circulaire
        from .core import SmartHRTState

        enum_types = {"current_state": SmartHRTState, **enum_types}

        # Crée une nouvelle instance basée sur defaults
        instance = cls(
            config=SmartHRTConfig(
                name=defaults.config.name,
                tsp=defaults.config.tsp,
                target_hour=defaults.config.target_hour,
                recoverycalc_hour=defaults.config.recoverycalc_hour,
                smartheating_mode=defaults.config.smartheating_mode,
                recovery_adaptive_mode=defaults.config.recovery_adaptive_mode,
            )
        )

        # Restaurer les champs depuis data
        for field_name in cls._PERSISTENT_FIELDS:
            if field_name in data:
                stored_value = data[field_name]
                expected_type = enum_types.get(field_name)
                decoded = JSONEncoder.decode(stored_value, expected_type)
                if decoded is not None:
                    setattr(instance, field_name, decoded)
            else:
                # Utiliser la valeur par défaut
                default_value = getattr(defaults, field_name, None)
                if default_value is not None:
                    setattr(instance, field_name, default_value)

        return instance

    @classmethod
    def migrate_legacy_format(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Migre les données de l'ancien format PERSISTED_FIELDS.

        Détecte l'ancien format (pas de __type__) et convertit au nouveau.

        Args:
            data: Données au format legacy

        Returns:
            Données au nouveau format avec __type__
        """
        # Import local pour éviter import circulaire
        from .core import SmartHRTState

        # Mapping ancien format vers nouveau
        legacy_types = {
            "target_hour": "time",
            "recoverycalc_hour": "time",
            "recovery_start_hour": "datetime",
            "time_recovery_calc": "datetime",
            "current_state": "state",
            "wind_speed_history": "list",
        }

        migrated: dict[str, Any] = {}
        for key, value in data.items():
            if value is None:
                migrated[key] = None
                continue

            field_type = legacy_types.get(key)
            if field_type == "datetime" and isinstance(value, str):
                migrated[key] = {"__type__": "datetime", "value": value}
            elif field_type == "time" and isinstance(value, str):
                migrated[key] = {"__type__": "time", "value": value}
            elif field_type == "state" and isinstance(value, str):
                migrated[key] = {"__type__": "enum", "value": value}
            elif field_type == "list" and isinstance(value, list):
                migrated[key] = {"__type__": "deque", "value": value, "maxlen": 50}
            else:
                # Valeurs primitives (float, bool, str) - pas de changement
                migrated[key] = value

        return migrated


class SmartHRTCoordinator(DataUpdateCoordinator[SmartHRTData]):
    """Coordinateur central pour SmartHRT (ADR-027: hérite de DataUpdateCoordinator).

    Hérite de DataUpdateCoordinator pour bénéficier de:
    - Gestion automatique des listeners via CoordinatorEntity
    - Debouncing des mises à jour
    - Gestion d'erreurs standardisée
    - Logging intégré
    """

    STORAGE_VERSION = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Initialiser DataUpdateCoordinator avec update_interval=None
        # car les mises à jour sont pilotées par les triggers et événements
        super().__init__(
            hass,
            _LOGGER,
            name=f"SmartHRT {entry.data.get(CONF_NAME, 'SmartHRT')}",
            update_interval=None,  # Pas de polling automatique, mises à jour manuelles
        )

        self._entry = entry
        self._unsub_listeners: list = []
        self._unsub_time_triggers: list = []
        self._unsub_recovery_update: Callable | None = (
            None  # Tracker pour recovery_update
        )
        self._unsub_recovery_start: Callable | None = (
            None  # Tracker pour recovery_start (corrige le bug yoyo)
        )
        # ADR-004 & ADR-009: Stratégie hybride de persistance
        # Les coefficients appris (RCth, RPth) et l'état survivent aux redémarrages
        self._store: Store = Store(
            hass, self.STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )

        self.data = SmartHRTData(
            name=entry.data.get(CONF_NAME, "SmartHRT"),
            tsp=entry.data.get(CONF_TSP, DEFAULT_TSP),
            target_hour=self._parse_time(entry.data.get(CONF_TARGET_HOUR, "06:00:00")),
            recoverycalc_hour=self._parse_time(
                entry.data.get(CONF_RECOVERYCALC_HOUR, DEFAULT_RECOVERYCALC_HOUR)
            ),
        )

        # ADR-033/034/046: Machine à états avec actions déclaratives
        log_prefix = f"[{self.data.name}#{entry.entry_id[:8]}]"
        self._state_machine = SmartHRTStateMachine(
            self.data.current_state,
            transition_actions=TRANSITION_ACTIONS,
            logger=_LOGGER,
            log_prefix=log_prefix,
        )
        self._state_machine.on_enter(SmartHRTState.HEATING_ON, self._on_state_entered)
        self._state_machine.on_enter(
            SmartHRTState.DETECTING_LAG, self._on_state_entered
        )
        self._state_machine.on_enter(SmartHRTState.MONITORING, self._on_state_entered)
        self._state_machine.on_enter(SmartHRTState.RECOVERY, self._on_state_entered)
        self._state_machine.on_enter(
            SmartHRTState.HEATING_PROCESS, self._on_state_entered
        )

        self._interior_temp_sensor_id = entry.data.get(CONF_SENSOR_INTERIOR_TEMP)
        # ADR-002: Entité météo sélectionnée explicitement par l'utilisateur
        self._weather_entity_id = entry.data.get(CONF_WEATHER_ENTITY)

        # ADR-026: Initialisation du solveur thermique Pure Python
        thermal_config = ThermalConfig(
            wind_low_kmh=WIND_LOW,
            wind_high_kmh=WIND_HIGH,
            default_rcth=DEFAULT_RCTH,
            default_rpth=DEFAULT_RPTH,
            default_relaxation_factor=DEFAULT_RELAXATION_FACTOR,
        )
        self._thermal_solver = ThermalSolver(thermal_config, logger=_LOGGER)

    def _log_prefix(self) -> str:
        """Retourne un préfixe pour les logs incluant le nom et entry_id de l'instance.

        Permet de dissocier les entrées de log quand plusieurs instances SmartHRT
        sont configurées, en incluant le nom et l'identifiant unique.
        """
        return f"[{self.data.name}#{self._entry.entry_id[:8]}]"

    def _on_state_entered(
        self, _old_state: SmartHRTState, new_state: SmartHRTState
    ) -> None:
        """Synchronise l'état exposé avec la machine à états."""
        self.data.current_state = new_state

    def transition_to(self, new_state: SmartHRTState) -> bool:
        """Effectue une transition d'état si elle est valide (ADR-028)."""
        current = self._state_machine.state
        valid_targets = VALID_TRANSITIONS.get(current, set())

        if self._state_machine.transition_to(new_state):
            _LOGGER.info(
                "%s Transition %s → %s",
                self._log_prefix(),
                current.value,
                new_state.value,
            )
            return True

        _LOGGER.warning(
            "%s Transition invalide %s → %s (autorisées: %s)",
            self._log_prefix(),
            current.value,
            new_state.value,
            ", ".join(s.value for s in valid_targets) if valid_targets else "aucune",
        )
        return False

    def _apply_state_transition_with_actions(
        self,
        new_state: SmartHRTState,
        updates: dict[str, object] | None = None,
        omit_actions: set[Action] | None = None,
    ) -> list[Action] | None:
        current = self._state_machine.state
        valid_targets = VALID_TRANSITIONS.get(current, set())
        if not self._state_machine.can_transition(current, new_state):
            _LOGGER.warning(
                "%s Transition invalide %s → %s (autorisées: %s)",
                self._log_prefix(),
                current.value,
                new_state.value,
                (
                    ", ".join(s.value for s in valid_targets)
                    if valid_targets
                    else "aucune"
                ),
            )
            return None

        # ADR-040: Les flags sont maintenant des propriétés calculées depuis current_state
        # On ne merge plus get_state_flags(), juste les updates fournis
        self.data.update(current_state=new_state, **(updates or {}))
        self._state_machine.force_state(new_state, run_callbacks=False)

        actions = self._state_machine.actions_for_transition(current, new_state)
        if omit_actions:
            actions = [action for action in actions if action not in omit_actions]
        return actions

    def transition_with_actions(
        self, new_state: SmartHRTState
    ) -> StateTransitionResult:
        """Effectue une transition et retourne les actions à exécuter (ADR-034)."""
        current = self._state_machine.state
        valid_targets = VALID_TRANSITIONS.get(current, set())
        result = self._state_machine.transition_with_actions(new_state)

        if result.success:
            _LOGGER.info(
                "%s Transition %s → %s",
                self._log_prefix(),
                current.value,
                new_state.value,
            )
            return result

        _LOGGER.warning(
            "%s Transition invalide %s → %s (autorisées: %s)",
            self._log_prefix(),
            current.value,
            new_state.value,
            ", ".join(s.value for s in valid_targets) if valid_targets else "aucune",
        )
        return result

    def _execute_actions(self, actions: list[Action]) -> None:
        """Exécute les actions émises par la machine à états (ADR-034).

        Gère les handlers sync et async avec logging et gestion d'erreurs.
        """
        if not actions:
            return

        action_handlers = {
            Action.SNAPSHOT_RECOVERY_START: self._snapshot_recovery_start,
            Action.SNAPSHOT_RECOVERY_END: self._snapshot_recovery_end,
            Action.CALCULATE_RCTH: self._calculate_rcth_at_recovery_start,
            Action.CALCULATE_RPTH: self._calculate_rpth_at_recovery_end,
            Action.SAVE_DATA: self._save_learned_data,
            Action.SCHEDULE_RECOVERY_UPDATE: self._schedule_recovery_update_from_data,
            Action.CANCEL_RECOVERY_TIMER: self._cancel_recovery_start_timer,
        }

        _LOGGER.debug(
            "%s Exécution actions: %s",
            self._log_prefix(),
            [a.value for a in actions],
        )

        for action in actions:
            handler = action_handlers.get(action)
            if not handler:
                _LOGGER.warning(
                    "%s Action non gérée: %s", self._log_prefix(), action.value
                )
                continue
            try:
                result = handler()
                if asyncio.iscoroutine(result):
                    self.hass.async_create_task(result)
            except Exception as e:
                _LOGGER.error(
                    "%s Erreur lors de l'action %s: %s",
                    self._log_prefix(),
                    action.value,
                    e,
                )

    def _cancel_recovery_start_timer(self) -> None:
        """Annule le trigger de recovery_start si présent."""
        if self._unsub_recovery_start:
            self._unsub_recovery_start()
            self._unsub_recovery_start = None

    def _snapshot_recovery_start(self) -> None:
        """Snapshot des données au démarrage de relance."""
        self.data.time_recovery_start = dt_util.now()
        self.data.temp_recovery_start = self.data.interior_temp or 17.0
        self.data.text_recovery_start = self.data.exterior_temp or 0.0

    def _snapshot_recovery_end(self) -> None:
        """Snapshot des données à la fin de relance."""
        self.data.time_recovery_end = dt_util.now()
        self.data.temp_recovery_end = self.data.interior_temp or 17.0
        self.data.text_recovery_end = self.data.exterior_temp or 0.0

    def _calculate_rcth_at_recovery_start(self) -> None:
        """Calcule RCth au démarrage de relance."""
        self.calculate_rcth_at_recovery_start()

    def _calculate_rpth_at_recovery_end(self) -> None:
        """Calcule RPth à la fin de relance."""
        self.calculate_rpth_at_recovery_end()

    def _schedule_recovery_update_from_data(self) -> None:
        """Programme la mise à jour de relance si une heure est connue."""
        if self.data.recovery_update_hour:
            self._schedule_recovery_update(self.data.recovery_update_hour)

    def force_state(self, new_state: SmartHRTState) -> None:
        """Force un changement d'état sans validation (ADR-028, ADR-035).

        Met à jour atomiquement l'état et les flags associés.
        À utiliser avec parcimonie (restauration, services admin).
        """
        old_state = self._state_machine.state
        if new_state == old_state:
            return

        # ADR-040: Les flags sont maintenant calculés depuis current_state
        # Seul current_state est mis à jour
        self.data.update(current_state=new_state)
        self._state_machine.force_state(new_state, run_callbacks=False)

        _LOGGER.info(
            "%s État forcé %s → %s",
            self._log_prefix(),
            old_state.value,
            new_state.value,
        )

    async def _async_update_data(self) -> SmartHRTData:
        """Méthode requise par DataUpdateCoordinator.

        Retourne les données actuelles car les mises à jour sont pilotées
        par les événements et triggers, pas par le polling.
        """
        return self.data

    @staticmethod
    def _parse_time(time_str: str) -> dt_time:
        """Parse une chaîne de temps en objet time"""
        try:
            parts = time_str.split(":")
            return dt_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except (ValueError, IndexError):
            return dt_time(6, 0, 0)

    # ─────────────────────────────────────────────────────────────────────────
    # Setup / Unload
    # ─────────────────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Configuration asynchrone du coordinateur.

        PERF: Les opérations bloquantes (météo, calculs) sont différées
        via async_create_task pour ne pas bloquer le setup de l'entrée.
        """
        _LOGGER.debug(
            "%s Configuration - TSP=%.1f°C, target_hour=%s, recoverycalc_hour=%s",
            self._log_prefix(),
            self.data.tsp,
            self.data.target_hour,
            self.data.recoverycalc_hour,
        )

        # Restore learned coefficients from storage (rapide, lecture locale)
        await self._restore_learned_data()

        await self._update_initial_states()
        self._setup_listeners()
        self._setup_time_triggers()

        # Restaurer les triggers selon l'état (ne dépend pas de la météo)
        await self._restore_state_after_restart()

        # PERF: Différer l'initialisation météo pour ne pas bloquer le setup
        # Les opérations météo (service calls) peuvent prendre >1s
        if self._weather_entity_id:
            weather = self.hass.states.get(self._weather_entity_id)
            if weather is None:
                _LOGGER.debug(
                    "%s Entité météo %s pas encore disponible, initialisation différée",
                    self._log_prefix(),
                    self._weather_entity_id,
                )
                # Différer l'initialisation météo après le démarrage complet
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._on_homeassistant_started
                )
            else:
                # Entité météo disponible - lancer en tâche de fond (non-bloquant)
                self.hass.async_create_task(
                    self._complete_weather_setup(),
                    name=f"SmartHRT {self.data.name} weather setup",
                )
        else:
            # Pas d'entité météo configurée - setup minimal en tâche de fond
            self.hass.async_create_task(
                self._complete_weather_setup(),
                name=f"SmartHRT {self.data.name} weather setup",
            )

    async def _on_homeassistant_started(self, event: Event) -> None:
        """Callback appelé après le démarrage complet de Home Assistant.

        Permet d'initialiser les fonctionnalités météo une fois que toutes
        les intégrations sont chargées.
        """
        _LOGGER.info(
            "%s Home Assistant démarré, initialisation météo de %s",
            self._log_prefix(),
            self._weather_entity_id,
        )
        await self._complete_weather_setup()

    async def _complete_weather_setup(self) -> None:
        """Termine l'initialisation des fonctionnalités dépendantes de la météo."""
        await self._update_weather_forecasts()

        # Calcul initial de l'heure de relance
        await self.hass.async_add_executor_job(self.calculate_recovery_time)

        # Programmer le trigger de relance si nécessaire
        now = dt_util.now()
        if self.data.recovery_start_hour and self.data.recovery_start_hour > now:
            self._schedule_recovery_start(self.data.recovery_start_hour)

        # Programmer la première mise à jour de recovery_update_hour
        # Le trigger est toujours programmé pour maintenir la chaîne de mises à jour active
        if self.data.smartheating_mode and self.data.recovery_start_hour:
            update_time = await self.hass.async_add_executor_job(
                self.calculate_recovery_update_time
            )
            if update_time:
                self.data.recovery_update_hour = update_time
                self._schedule_recovery_update(update_time)

    async def _restore_learned_data(self) -> None:
        """Restore learned coefficients and state from persistent storage.

        ADR-004: Stratégie hybride de persistance
        ADR-009: Persistance des coefficients thermiques
        ADR-029: Validation des données avec Pydantic
        ADR-041: Sérialisation centralisée via as_dict/from_dict

        This ensures that learned thermal constants (RCth, RPth) and the
        current state machine state survive Home Assistant restarts.
        """
        stored_data = await self._store.async_load()
        if stored_data:
            _LOGGER.info(
                "%s Restoration des données apprises depuis le stockage",
                self._log_prefix(),
            )

            # ADR-041: Détecter et migrer l'ancien format si nécessaire
            # L'ancien format n'a pas de __type__ dans les valeurs complexes
            if self._is_legacy_format(stored_data):
                _LOGGER.debug("%s Migration depuis l'ancien format", self._log_prefix())
                stored_data = SmartHRTData.migrate_legacy_format(stored_data)

            # ADR-029: Validation Pydantic des données persistées
            from .models import validate_persisted_data

            validated_data = validate_persisted_data(
                self._decode_stored_data(stored_data)
            )

            # ADR-041: Désérialisation centralisée avec données validées
            self.data = SmartHRTData.from_dict(stored_data, defaults=self.data)

            _LOGGER.debug(
                "%s Données restaurées: state=%s, rcth=%.2f, rpth=%.2f, recovery_calc_mode=%s, temp_forecast=%.1f°C, wind_forecast=%.1f",
                self._log_prefix(),
                self.data.current_state,
                self.data.rcth,
                self.data.rpth,
                self.data.recovery_calc_mode,
                self.data.temperature_forecast_avg,
                self.data.wind_speed_forecast_avg,
            )
        else:
            _LOGGER.debug(
                "%s Aucune donnée apprise trouvée, utilisation des défauts",
                self._log_prefix(),
            )

        self._state_machine.force_state(self.data.current_state)

    def _decode_stored_data(self, stored_data: dict) -> dict:
        """Décode les données stockées en types Python natifs pour validation.

        ADR-029: Prépare les données pour validation Pydantic.
        """
        decoded = {}
        for key, value in stored_data.items():
            if isinstance(value, dict) and "__type__" in value:
                decoded[key] = JSONEncoder.decode(value)
            else:
                decoded[key] = value
        return decoded

    def _is_legacy_format(self, data: dict) -> bool:
        """Détecte si les données sont au format legacy (PERSISTED_FIELDS).

        Le nouveau format utilise des dict avec __type__ pour datetime, time, etc.
        L'ancien format stocke directement les strings ISO.
        """
        # Si current_state est une string simple (pas un dict), c'est l'ancien format
        current_state = data.get("current_state")
        if isinstance(current_state, str):
            return True
        # Si c'est un dict avec __type__, c'est le nouveau format
        if isinstance(current_state, dict) and "__type__" in current_state:
            return False
        # Par défaut, considérer comme nouveau format
        return False

    async def _save_learned_data(self) -> None:
        """Save learned coefficients and state to persistent storage.

        ADR-041: Sérialisation centralisée via as_dict/from_dict.

        Called after each state transition and learning cycle to persist
        the updated coefficients and state.
        """
        data_to_store = self.data.as_dict()
        await self._store.async_save(data_to_store)
        _LOGGER.debug(
            "%s Données apprises et état sauvegardés en stockage", self._log_prefix()
        )

    def _setup_listeners(self) -> None:
        """Configure les listeners pour les capteurs.

        ADR-037: Architecture push (pilotée par événements) :
        - Listener sur le capteur de température intérieure
        - Listener sur l'entité météo (remplace le polling minute)
        - Mise à jour horaire des prévisions météo
        """
        sensors = [s for s in [self._interior_temp_sensor_id] if s]

        if sensors:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass, sensors, self._on_sensor_state_change
                )
            )

        # ADR-037: Listener sur l'entité météo (remplace le polling minute)
        if self._weather_entity_id:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass, [self._weather_entity_id], self._on_weather_state_change
                )
            )

        # Update weather forecasts every hour
        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass, self._hourly_forecast_update, timedelta(hours=1)
            )
        )

    def _setup_time_triggers(self) -> None:
        """Configure les déclencheurs horaires selon le YAML"""
        self._cancel_time_triggers()

        now = dt_util.now()

        # Trigger pour recoverycalc_hour (arrêt chauffage le soir)
        recoverycalc_dt = now.replace(
            hour=self.data.recoverycalc_hour.hour,
            minute=self.data.recoverycalc_hour.minute,
            second=0,
            microsecond=0,
        )
        if recoverycalc_dt <= now:
            recoverycalc_dt += timedelta(days=1)

        self._unsub_time_triggers.append(
            async_track_point_in_time(
                self.hass, self._on_recoverycalc_hour, recoverycalc_dt
            )
        )

        # Trigger pour target_hour (fin de relance / réveil)
        target_dt = now.replace(
            hour=self.data.target_hour.hour,
            minute=self.data.target_hour.minute,
            second=0,
            microsecond=0,
        )
        if target_dt <= now:
            target_dt += timedelta(days=1)

        self._unsub_time_triggers.append(
            async_track_point_in_time(self.hass, self._on_target_hour, target_dt)
        )

        # Trigger pour recovery_start_hour (démarrage relance)
        if self.data.recovery_start_hour:
            recovery_start = self.data.recovery_start_hour
            if recovery_start.tzinfo is None:
                recovery_start = dt_util.as_local(recovery_start)
            if recovery_start > now:
                self._unsub_time_triggers.append(
                    async_track_point_in_time(
                        self.hass,
                        self._on_recovery_start_hour,
                        recovery_start,
                    )
                )

        # Trigger pour recovery_update_hour (mise à jour calcul)
        if self.data.recovery_update_hour:
            recovery_update = self.data.recovery_update_hour
            if recovery_update.tzinfo is None:
                recovery_update = dt_util.as_local(recovery_update)
            if recovery_update > now:
                self._unsub_time_triggers.append(
                    async_track_point_in_time(
                        self.hass,
                        self._on_recovery_update_hour,
                        recovery_update,
                    )
                )

    def _cancel_time_triggers(self) -> None:
        """Annule les déclencheurs horaires"""
        for unsub in self._unsub_time_triggers:
            unsub()
        self._unsub_time_triggers.clear()

    async def async_unload(self) -> None:
        """Déchargement du coordinateur"""
        self._cancel_time_triggers()
        # Annuler le trigger de recovery_update
        if self._unsub_recovery_update:
            self._unsub_recovery_update()
            self._unsub_recovery_update = None
        # Annuler le trigger de recovery_start (bug yoyo fix)
        if self._unsub_recovery_start:
            self._unsub_recovery_start()
            self._unsub_recovery_start = None
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

    # ─────────────────────────────────────────────────────────────────────────
    # État initial et callbacks
    # ─────────────────────────────────────────────────────────────────────────

    async def _update_initial_states(self) -> None:
        """Récupération des états initiaux"""
        if self._interior_temp_sensor_id:
            state = self.hass.states.get(self._interior_temp_sensor_id)
            if state and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                try:
                    self.data.interior_temp = float(state.state)
                except ValueError:
                    pass

        self._update_weather_data()

    @callback
    def _on_sensor_state_change(self, event) -> None:
        """Callback lors d'un changement d'état du capteur de température."""
        new_state = event.data.get("new_state")
        if not new_state or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        entity_id = new_state.entity_id

        if entity_id == self._interior_temp_sensor_id:
            try:
                self.data.interior_temp = float(new_state.state)
                self._check_temperature_thresholds()
            except ValueError:
                pass

        self.async_set_updated_data(self.data)

    @callback
    def _on_weather_state_change(self, event) -> None:
        """Callback lors d'un changement d'état de l'entité météo.

        ADR-037: Remplace le polling périodique d'une minute.
        Met à jour les données météo et la moyenne de vent uniquement
        quand l'entité météo change réellement.
        """
        new_state = event.data.get("new_state")
        if not new_state or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self._update_weather_data()
        self._update_wind_speed_average()
        self.async_set_updated_data(self.data)

    @callback
    def _hourly_forecast_update(self, _now) -> None:
        """Mise à jour des prévisions météo (chaque heure)"""
        self.hass.async_create_task(self._update_weather_forecasts())

    # ─────────────────────────────────────────────────────────────────────────
    # Déclencheurs horaires (équivalent des automations YAML)
    # ─────────────────────────────────────────────────────────────────────────

    @callback
    def _on_recoverycalc_hour(self, _now) -> None:
        """Appelé à l'heure de coupure du chauffage (recoverycalc_hour)
        Équivalent de l'automation 'heatingstopTIME' du YAML
        """
        _LOGGER.info("%s Heure de coupure chauffage atteinte", self._log_prefix())

        if not self.data.smartheating_mode:
            self._reschedule_recoverycalc_hour()
            return

        # Déléguer les calculs lourds à une tâche asynchrone
        self.hass.async_create_task(self._async_on_recoverycalc_hour())

    async def _async_on_recoverycalc_hour(self) -> None:
        """Exécute l'initialisation à l'heure de coupure du chauffage.

        Transition: HEATING_ON → DETECTING_LAG (État 1 → État 2)

        Conformément au YAML original (automation heatingstopTIME):
        - On enregistre les valeurs courantes (temps, température)
        - On active la détection du lag de température
        - On N'appelle PAS calculate_recovery_time ici (sera fait après
          détection de la baisse de -0.2°C dans _on_temperature_decrease_detected)
        """
        # Initialisation des constantes si première exécution
        if self.data.rcth_lw <= 0:
            self.data.rcth_lw = 50.0
            self.data.rcth_hw = 50.0
            self.data.rpth_lw = 50.0
            self.data.rpth_hw = 50.0
            _LOGGER.info("%s Initialisation des constantes à 50", self._log_prefix())

        # Enregistre les valeurs courantes (snapshot avant refroidissement)
        self.data.time_recovery_calc = dt_util.now()
        self.data.temp_recovery_calc = self.data.interior_temp or 17.0
        self.data.text_recovery_calc = self.data.exterior_temp or 0.0

        # Transition vers DETECTING_LAG (État 2)
        # On attend la baisse de température de 0.2°C avant de lancer les calculs
        if not self.transition_to(SmartHRTState.DETECTING_LAG):
            self.force_state(SmartHRTState.DETECTING_LAG)
        # ADR-040: temp_lag_detection_active est maintenant calculé depuis current_state
        _LOGGER.debug("%s Transition vers état DETECTING_LAG", self._log_prefix())

        # Sauvegarder l'heure de relance avant calcul
        prev_recovery_start = self.data.recovery_start_hour

        # Exécuter les calculs lourds dans un exécuteur
        await self.hass.async_add_executor_job(self.calculate_recovery_time)

        # Programmer le trigger de relance si nécessaire (depuis le thread principal)
        now = dt_util.now()
        if (
            self.data.recovery_start_hour
            and prev_recovery_start != self.data.recovery_start_hour
            and self.data.recovery_start_hour > now
        ):
            self._schedule_recovery_start(self.data.recovery_start_hour)

        # Toujours programmer la mise à jour de recovery_update_hour
        # pour maintenir la chaîne de mises à jour active
        update_time = await self.hass.async_add_executor_job(
            self.calculate_recovery_update_time
        )
        if update_time:
            self.data.recovery_update_hour = update_time
            self._schedule_recovery_update(update_time)

        self._reschedule_recoverycalc_hour()

        # Sauvegarder l'état après la transition
        await self._save_learned_data()

        self.async_set_updated_data(self.data)

    @callback
    def _on_recovery_start_hour(self, _now) -> None:
        """Appelé à l'heure calculée de démarrage de la relance (recoverystart_hour)
        Équivalent de l'automation 'boostTIME' du YAML
        """
        _LOGGER.info("%s Heure de démarrage relance atteinte", self._log_prefix())

        if not self.data.smartheating_mode:
            return

        # Éviter de déclencher si on est déjà en RECOVERY ou HEATING_PROCESS
        if self.data.current_state in (
            SmartHRTState.RECOVERY,
            SmartHRTState.HEATING_PROCESS,
        ):
            _LOGGER.debug(
                "%s Relance ignorée, déjà en état %s",
                self._log_prefix(),
                self.data.current_state,
            )
            return

        self.on_recovery_start()

    @callback
    def _on_target_hour(self, _now) -> None:
        """Appelé à l'heure cible (target_hour / réveil)
        Équivalent de l'automation 'recoveryendTIME' du YAML
        """
        _LOGGER.info("%s Heure cible atteinte", self._log_prefix())

        if self.data.smartheating_mode and self.data.rp_calc_mode:
            self.on_recovery_end()

        self._reschedule_target_hour()

    @callback
    def _on_recovery_update_hour(self, _now) -> None:
        """Appelé pour mettre à jour le calcul de l'heure de relance
        Équivalent de l'automation 'Nth_RECOVERY_calc' du YAML
        """
        if not self.data.smartheating_mode:
            return

        _LOGGER.debug("%s Mise à jour du calcul de relance", self._log_prefix())
        # Déléguer les calculs lourds à une tâche asynchrone
        self.hass.async_create_task(self._async_on_recovery_update_hour())

    async def _async_on_recovery_update_hour(self) -> None:
        """Exécute les calculs lourds de mise à jour dans un exécuteur"""
        # Sauvegarder l'heure de relance avant calcul
        prev_recovery_start = self.data.recovery_start_hour

        # N'exécuter les calculs que si recovery_calc_mode est actif
        if self.data.recovery_calc_mode:
            await self.hass.async_add_executor_job(self.calculate_rcth_fast)
            await self.hass.async_add_executor_job(self.calculate_recovery_time)

            # Programmer le trigger de relance si nécessaire (depuis le thread principal)
            now = dt_util.now()
            if (
                self.data.recovery_start_hour
                and prev_recovery_start != self.data.recovery_start_hour
                and self.data.recovery_start_hour > now
            ):
                self._schedule_recovery_start(self.data.recovery_start_hour)

        # Toujours reprogrammer le prochain trigger de mise à jour
        # pour maintenir la chaîne active même si recovery_calc_mode est off
        update_time = await self.hass.async_add_executor_job(
            self.calculate_recovery_update_time
        )

        if update_time:
            self.data.recovery_update_hour = update_time
            self._schedule_recovery_update(update_time)
            _LOGGER.debug(
                "%s Prochaine mise à jour programmée: %s",
                self._log_prefix(),
                update_time,
            )

        self.async_set_updated_data(self.data)

    def _reschedule_recoverycalc_hour(self) -> None:
        """Reprogramme le déclencheur recoverycalc_hour pour le lendemain"""
        now = dt_util.now()
        next_trigger = now.replace(
            hour=self.data.recoverycalc_hour.hour,
            minute=self.data.recoverycalc_hour.minute,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)

        self._unsub_time_triggers.append(
            async_track_point_in_time(
                self.hass, self._on_recoverycalc_hour, next_trigger
            )
        )

    def _reschedule_target_hour(self) -> None:
        """Reprogramme le déclencheur target_hour pour le lendemain"""
        now = dt_util.now()
        next_trigger = now.replace(
            hour=self.data.target_hour.hour,
            minute=self.data.target_hour.minute,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)

        self._unsub_time_triggers.append(
            async_track_point_in_time(self.hass, self._on_target_hour, next_trigger)
        )

    def _schedule_recovery_start(self, trigger_time: datetime) -> None:
        """Programme le déclencheur de démarrage de relance.

        Annule le trigger précédent s'il existe avant d'en programmer un nouveau.
        Génère des logs appropriés pour la traçabilité.
        """
        # Annuler le trigger précédent s'il existe
        if self._unsub_recovery_start:
            self._unsub_recovery_start()
            _LOGGER.debug(
                "%s Trigger reprogrammé: annulation du précédent, nouveau à %s",
                self._log_prefix(),
                trigger_time,
            )
        else:
            _LOGGER.debug(
                "%s Nouveau trigger recovery_start programmé: %s",
                self._log_prefix(),
                trigger_time,
            )

        self._unsub_recovery_start = async_track_point_in_time(
            self.hass, self._on_recovery_start_hour, trigger_time
        )

    @callback
    def _schedule_recovery_update(self, trigger_time: datetime) -> None:
        """Programme le déclencheur de mise à jour du calcul"""
        # Annuler le trigger précédent s'il existe
        if self._unsub_recovery_update:
            self._unsub_recovery_update()
            self._unsub_recovery_update = None

        _LOGGER.debug(
            "%s Programmation prochaine mise à jour: %s",
            self._log_prefix(),
            trigger_time,
        )
        self._unsub_recovery_update = async_track_point_in_time(
            self.hass, self._on_recovery_update_hour, trigger_time
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Données météo
    # ─────────────────────────────────────────────────────────────────────────

    def _update_weather_data(self) -> None:
        """Mise à jour des données météo actuelles.

        ADR-002: Utilise l'entité météo configurée explicitement par l'utilisateur
        au lieu de scanner automatiquement toutes les entités weather.
        """
        if not self._weather_entity_id:
            _LOGGER.debug(
                "%s Aucune entité météo configurée, mise à jour météo ignorée",
                self._log_prefix(),
            )
            return

        weather = self.hass.states.get(self._weather_entity_id)
        if weather is None:
            _LOGGER.warning(
                "%s Entité météo %s non trouvée",
                self._log_prefix(),
                self._weather_entity_id,
            )
            return

        if (temp := weather.attributes.get("temperature")) is not None:
            self.data.exterior_temp = float(temp)

        if (wind := weather.attributes.get("wind_speed")) is not None:
            self.data.wind_speed = float(wind) / 3.6  # km/h -> m/s
            # Ajouter à l'historique pour la moyenne
            self.data.wind_speed_history.append(self.data.wind_speed)

        self._calculate_windchill()

    def _update_wind_speed_average(self) -> None:
        """Calcule la moyenne de vitesse du vent sur 4h"""
        if self.data.wind_speed_history:
            self.data.wind_speed_avg = sum(self.data.wind_speed_history) / len(
                self.data.wind_speed_history
            )

    async def _update_weather_forecasts(self) -> None:
        """Mise à jour des prévisions météo (température et vent).

        ADR-002: Utilise l'entité météo configurée explicitement par l'utilisateur.
        """
        if not self._weather_entity_id:
            _LOGGER.debug(
                "%s Aucune entité météo configurée, mise à jour prévisions ignorée",
                self._log_prefix(),
            )
            return

        entity_id = self._weather_entity_id

        try:
            # Vérifier que le service existe avant de l'appeler
            if not self.hass.services.has_service("weather", "get_forecasts"):
                _LOGGER.debug(
                    "%s Service weather.get_forecasts pas encore disponible (démarrage en cours)",
                    self._log_prefix(),
                )
                return

            # Appeler le service weather.get_forecasts
            forecast_response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )

            if forecast_response and entity_id in forecast_response:
                entity_forecast = forecast_response[entity_id]
                if isinstance(entity_forecast, dict):
                    forecast_list = entity_forecast.get("forecast", [])
                    if isinstance(forecast_list, list):
                        forecasts = forecast_list[:FORECAST_HOURS]

                        if forecasts:
                            # Moyenne température
                            temps: list[float] = []
                            winds: list[float] = []

                            for f in forecasts:
                                if isinstance(f, dict):
                                    temp_val = f.get("temperature")
                                    if isinstance(temp_val, (int, float)):
                                        temps.append(float(temp_val))

                                    wind_val = f.get("wind_speed")
                                    if isinstance(wind_val, (int, float)):
                                        winds.append(float(wind_val))

                            if temps:
                                self.data.temperature_forecast_avg = sum(temps) / len(
                                    temps
                                )

                            if winds:
                                self.data.wind_speed_forecast_avg = sum(winds) / len(
                                    winds
                                )

                            _LOGGER.debug(
                                "%s Prévisions mises à jour: temp=%.1f°C, vent=%.1fkm/h",
                                self._log_prefix(),
                                self.data.temperature_forecast_avg,
                                self.data.wind_speed_forecast_avg,
                            )
        except ServiceNotFound:
            _LOGGER.debug(
                "%s Service weather.get_forecasts non disponible au démarrage",
                self._log_prefix(),
            )
        except Exception as ex:
            _LOGGER.warning(
                "%s Erreur lors de la récupération des prévisions météo: %s",
                self._log_prefix(),
                ex,
            )

    def _calculate_windchill(self) -> None:
        """Calcul de la température ressentie (windchill).

        ADR-026: Délègue au ThermalSolver pour le calcul Pure Python.
        """
        if self.data.exterior_temp is None:
            return

        self.data.windchill = self._thermal_solver.calculate_windchill(
            self.data.exterior_temp,
            self.data.wind_speed,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-007: Compensation météo - Interpolation linéaire selon le vent
    # ─────────────────────────────────────────────────────────────────────────

    def _get_interpolated_rcth(self, wind_kmh: float) -> float:
        """Retourne RCth interpolé selon le vent (ADR-026: via ThermalSolver)."""
        coeffs = self._build_thermal_coefficients()
        return self._thermal_solver.get_interpolated_rcth(coeffs, wind_kmh)

    def _get_interpolated_rpth(self, wind_kmh: float) -> float:
        """Retourne RPth interpolé selon le vent (ADR-026: via ThermalSolver)."""
        coeffs = self._build_thermal_coefficients()
        return self._thermal_solver.get_interpolated_rpth(coeffs, wind_kmh)

    def _build_thermal_coefficients(self) -> ThermalCoefficients:
        """Construit un objet ThermalCoefficients depuis les données actuelles.

        ADR-026: Facilite le passage des données au ThermalSolver.
        """
        return ThermalCoefficients(
            rcth=self.data.rcth,
            rpth=self.data.rpth,
            rcth_lw=self.data.rcth_lw,
            rcth_hw=self.data.rcth_hw,
            rpth_lw=self.data.rpth_lw,
            rpth_hw=self.data.rpth_hw,
            rcth_calculated=self.data.rcth_calculated,
            rpth_calculated=self.data.rpth_calculated,
            rcth_fast=self.data.rcth_fast,
            relaxation_factor=self.data.relaxation_factor,
            last_rcth_error=self.data.last_rcth_error,
            last_rpth_error=self.data.last_rpth_error,
        )

    def _build_thermal_state(self) -> ThermalState:
        """Construit un objet ThermalState depuis les données actuelles.

        ADR-026: Facilite le passage des données au ThermalSolver.
        """
        return ThermalState(
            interior_temp=self.data.interior_temp,
            exterior_temp=self.data.exterior_temp,
            windchill=self.data.windchill,
            wind_speed_ms=self.data.wind_speed,
            wind_speed_avg_ms=self.data.wind_speed_avg,
            temperature_forecast_avg=self.data.temperature_forecast_avg,
            wind_speed_forecast_avg_kmh=self.data.wind_speed_forecast_avg,
            tsp=self.data.tsp,
            target_hour=self.data.target_hour,
            now=dt_util.now(),
            temp_recovery_calc=self.data.temp_recovery_calc,
            text_recovery_calc=self.data.text_recovery_calc,
            temp_recovery_start=self.data.temp_recovery_start,
            text_recovery_start=self.data.text_recovery_start,
            temp_recovery_end=self.data.temp_recovery_end,
            text_recovery_end=self.data.text_recovery_end,
            time_recovery_calc=self.data.time_recovery_calc,
            time_recovery_start=self.data.time_recovery_start,
            time_recovery_end=self.data.time_recovery_end,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-005: Stratégie de pilotage - Calculs thermiques d'anticipation
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_recovery_time(self) -> None:
        """Calcule l'heure de démarrage de la relance (ADR-005, ADR-026).

        ADR-026: Délègue au ThermalSolver pour le calcul Pure Python.
        Utilise les prévisions météo et 20 itérations pour affiner la prédiction.
        """
        now = dt_util.now()
        state = self._build_thermal_state()
        coeffs = self._build_thermal_coefficients()

        result = self._thermal_solver.calculate_recovery_duration(state, coeffs, now)

        self.data.recovery_start_hour = result.recovery_start_hour

        # Note: Le scheduling du trigger est fait dans le contexte async appelant
        # car async_track_point_in_time doit être appelé depuis le thread principal

        _LOGGER.debug(
            "%s Recovery time: %s (%.2fh avant target)",
            self._log_prefix(),
            self.data.recovery_start_hour,
            result.duration_hours,
        )

    def calculate_recovery_update_time(self) -> datetime | None:
        """Calcule l'heure de mise à jour de la relance (ADR-026: via ThermalSolver).

        La logique:
        - Reconstruit recoverystart_time à partir de l'heure de recovery_start_hour
        - Calcule le temps restant avant la relance
        - Reprogramme dans max(time_remaining/3, 0) secondes, plafonné à 1200s (20min)
        - À moins de 30min avant la relance, arrête en programmant dans 3600s
        """
        if self.data.recovery_start_hour is None:
            return None

        now = dt_util.now()
        update_time = self._thermal_solver.calculate_recovery_update_time(
            self.data.recovery_start_hour,
            now,
        )

        if update_time:
            _LOGGER.debug(
                "%s Recovery update time calculated: %s",
                self._log_prefix(),
                update_time,
            )

        return update_time

    def calculate_rcth_fast(self) -> None:
        """Calcule l'évolution dynamique de RCth (ADR-026: via ThermalSolver)."""
        if (
            self.data.interior_temp is None
            or self.data.exterior_temp is None
            or self.data.time_recovery_calc is None
        ):
            return

        dt_hours = (dt_util.now() - self.data.time_recovery_calc).total_seconds() / 3600

        result = self._thermal_solver.calculate_rcth_fast(
            interior_temp=self.data.interior_temp,
            exterior_temp=self.data.exterior_temp,
            temp_at_start=self.data.temp_recovery_calc,
            text_at_start=self.data.text_recovery_calc,
            time_since_start_hours=dt_hours,
        )

        if result is not None:
            self.data.rcth_fast = result

    def calculate_rcth_at_recovery_start(self) -> None:
        """Calcule RCth au démarrage de la relance (ADR-026: via ThermalSolver)."""
        if (
            self.data.time_recovery_start is None
            or self.data.time_recovery_calc is None
        ):
            return

        result = self._thermal_solver.calculate_rcth_at_recovery(
            temp_recovery_calc=self.data.temp_recovery_calc,
            temp_recovery_start=self.data.temp_recovery_start,
            text_recovery_calc=self.data.text_recovery_calc,
            text_recovery_start=self.data.text_recovery_start,
            time_recovery_calc=self.data.time_recovery_calc,
            time_recovery_start=self.data.time_recovery_start,
        )

        if result is not None:
            self.data.rcth_calculated = result

        if self.data.recovery_adaptive_mode:
            self._update_coefficients("rcth")

    def calculate_rpth_at_recovery_end(self) -> None:
        """Calcule RPth à la fin de la relance (ADR-026: via ThermalSolver).

        Utilise wind_speed_avg (moyenne 4h) pour l'interpolation RCth,
        conformément au YAML original.
        """
        if self.data.time_recovery_start is None or self.data.time_recovery_end is None:
            return

        # Utiliser wind_speed_avg (moyenne 4h) comme dans le YAML
        wind_kmh = self.data.wind_speed_avg * 3.6
        rcth_interpol = self._get_interpolated_rcth(wind_kmh)

        result = self._thermal_solver.calculate_rpth_at_recovery(
            temp_recovery_start=self.data.temp_recovery_start,
            temp_recovery_end=self.data.temp_recovery_end,
            text_recovery_start=self.data.text_recovery_start,
            text_recovery_end=self.data.text_recovery_end,
            time_recovery_start=self.data.time_recovery_start,
            time_recovery_end=self.data.time_recovery_end,
            rcth_interpolated=rcth_interpol,
        )

        if result is not None:
            self.data.rpth_calculated = result

        if self.data.recovery_adaptive_mode:
            self._update_coefficients("rpth")

    def _update_coefficients(self, coef_type: str) -> None:
        """Met à jour les coefficients avec relaxation (ADR-006, ADR-026).

        ADR-026: Délègue au ThermalSolver pour le calcul Pure Python.
        Utilise wind_speed_avg (moyenne 4h) conformément au YAML original.
        """
        # Utiliser wind_speed_avg (moyenne 4h) comme dans le YAML
        wind_kmh = self.data.wind_speed_avg * 3.6

        if coef_type == "rcth":
            result = self._thermal_solver.update_coefficients(
                coef_type="rcth",
                current_lw=self.data.rcth_lw,
                current_hw=self.data.rcth_hw,
                current_main=self.data.rcth,
                calculated_value=self.data.rcth_calculated,
                wind_kmh=wind_kmh,
                relaxation_factor=self.data.relaxation_factor,
            )
            self.data.rcth_lw = result.coef_lw
            self.data.rcth_hw = result.coef_hw
            self.data.rcth = result.coef_main
            self.data.last_rcth_error = result.error
            # Note: La sauvegarde est effectuée par la fonction appelante
        else:
            result = self._thermal_solver.update_coefficients(
                coef_type="rpth",
                current_lw=self.data.rpth_lw,
                current_hw=self.data.rpth_hw,
                current_main=self.data.rpth,
                calculated_value=self.data.rpth_calculated,
                wind_kmh=wind_kmh,
                relaxation_factor=self.data.relaxation_factor,
            )
            self.data.rpth_lw = result.coef_lw
            self.data.rpth_hw = result.coef_hw
            self.data.rpth = result.coef_main
            self.data.last_rpth_error = result.error
            # Note: La sauvegarde est effectuée par la fonction appelante

    # ─────────────────────────────────────────────────────────────────────────
    # Événements chauffage
    # ─────────────────────────────────────────────────────────────────────────

    def _check_temperature_thresholds(self) -> None:
        """Vérifie les seuils de température
        Gère la détection du lag de température et la fin de relance
        """
        if self.data.interior_temp is None:
            return

        # ADR-008: Validation arrêt par détection lag de température
        # Attend une baisse de 0.2°C pour confirmer l'arrêt réel du chauffage
        if self.data.temp_lag_detection_active:
            temp_threshold = self.data.temp_recovery_calc - TEMP_DECREASE_THRESHOLD

            if self.data.interior_temp <= temp_threshold:
                # Température a baissé de 0.2°C - le refroidissement réel commence
                self._on_temperature_decrease_detected()
            elif self.data.interior_temp > self.data.temp_recovery_calc:
                # Température a augmenté - mettre à jour le snapshot
                self.data.temp_recovery_calc = self.data.interior_temp

        # Vérifier si la consigne est atteinte pendant le mode rp_calc
        if self.data.rp_calc_mode and self.data.interior_temp >= self.data.tsp:
            self.on_recovery_end()

    def _on_temperature_decrease_detected(self) -> None:
        """Appelé quand la température commence réellement à baisser
        Équivalent du trigger 'temperatureDecrease' dans l'automation detect_temperature_lag

        Transition: DETECTING_LAG → MONITORING (État 2 → État 3)
        """
        if self.data.time_recovery_calc is None:
            return

        now = dt_util.now()

        # Calculer la durée du lag
        self.data.stop_lag_duration = min(
            (now.timestamp() - self.data.time_recovery_calc.timestamp()), 10799
        )

        # Mettre à jour les snapshots avec les vraies valeurs de départ du refroidissement
        self.data.temp_recovery_calc = self.data.interior_temp or 17.0
        self.data.text_recovery_calc = self.data.exterior_temp or 0.0
        self.data.time_recovery_calc = now

        # Transition vers MONITORING (État 3)
        if not self.transition_to(SmartHRTState.MONITORING):
            self.force_state(SmartHRTState.MONITORING)
        # ADR-040: recovery_calc_mode et temp_lag_detection_active sont calculés depuis current_state
        _LOGGER.debug("%s Transition vers état MONITORING", self._log_prefix())

        self.calculate_recovery_time()

        # Calculer et programmer la prochaine mise à jour (comme dans le YAML)
        update_time = self.calculate_recovery_update_time()
        if update_time:
            self.data.recovery_update_hour = update_time
            self._schedule_recovery_update(update_time)

        _LOGGER.info(
            "%s Baisse de température détectée après %.0fs de lag",
            self._log_prefix(),
            self.data.stop_lag_duration,
        )

        # Sauvegarder l'état après la transition
        self.hass.async_create_task(self._save_learned_data())

        self.async_set_updated_data(self.data)

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-039: Restauration simplifiée avec auto-correction
    # ─────────────────────────────────────────────────────────────────────────

    def _is_night_period(
        self, current_time: dt_time, target: dt_time, recoverycalc: dt_time
    ) -> bool:
        """Détermine si on est en période nocturne (entre recoverycalc et target).

        Args:
            current_time: Heure actuelle (time)
            target: Heure cible du matin (ex: 06:00)
            recoverycalc: Heure de calcul du soir (ex: 23:00)

        Returns:
            True si on est en période nocturne (MONITORING attendu)
        """
        if target < recoverycalc:
            # Cas normal: target=06:00, recoverycalc=23:00
            # Nuit = après 23:00 OU avant 06:00
            return current_time >= recoverycalc or current_time < target
        else:
            # Cas atypique: recoverycalc=13:30, target=17:30
            # "Nuit" = entre 13:30 et 17:30
            return recoverycalc <= current_time < target

    def _is_state_coherent(self, persisted_state: SmartHRTState, now: datetime) -> bool:
        """Vérifie si l'état persisté est cohérent avec l'heure actuelle (ADR-039).

        Règles simplifiées :
        1. HEATING_ON est toujours valide (état par défaut sûr)
        2. MONITORING/DETECTING_LAG sont valides la nuit (après recoverycalc, avant target)
        3. RECOVERY/HEATING_PROCESS sont valides si recovery_start_hour <= now < target

        Args:
            persisted_state: État lu depuis la persistance
            now: Heure actuelle

        Returns:
            True si l'état est cohérent, False sinon
        """
        # HEATING_ON est toujours valide (état sûr par défaut)
        if persisted_state == SmartHRTState.HEATING_ON:
            return True

        current_time = now.time()
        target = self.data.target_hour
        recoverycalc = self.data.recoverycalc_hour

        is_night = self._is_night_period(current_time, target, recoverycalc)

        # MONITORING/DETECTING_LAG valides la nuit uniquement
        if persisted_state in (SmartHRTState.MONITORING, SmartHRTState.DETECTING_LAG):
            return is_night

        # RECOVERY/HEATING_PROCESS valides pendant la période de relance
        if persisted_state in (SmartHRTState.RECOVERY, SmartHRTState.HEATING_PROCESS):
            if not self.data.recovery_start_hour:
                return False  # Pas de recovery_start_hour = incohérent
            # Valide si : recovery_start_hour <= now ET current_time < target
            return now >= self.data.recovery_start_hour and current_time < target

        # État inconnu = incohérent
        return False

    def _restore_triggers_for_state(self, state: SmartHRTState, now: datetime) -> None:
        """Reprogramme les triggers appropriés pour l'état restauré (ADR-039).

        Args:
            state: État courant restauré
            now: Heure actuelle
        """
        if state == SmartHRTState.MONITORING:
            if self.data.recovery_start_hour:
                if self.data.recovery_start_hour > now:
                    self._schedule_recovery_start(self.data.recovery_start_hour)
                else:
                    # L'heure est passée mais on était en MONITORING = on démarre
                    _LOGGER.info(
                        "%s Heure de relance dépassée, démarrage immédiat",
                        self._log_prefix(),
                    )
                    self.on_recovery_start()

        elif state == SmartHRTState.DETECTING_LAG:
            # La surveillance de température reprendra automatiquement
            pass

        elif state in (SmartHRTState.RECOVERY, SmartHRTState.HEATING_PROCESS):
            # Vérifier si target_hour est dépassée
            target_dt = now.replace(
                hour=self.data.target_hour.hour,
                minute=self.data.target_hour.minute,
                second=0,
                microsecond=0,
            )
            if now >= target_dt:
                # Target dépassée, terminer le cycle
                self.on_recovery_end()

        # HEATING_ON: rien à faire (attend le prochain recoverycalc_hour)

    async def _restore_state_after_restart(self) -> None:
        """Restaure l'état après redémarrage avec vérification minimale (ADR-039).

        Principe "Trust the Persistence, Verify Minimally" :
        - L'état persisté est la source de vérité
        - Vérification de cohérence minimaliste
        - En cas d'incohérence, reset à HEATING_ON (auto-correction)
        """
        persisted_state = self.data.current_state
        now = dt_util.now()

        _LOGGER.info(
            "%s Restauration - État persisté: %s",
            self._log_prefix(),
            persisted_state.value,
        )

        # Vérification de cohérence minimale
        if not self._is_state_coherent(persisted_state, now):
            _LOGGER.warning(
                "%s État incohérent %s pour l'heure actuelle, reset à HEATING_ON",
                self._log_prefix(),
                persisted_state.value,
            )
            self.force_state(SmartHRTState.HEATING_ON)
            await self._save_learned_data()
            self.async_set_updated_data(self.data)
            return

        # État cohérent : reprogrammer les triggers nécessaires
        _LOGGER.debug(
            "%s État %s cohérent, restauration des triggers",
            self._log_prefix(),
            persisted_state.value,
        )
        self._restore_triggers_for_state(persisted_state, now)
        self.async_set_updated_data(self.data)

    def on_heating_stop(self) -> None:
        """Appelé quand le chauffage s'arrête (service manuel)"""
        self.data.time_recovery_calc = dt_util.now()
        self.data.temp_recovery_calc = self.data.interior_temp or 17.0
        self.data.text_recovery_calc = self.data.exterior_temp or 0.0
        # ADR-040: temp_lag_detection_active est calculé depuis current_state
        self.calculate_recovery_time()
        # Reprogrammer le trigger de relance avec la nouvelle heure calculée
        if (
            self.data.recovery_start_hour
            and self.data.current_state == SmartHRTState.MONITORING
        ):
            try:
                self._schedule_recovery_start(self.data.recovery_start_hour)
            except Exception as e:
                _LOGGER.warning(
                    "%s Erreur reprogrammation trigger: %s", self._log_prefix(), e
                )
        self.async_set_updated_data(self.data)

    def on_recovery_start(self) -> None:
        """Appelé au début de la relance
        Équivalent de l'automation 'boostTIME' du YAML

        Transition: MONITORING → RECOVERY → HEATING_PROCESS (État 3 → État 4 → État 5)
        """
        # Transition vers RECOVERY (État 4)
        now = dt_util.now()
        updates = {
            "time_recovery_start": now,
            "temp_recovery_start": self.data.interior_temp or 17.0,
            "text_recovery_start": self.data.exterior_temp or 0.0,
        }
        actions = self._apply_state_transition_with_actions(
            SmartHRTState.RECOVERY,
            updates=updates,
            omit_actions={Action.SNAPSHOT_RECOVERY_START},
        )
        if actions is None:
            self.data.update(**updates)
            self.force_state(SmartHRTState.RECOVERY)
            actions = [
                Action.CANCEL_RECOVERY_TIMER,
                Action.CALCULATE_RCTH,
                Action.SAVE_DATA,
            ]
        _LOGGER.debug("%s Transition vers état RECOVERY", self._log_prefix())

        pre_actions = [action for action in actions if action != Action.SAVE_DATA]
        if pre_actions:
            self._execute_actions(pre_actions)

        # Transition vers HEATING_PROCESS (État 5) - chauffage en cours
        if not self.transition_to(SmartHRTState.HEATING_PROCESS):
            self.force_state(SmartHRTState.HEATING_PROCESS)
        _LOGGER.debug("%s Transition vers état HEATING_PROCESS", self._log_prefix())

        if Action.SAVE_DATA in actions:
            self._execute_actions([Action.SAVE_DATA])

        _LOGGER.info(
            "%s Début de relance - Tint=%.1f°C, RCth calculé=%.2f",
            self._log_prefix(),
            self.data.temp_recovery_start,
            self.data.rcth_calculated,
        )

        self.async_set_updated_data(self.data)

    def on_recovery_end(self) -> None:
        """Appelé à la fin de la relance (consigne atteinte ou target_hour)
        Équivalent de l'automation 'recoveryendTIME' du YAML

        Transition: HEATING_PROCESS → HEATING_ON (État 5 → État 1)
        """
        if not self.data.rp_calc_mode:
            return

        pre_actions = []
        # Transition vers HEATING_ON (État 1) - cycle terminé
        now = dt_util.now()
        updates = {
            "time_recovery_end": now,
            "temp_recovery_end": self.data.interior_temp or 17.0,
            "text_recovery_end": self.data.exterior_temp or 0.0,
        }
        actions = self._apply_state_transition_with_actions(
            SmartHRTState.HEATING_ON,
            updates=updates,
            omit_actions={Action.SNAPSHOT_RECOVERY_END},
        )
        if actions is None:
            self.data.update(**updates)
            self.force_state(SmartHRTState.HEATING_ON)
            actions = [
                Action.CALCULATE_RPTH,
                Action.SAVE_DATA,
            ]
        _LOGGER.debug(
            "%s Transition vers état HEATING_ON - Cycle terminé", self._log_prefix()
        )

        pre_actions = [action for action in actions if action != Action.SAVE_DATA]
        if pre_actions:
            self._execute_actions(pre_actions)

        if Action.SAVE_DATA in actions:
            self._execute_actions([Action.SAVE_DATA])

        _LOGGER.info(
            "%s Fin de relance - Tint=%.1f°C, RPth calculé=%.2f",
            self._log_prefix(),
            self.data.temp_recovery_end,
            self.data.rpth_calculated,
        )

        self.async_set_updated_data(self.data)

    def _on_recovery_end(self) -> None:
        """Ancienne méthode interne - redirige vers on_recovery_end"""
        self.on_recovery_end()

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-042: Méthodes Façade pour les services
    # ─────────────────────────────────────────────────────────────────────────

    async def async_manual_stop_heating(self) -> dict[str, Any]:
        """Arrêt manuel du chauffage - méthode façade pour les services (ADR-042).

        Encapsule:
        - Transition vers HEATING_ON
        - Réinitialisation des flags via la machine à états
        - Annulation des timers en cours
        - Sauvegarde des données
        """
        # Annuler les timers
        self._cancel_recovery_start_timer()
        if self._unsub_recovery_update:
            self._unsub_recovery_update()
            self._unsub_recovery_update = None

        # Transition vers HEATING_ON
        if not self.transition_to(SmartHRTState.HEATING_ON):
            self.force_state(SmartHRTState.HEATING_ON)

        await self._save_learned_data()
        self.async_set_updated_data(self.data)

        _LOGGER.info("%s Arrêt manuel du chauffage effectué", self._log_prefix())

        return {
            "success": True,
            "state": str(self.data.current_state),
            "message": "Chauffage arrêté et réinitialisé",
        }

    async def async_start_heating_cycle(self) -> dict[str, Any]:
        """Démarrage d'un nouveau cycle de chauffage - méthode façade (ADR-042).

        Équivalent à l'appel de recoverycalc_hour.
        """
        await self._async_on_recoverycalc_hour()

        _LOGGER.info(
            "%s Nouveau cycle de chauffage démarré manuellement", self._log_prefix()
        )

        return {
            "success": True,
            "state": str(self.data.current_state),
            "recovery_start_hour": (
                self.data.recovery_start_hour.isoformat()
                if self.data.recovery_start_hour
                else None
            ),
            "message": "Cycle de chauffage démarré",
        }

    async def async_manual_start_recovery(self) -> dict[str, Any]:
        """Démarrage manuel de la relance - méthode façade (ADR-042).

        Encapsule:
        - Appel à on_recovery_start()
        - Sauvegarde des données
        """
        self.on_recovery_start()
        await self._save_learned_data()

        _LOGGER.info("%s Relance démarrée manuellement", self._log_prefix())

        return {
            "success": True,
            "state": str(self.data.current_state),
            "time_recovery_start": (
                self.data.time_recovery_start.isoformat()
                if self.data.time_recovery_start
                else None
            ),
            "rcth_calculated": self.data.rcth_calculated,
            "message": "Relance démarrée",
        }

    async def async_manual_end_recovery(self) -> dict[str, Any]:
        """Fin manuelle de la relance - méthode façade (ADR-042).

        Encapsule:
        - Appel à on_recovery_end()
        - Sauvegarde des données
        """
        self.on_recovery_end()
        await self._save_learned_data()

        _LOGGER.info("%s Relance terminée manuellement", self._log_prefix())

        return {
            "success": True,
            "state": str(self.data.current_state),
            "time_recovery_end": (
                self.data.time_recovery_end.isoformat()
                if self.data.time_recovery_end
                else None
            ),
            "rpth_calculated": self.data.rpth_calculated,
            "message": "Relance terminée",
        }

    def get_state_dict(self) -> dict[str, Any]:
        """Retourne l'état complet du coordinateur - méthode façade (ADR-042).

        Utilisée par le service get_state.
        """
        return {
            "success": True,
            "state": str(self.data.current_state),
            "smartheating_mode": self.data.smartheating_mode,
            "recovery_calc_mode": self.data.recovery_calc_mode,
            "rp_calc_mode": self.data.rp_calc_mode,
            "temp_lag_detection_active": self.data.temp_lag_detection_active,
            "interior_temp": self.data.interior_temp,
            "exterior_temp": self.data.exterior_temp,
            "target_hour": self.data.target_hour.isoformat(),
            "recoverycalc_hour": self.data.recoverycalc_hour.isoformat(),
            "recovery_start_hour": (
                self.data.recovery_start_hour.isoformat()
                if self.data.recovery_start_hour
                else None
            ),
            "time_to_recovery_hours": self.get_time_to_recovery_hours(),
            "rcth": self.data.rcth,
            "rpth": self.data.rpth,
        }

    async def async_trigger_calculation(self) -> dict[str, Any]:
        """Force un recalcul du temps de relance - méthode façade (ADR-042)."""
        await self.hass.async_add_executor_job(self.calculate_recovery_time)
        self._notify_listeners()

        return {
            "success": True,
            "recovery_start_hour": (
                self.data.recovery_start_hour.isoformat()
                if self.data.recovery_start_hour
                else None
            ),
            "time_to_recovery_hours": self.get_time_to_recovery_hours(),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-036: Méthode générique pour setters
    # ─────────────────────────────────────────────────────────────────────────

    def _update_and_recalculate(
        self,
        attr_name: str,
        value: float | dt_time,
        recalculate: bool = True,
        reschedule: bool = True,
        persist: bool = False,
    ) -> None:
        """Met à jour un attribut et gère les effets de bord de manière centralisée.

        ADR-036: Factorisation des setters pour éviter la duplication de code.

        Args:
            attr_name: Nom de l'attribut dans self.data (ex: "rcth", "tsp")
            value: Nouvelle valeur à assigner
            recalculate: Si True, recalcule recovery_time après modification
            reschedule: Si True, reprogramme le trigger si en MONITORING
            persist: Si True, sauvegarde les données après modification

        ADR-023: Protection try/except centralisée pour la reprogrammation.
        """
        # 1. Mise à jour atomique de la valeur
        setattr(self.data, attr_name, value)

        # 2. Recalcul optionnel de l'heure de relance
        if recalculate:
            self.calculate_recovery_time()

        # 3. Reprogrammation du trigger si nécessaire (ADR-023)
        if (
            reschedule
            and self.data.recovery_start_hour
            and self.data.current_state == SmartHRTState.MONITORING
        ):
            try:
                self._schedule_recovery_start(self.data.recovery_start_hour)
            except Exception as e:
                _LOGGER.warning(
                    "%s Erreur reprogrammation trigger: %s",
                    self._log_prefix(),
                    e,
                )

        # 4. Notification des entités
        self.async_set_updated_data(self.data)

        # 5. Persistance optionnelle
        if persist:
            self.hass.async_create_task(self._save_learned_data())

    # ─────────────────────────────────────────────────────────────────────────
    # Setters publics (ADR-036: factorisés via _update_and_recalculate)
    # ─────────────────────────────────────────────────────────────────────────

    def set_tsp(self, value: float) -> None:
        """Définit la température de consigne (TSP)."""
        self._update_and_recalculate("tsp", value)

    def set_target_hour(self, value: dt_time) -> None:
        """Définit l'heure cible (réveil)."""
        self._update_and_recalculate(
            "target_hour",
            value,
            reschedule=False,  # Géré par _setup_time_triggers
            persist=True,
        )
        self._setup_time_triggers()

    def set_recoverycalc_hour(self, value: dt_time) -> None:
        """Définit l'heure de coupure chauffage."""
        self._update_and_recalculate(
            "recoverycalc_hour",
            value,
            recalculate=False,
            reschedule=False,
            persist=True,
        )
        self._setup_time_triggers()

    def set_smartheating_mode(self, value: bool) -> None:
        """Active/désactive le mode chauffage intelligent."""
        self.data.smartheating_mode = value
        self.async_set_updated_data(self.data)

    def set_recovery_adaptive_mode(self, value: bool) -> None:
        """Active/désactive le mode adaptatif."""
        self.data.recovery_adaptive_mode = value
        self.async_set_updated_data(self.data)

    def set_adaptive_mode(self, value: bool) -> None:
        """Alias pour set_recovery_adaptive_mode."""
        self.set_recovery_adaptive_mode(value)

    def set_rcth(self, value: float) -> None:
        """Définit le coefficient thermique RCth."""
        self._update_and_recalculate("rcth", value)

    def set_rpth(self, value: float) -> None:
        """Définit le coefficient thermique RPth."""
        self._update_and_recalculate("rpth", value)

    def set_relaxation_factor(self, value: float) -> None:
        """Définit le facteur de relaxation."""
        self._update_and_recalculate(
            "relaxation_factor", value, recalculate=False, reschedule=False
        )

    def set_rcth_lw(self, value: float) -> None:
        """Définit RCth pour vent faible."""
        self._update_and_recalculate("rcth_lw", value)

    def set_rcth_hw(self, value: float) -> None:
        """Définit RCth pour vent fort."""
        self._update_and_recalculate("rcth_hw", value)

    def set_rpth_lw(self, value: float) -> None:
        """Définit RPth pour vent faible."""
        self._update_and_recalculate("rpth_lw", value)

    def set_rpth_hw(self, value: float) -> None:
        """Définit RPth pour vent fort."""
        self._update_and_recalculate("rpth_hw", value)

    # ─────────────────────────────────────────────────────────────────────────
    # Public methods for services
    # ─────────────────────────────────────────────────────────────────────────

    async def reset_learning(self) -> None:
        """Reset all learned thermal coefficients to defaults.

        Resets RCth and RPth (and their wind variants) to default values
        and clears the error tracking. Also clears persistent storage.
        """
        _LOGGER.info(
            "%s Resetting learned coefficients to defaults", self._log_prefix()
        )
        self.data.rcth = DEFAULT_RCTH
        self.data.rpth = DEFAULT_RPTH
        self.data.rcth_lw = DEFAULT_RCTH
        self.data.rcth_hw = DEFAULT_RCTH
        self.data.rpth_lw = DEFAULT_RPTH
        self.data.rpth_hw = DEFAULT_RPTH
        self.data.rcth_calculated = 0.0
        self.data.rpth_calculated = 0.0
        self.data.rcth_fast = 0.0
        self.data.last_rcth_error = 0.0
        self.data.last_rpth_error = 0.0

        # Save the reset values to storage
        await self._save_learned_data()

        # Recalculate recovery time with new coefficients
        self.calculate_recovery_time()
        self.async_set_updated_data(self.data)

    def get_time_to_recovery_hours(self) -> float | None:
        """Calculate the time remaining until recovery starts in hours.

        Returns the duration in hours until the heating should start,
        or None if recovery_start_hour is not set.
        """
        if self.data.recovery_start_hour is None:
            return None

        now = dt_util.now()
        recovery_time = self.data.recovery_start_hour

        if recovery_time.tzinfo is None:
            recovery_time = dt_util.as_local(recovery_time)

        delta = recovery_time - now
        hours = delta.total_seconds() / 3600

        # Return 0 if time has passed
        return max(0, round(hours, 2))
