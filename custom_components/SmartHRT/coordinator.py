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
"""

import logging
from datetime import datetime, timedelta, time as dt_time
from dataclasses import dataclass, field
from typing import Callable
from collections import deque
from enum import StrEnum

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
    PERSISTED_FIELDS,
)

# ADR-026: Import du modèle thermique Pure Python
from .core import ThermalSolver, ThermalState, ThermalCoefficients, ThermalConfig

_LOGGER = logging.getLogger(__name__)


# ADR-003 & ADR-028: Machine à états explicite avec StrEnum
# Les 5 états modélisent le cycle thermique journalier complet
class SmartHRTState(StrEnum):
    """États de la machine à états SmartHRT (ADR-003, ADR-028).

    Cycle de vie:
    HEATING_ON → DETECTING_LAG → MONITORING → RECOVERY → HEATING_PROCESS → HEATING_ON

    StrEnum permet:
    - Compatibilité JSON native (SmartHRTState.HEATING_ON == "heating_on")
    - Validation automatique à l'assignation
    - Itération sur les états: list(SmartHRTState)
    - Meilleur support IDE et mypy
    """

    HEATING_ON = "heating_on"  # État 1: Journée, chauffage actif
    DETECTING_LAG = "detecting_lag"  # État 2: Attente baisse de température (-0.2°C)
    MONITORING = "monitoring"  # État 3: Surveillance nocturne, calculs récurrents
    RECOVERY = "recovery"  # État 4: Moment de la relance, calcul RCth
    HEATING_PROCESS = "heating_process"  # État 5: Montée en température, calcul RPth


# ADR-028: Table des transitions valides entre états
# Définit explicitement les transitions autorisées pour la machine à états
VALID_TRANSITIONS: dict[SmartHRTState, set[SmartHRTState]] = {
    SmartHRTState.HEATING_ON: {SmartHRTState.DETECTING_LAG},
    SmartHRTState.DETECTING_LAG: {SmartHRTState.MONITORING},
    SmartHRTState.MONITORING: {SmartHRTState.RECOVERY, SmartHRTState.HEATING_PROCESS},
    SmartHRTState.RECOVERY: {SmartHRTState.HEATING_PROCESS},
    SmartHRTState.HEATING_PROCESS: {SmartHRTState.HEATING_ON},
}


@dataclass
class SmartHRTData:
    """Données du système SmartHRT"""

    # Configuration
    name: str = "SmartHRT"
    tsp: float = DEFAULT_TSP
    target_hour: dt_time = field(default_factory=lambda: dt_time(6, 0, 0))
    recoverycalc_hour: dt_time = field(default_factory=lambda: dt_time(23, 0, 0))

    # État courant de la machine à états (ADR-028: typé SmartHRTState)
    current_state: SmartHRTState = SmartHRTState.HEATING_ON

    # Modes (conservés pour compatibilité)
    smartheating_mode: bool = True
    recovery_adaptive_mode: bool = True
    recovery_calc_mode: bool = False
    rp_calc_mode: bool = False
    temp_lag_detection_active: bool = False

    # Coefficients thermiques
    rcth: float = DEFAULT_RCTH
    rpth: float = DEFAULT_RPTH
    rcth_lw: float = DEFAULT_RCTH
    rcth_hw: float = DEFAULT_RCTH
    rpth_lw: float = DEFAULT_RPTH
    rpth_hw: float = DEFAULT_RPTH
    rcth_fast: float = 0.0
    rcth_calculated: float = 0.0
    rpth_calculated: float = 0.0
    relaxation_factor: float = DEFAULT_RELAXATION_FACTOR

    # Températures actuelles
    interior_temp: float | None = None
    exterior_temp: float | None = None
    wind_speed: float = 0.0  # m/s
    windchill: float | None = None

    # Prévisions météo
    wind_speed_forecast_avg: float = 0.0  # km/h
    temperature_forecast_avg: float = 0.0  # °C
    wind_speed_avg: float = 0.0  # m/s - moyenne sur 4h

    # Températures de référence
    temp_recovery_calc: float = 17.0
    temp_recovery_start: float = 17.0
    temp_recovery_end: float = 17.0
    text_recovery_calc: float = 0.0
    text_recovery_start: float = 0.0
    text_recovery_end: float = 0.0

    # Timestamps
    time_recovery_calc: datetime | None = None
    time_recovery_start: datetime | None = None
    time_recovery_end: datetime | None = None
    recovery_start_hour: datetime | None = None
    recovery_update_hour: datetime | None = None

    # Délai de lag avant baisse température
    stop_lag_duration: float = 0.0  # secondes

    # ADR-013: Historique vent pour calcul de moyenne sur 4h
    # Permet de lisser les variations de vent pour un calcul plus stable
    wind_speed_history: deque = field(
        default_factory=lambda: deque(maxlen=240)
    )  # 4h à 1 sample/min

    # Erreurs du dernier cycle (pour diagnostic)
    last_rcth_error: float = 0.0
    last_rpth_error: float = 0.0


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

    def transition_to(self, new_state: SmartHRTState) -> bool:
        """Effectue une transition d'état si elle est valide (ADR-028).

        Vérifie que la transition est autorisée selon VALID_TRANSITIONS
        avant de changer l'état. Log un warning si la transition est invalide.

        Args:
            new_state: Le nouvel état cible (SmartHRTState)

        Returns:
            True si la transition a été effectuée, False sinon
        """
        current = self.data.current_state
        valid_targets = VALID_TRANSITIONS.get(current, set())

        if new_state in valid_targets:
            _LOGGER.info(
                "%s Transition %s → %s",
                self._log_prefix(),
                current.value,
                new_state.value,
            )
            self.data.current_state = new_state
            return True

        _LOGGER.warning(
            "%s Transition invalide %s → %s (autorisées: %s)",
            self._log_prefix(),
            current.value,
            new_state.value,
            ", ".join(s.value for s in valid_targets) if valid_targets else "aucune",
        )
        return False

    def force_state(self, new_state: SmartHRTState) -> None:
        """Force un changement d'état sans validation (ADR-028).

        À utiliser uniquement pour la restauration d'état ou les services
        administratifs. Log l'action pour traçabilité.

        Args:
            new_state: Le nouvel état à forcer (SmartHRTState)
        """
        old_state = self.data.current_state
        self.data.current_state = new_state
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
        """Configuration asynchrone du coordinateur"""
        _LOGGER.debug(
            "%s Configuration - TSP=%.1f°C, target_hour=%s, recoverycalc_hour=%s",
            self._log_prefix(),
            self.data.tsp,
            self.data.target_hour,
            self.data.recoverycalc_hour,
        )

        # Restore learned coefficients from storage
        await self._restore_learned_data()

        await self._update_initial_states()
        self._setup_listeners()
        self._setup_time_triggers()

        # Différer l'initialisation météo après le démarrage complet de HA
        # si l'entité météo n'est pas encore disponible
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
                # Entité météo déjà disponible, initialisation immédiate
                await self._complete_weather_setup()
        else:
            # Pas d'entité météo configurée
            await self._complete_weather_setup()

        # Restaurer les triggers et tâches périodiques selon l'état de la machine
        await self._restore_state_after_restart()

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

        This ensures that learned thermal constants (RCth, RPth) and the
        current state machine state survive Home Assistant restarts,
        as specified in the requirements.

        Uses PERSISTED_FIELDS mapping for automatic serialization,
        reducing maintenance burden when adding new fields.
        """
        stored_data = await self._store.async_load()
        if stored_data:
            _LOGGER.info(
                "%s Restoration des données apprises depuis le stockage",
                self._log_prefix(),
            )

            for storage_key, attr_name, default_value, field_type in PERSISTED_FIELDS:
                stored_value = stored_data.get(storage_key)

                if stored_value is None:
                    # Use default value if not in storage (None means keep current value)
                    if default_value is not None:
                        setattr(self.data, attr_name, default_value)
                elif field_type == "datetime":
                    # Parse ISO format datetime strings
                    try:
                        setattr(
                            self.data, attr_name, datetime.fromisoformat(stored_value)
                        )
                    except (ValueError, TypeError):
                        setattr(self.data, attr_name, default_value)
                elif field_type == "time":
                    # Parse time strings (HH:MM:SS or HH:MM)
                    try:
                        setattr(self.data, attr_name, self._parse_time(stored_value))
                    except (ValueError, TypeError):
                        # Keep current value if parsing fails
                        pass
                elif field_type == "list":
                    # Restore list to deque (for wind_speed_history)
                    if isinstance(stored_value, list):
                        current_deque = getattr(self.data, attr_name)
                        if isinstance(current_deque, deque):
                            current_deque.clear()
                            current_deque.extend(stored_value)
                        else:
                            setattr(
                                self.data, attr_name, deque(stored_value, maxlen=240)
                            )
                elif field_type == "state":
                    # ADR-028: Convert string to SmartHRTState enum
                    try:
                        setattr(self.data, attr_name, SmartHRTState(stored_value))
                    except ValueError:
                        # Invalid state string, use default
                        _LOGGER.warning(
                            "%s État invalide '%s', utilisation de '%s'",
                            self._log_prefix(),
                            stored_value,
                            default_value,
                        )
                        setattr(self.data, attr_name, SmartHRTState(default_value))
                else:
                    # Direct assignment for float, bool, str
                    setattr(self.data, attr_name, stored_value)

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

    async def _save_learned_data(self) -> None:
        """Save learned coefficients and state to persistent storage.

        Called after each state transition and learning cycle to persist
        the updated coefficients and state.

        Uses PERSISTED_FIELDS mapping for automatic serialization,
        reducing maintenance burden when adding new fields.
        """
        data_to_store = {}

        for storage_key, attr_name, _default_value, field_type in PERSISTED_FIELDS:
            value = getattr(self.data, attr_name)

            if field_type == "datetime":
                # Serialize datetime to ISO format string
                data_to_store[storage_key] = value.isoformat() if value else None
            elif field_type == "time":
                # Serialize time to HH:MM:SS string
                data_to_store[storage_key] = value.isoformat() if value else None
            elif field_type == "list":
                # Serialize deque to list (for wind_speed_history)
                if isinstance(value, deque):
                    data_to_store[storage_key] = list(value)
                else:
                    data_to_store[storage_key] = (
                        value if isinstance(value, list) else []
                    )
            elif field_type == "state":
                # ADR-028: StrEnum serializes to string automatically
                data_to_store[storage_key] = str(value) if value else None
            else:
                # Direct storage for float, bool, str
                data_to_store[storage_key] = value

        await self._store.async_save(data_to_store)
        _LOGGER.debug(
            "%s Données apprises et état sauvegardés en stockage", self._log_prefix()
        )

    def _setup_listeners(self) -> None:
        """Configure les listeners pour les capteurs"""
        sensors = [s for s in [self._interior_temp_sensor_id] if s]

        if sensors:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass, sensors, self._on_sensor_state_change
                )
            )

        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass, self._periodic_update, timedelta(minutes=1)
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
        """Callback lors d'un changement d'état"""
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
    def _periodic_update(self, _now) -> None:
        """Mise à jour périodique (chaque minute)

        Note: Les calculs de recovery_time sont gérés par recovery_update_hour
        selon une fréquence dynamique (fidèle au YAML original).
        """
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
        self.data.current_state = SmartHRTState.DETECTING_LAG
        self.data.temp_lag_detection_active = True
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
        self.data.current_state = SmartHRTState.MONITORING
        self.data.recovery_calc_mode = True
        self.data.temp_lag_detection_active = False
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
    # ADR-019: Restauration état après redémarrage
    # ─────────────────────────────────────────────────────────────────────────

    def _determine_expected_state_for_time(self, now: datetime) -> str:
        """Détermine l'état attendu de la machine à états basé sur l'heure actuelle.

        Cette méthode analyse l'heure actuelle par rapport aux heures configurées
        (target_hour, recoverycalc_hour, recovery_start_hour) pour déterminer
        dans quel état la machine devrait logiquement se trouver.

        Logique simplifiée:
        1. Si recovery_start_hour existe ET now >= recovery_start_hour ET now < target_hour:
           => HEATING_PROCESS (relance en cours)
        2. Sinon, on regarde l'heure actuelle:
           - Entre target_hour et recoverycalc_hour => HEATING_ON (jour)
           - Sinon (nuit) => MONITORING

        Returns:
            L'état attendu (SmartHRTState.*) pour l'heure donnée.
        """
        current_time = now.time()
        target = self.data.target_hour  # ex: 06:00 (time)
        recoverycalc = self.data.recoverycalc_hour  # ex: 23:00 (time)

        # recovery_start_hour est un datetime complet (ex: 2026-02-04 05:00:00)
        recovery_start_datetime = self.data.recovery_start_hour  # datetime ou None

        # 1. Si on a dépassé recovery_start_datetime => potentiellement HEATING_PROCESS
        if recovery_start_datetime and now >= recovery_start_datetime:
            # Mais on doit aussi vérifier qu'on n'a pas dépassé target_hour
            target_datetime_today = now.replace(
                hour=target.hour,
                minute=target.minute,
                second=0,
                microsecond=0,
            )
            # Si now est après target_hour du même jour que recovery_start_datetime
            if (
                now.date() == recovery_start_datetime.date()
                and now >= target_datetime_today
            ):
                return SmartHRTState.HEATING_ON
            # Si now est le jour suivant et après target_hour
            if now.date() > recovery_start_datetime.date() and current_time >= target:
                return SmartHRTState.HEATING_ON
            # On est bien en période de relance
            return SmartHRTState.HEATING_PROCESS

        # 2. Déterminer si on est en période "jour" (HEATING_ON) ou "nuit" (MONITORING)
        # Cas typique: target (matin) < recoverycalc (soir)
        if target < recoverycalc:
            if target <= current_time < recoverycalc:
                return SmartHRTState.HEATING_ON
            else:
                return SmartHRTState.MONITORING
        else:
            # Cas atypique: recoverycalc < target (ex: 13:30 < 17:30)
            if recoverycalc <= current_time < target:
                return SmartHRTState.MONITORING
            else:
                return SmartHRTState.HEATING_ON

    async def _restore_state_after_restart(self) -> None:
        """Restaure les triggers et tâches périodiques après redémarrage selon l'état.

        Cette méthode garantit que tous les mécanismes de la machine à états
        sont correctement reprogrammés après un redémarrage de Home Assistant.
        """
        persisted_state = self.data.current_state
        now = dt_util.now()

        # Déterminer l'état attendu basé sur l'heure actuelle
        expected_state = self._determine_expected_state_for_time(now)

        _LOGGER.info(
            "%s Restauration après redémarrage - État persisté: %s, État attendu: %s",
            self._log_prefix(),
            persisted_state,
            expected_state,
        )

        # Vérifier la cohérence entre état persisté et état attendu
        state_is_coherent = (
            persisted_state == expected_state
            or (
                persisted_state == SmartHRTState.DETECTING_LAG
                and expected_state == SmartHRTState.MONITORING
            )
            or (
                persisted_state == SmartHRTState.RECOVERY
                and expected_state == SmartHRTState.HEATING_PROCESS
            )
        )

        if not state_is_coherent:
            _LOGGER.warning(
                "%s État incohérent détecté - Correction: %s → %s",
                self._log_prefix(),
                persisted_state,
                expected_state,
            )
            await self._transition_to_expected_state(expected_state, now)
            return

        # État cohérent, restaurer les comportements selon l'état persisté
        current_state = persisted_state

        if current_state == SmartHRTState.MONITORING:
            # Reprogrammer le trigger de démarrage de relance si nécessaire
            if self.data.recovery_start_hour:
                if self.data.recovery_start_hour > now:
                    self._schedule_recovery_start(self.data.recovery_start_hour)
                else:
                    # L'heure de relance est dépassée, démarrer immédiatement
                    _LOGGER.info(
                        "%s Heure de relance dépassée, démarrage immédiat",
                        self._log_prefix(),
                    )
                    self.on_recovery_start()

        elif current_state == SmartHRTState.RECOVERY:
            # Transition vers HEATING_PROCESS
            self.data.current_state = SmartHRTState.HEATING_PROCESS
            self.data.rp_calc_mode = True
            await self._save_learned_data()

        elif current_state == SmartHRTState.HEATING_PROCESS:
            # Vérifier si target_hour est dépassée
            if self.data.target_hour:
                target_dt = now.replace(
                    hour=self.data.target_hour.hour,
                    minute=self.data.target_hour.minute,
                    second=0,
                    microsecond=0,
                )
                if target_dt < now and self.data.rp_calc_mode:
                    self.on_recovery_end()

        self.async_set_updated_data(self.data)

    async def _transition_to_expected_state(
        self, expected_state: str, now: datetime
    ) -> None:
        """Transition vers l'état attendu après détection d'incohérence.

        Args:
            expected_state: L'état vers lequel transitionner (SmartHRTState.*)
            now: L'heure actuelle
        """
        _LOGGER.info(
            "%s Transition forcée vers l'état %s",
            self._log_prefix(),
            expected_state,
        )

        if expected_state == SmartHRTState.HEATING_ON:
            self.data.current_state = SmartHRTState.HEATING_ON
            self.data.recovery_calc_mode = False
            self.data.rp_calc_mode = False
            self.data.temp_lag_detection_active = False

        elif expected_state == SmartHRTState.MONITORING:
            self.data.current_state = SmartHRTState.MONITORING
            self.data.recovery_calc_mode = True
            self.data.rp_calc_mode = False
            self.data.temp_lag_detection_active = False

            # Initialiser les valeurs de référence si non définies
            if (
                self.data.temp_recovery_calc is None
                or self.data.temp_recovery_calc == 0
            ):
                self.data.temp_recovery_calc = self.data.interior_temp or 17.0
            if self.data.text_recovery_calc is None:
                self.data.text_recovery_calc = self.data.exterior_temp or 0.0

            # Programmer les triggers
            if self.data.recovery_start_hour and self.data.recovery_start_hour > now:
                self._schedule_recovery_start(self.data.recovery_start_hour)

        elif expected_state == SmartHRTState.HEATING_PROCESS:
            self.data.current_state = SmartHRTState.HEATING_PROCESS
            self.data.recovery_calc_mode = False
            self.data.rp_calc_mode = True
            self.data.temp_lag_detection_active = False

            # Initialiser les valeurs de début de relance si non définies
            if self.data.time_recovery_start is None:
                if self.data.recovery_start_hour:
                    self.data.time_recovery_start = self.data.recovery_start_hour
                else:
                    self.data.time_recovery_start = now
            if (
                self.data.temp_recovery_start is None
                or self.data.temp_recovery_start == 0
            ):
                self.data.temp_recovery_start = self.data.interior_temp or 17.0
            if self.data.text_recovery_start is None:
                self.data.text_recovery_start = self.data.exterior_temp or 0.0

        elif expected_state == SmartHRTState.DETECTING_LAG:
            self.data.current_state = SmartHRTState.DETECTING_LAG
            self.data.recovery_calc_mode = False
            self.data.rp_calc_mode = False
            self.data.temp_lag_detection_active = True
            self.data.temp_recovery_calc = self.data.interior_temp or 17.0
            self.data.text_recovery_calc = self.data.exterior_temp or 0.0
            self.data.time_recovery_calc = now

        # Sauvegarder le nouvel état
        await self._save_learned_data()
        self.async_set_updated_data(self.data)

    def on_heating_stop(self) -> None:
        """Appelé quand le chauffage s'arrête (service manuel)"""
        self.data.time_recovery_calc = dt_util.now()
        self.data.temp_recovery_calc = self.data.interior_temp or 17.0
        self.data.text_recovery_calc = self.data.exterior_temp or 0.0
        self.data.temp_lag_detection_active = True
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
        # Annuler le trigger de recovery_start pour éviter les redéclenchements
        if self._unsub_recovery_start:
            self._unsub_recovery_start()
            self._unsub_recovery_start = None

        # Transition vers RECOVERY (État 4)
        self.data.current_state = SmartHRTState.RECOVERY
        _LOGGER.debug("%s Transition vers état RECOVERY", self._log_prefix())

        self.data.time_recovery_start = dt_util.now()
        self.data.temp_recovery_start = self.data.interior_temp or 17.0
        self.data.text_recovery_start = self.data.exterior_temp or 0.0

        self.calculate_rcth_at_recovery_start()

        self.data.rp_calc_mode = True
        self.data.recovery_calc_mode = False
        self.data.temp_lag_detection_active = False

        # Transition vers HEATING_PROCESS (État 5) - chauffage en cours
        self.data.current_state = SmartHRTState.HEATING_PROCESS
        _LOGGER.debug("%s Transition vers état HEATING_PROCESS", self._log_prefix())

        _LOGGER.info(
            "%s Début de relance - Tint=%.1f°C, RCth calculé=%.2f",
            self._log_prefix(),
            self.data.temp_recovery_start,
            self.data.rcth_calculated,
        )

        # Sauvegarder l'état après la transition
        self.hass.async_create_task(self._save_learned_data())

        self.async_set_updated_data(self.data)

    def on_recovery_end(self) -> None:
        """Appelé à la fin de la relance (consigne atteinte ou target_hour)
        Équivalent de l'automation 'recoveryendTIME' du YAML

        Transition: HEATING_PROCESS → HEATING_ON (État 5 → État 1)
        """
        if not self.data.rp_calc_mode:
            return

        self.data.time_recovery_end = dt_util.now()
        self.data.temp_recovery_end = self.data.interior_temp or 17.0
        self.data.text_recovery_end = self.data.exterior_temp or 0.0

        self.calculate_rpth_at_recovery_end()

        self.data.rp_calc_mode = False

        # Transition vers HEATING_ON (État 1) - cycle terminé
        self.data.current_state = SmartHRTState.HEATING_ON
        _LOGGER.debug(
            "%s Transition vers état HEATING_ON - Cycle terminé", self._log_prefix()
        )

        _LOGGER.info(
            "%s Fin de relance - Tint=%.1f°C, RPth calculé=%.2f",
            self._log_prefix(),
            self.data.temp_recovery_end,
            self.data.rpth_calculated,
        )

        # Sauvegarder l'état après la transition (coefficients mis à jour)
        self.hass.async_create_task(self._save_learned_data())

        self.async_set_updated_data(self.data)

    def _on_recovery_end(self) -> None:
        """Ancienne méthode interne - redirige vers on_recovery_end"""
        self.on_recovery_end()

    # ─────────────────────────────────────────────────────────────────────────
    # Setters publics
    # ─────────────────────────────────────────────────────────────────────────

    def set_tsp(self, value: float) -> None:
        self.data.tsp = value
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

    def set_target_hour(self, value: dt_time) -> None:
        self.data.target_hour = value
        self._setup_time_triggers()  # Reconfigure les triggers
        self.calculate_recovery_time()
        self.async_set_updated_data(self.data)
        # Persister la nouvelle valeur
        self.hass.async_create_task(self._save_learned_data())

    def set_recoverycalc_hour(self, value: dt_time) -> None:
        """Définit l'heure de coupure chauffage"""
        self.data.recoverycalc_hour = value
        self._setup_time_triggers()  # Reconfigure les triggers
        self.async_set_updated_data(self.data)
        # Persister la nouvelle valeur
        self.hass.async_create_task(self._save_learned_data())

    def set_smartheating_mode(self, value: bool) -> None:
        self.data.smartheating_mode = value
        self.async_set_updated_data(self.data)

    def set_recovery_adaptive_mode(self, value: bool) -> None:
        self.data.recovery_adaptive_mode = value
        self.async_set_updated_data(self.data)

    def set_adaptive_mode(self, value: bool) -> None:
        self.set_recovery_adaptive_mode(value)

    def set_rcth(self, value: float) -> None:
        self.data.rcth = value
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

    def set_rpth(self, value: float) -> None:
        self.data.rpth = value
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

    def set_relaxation_factor(self, value: float) -> None:
        self.data.relaxation_factor = value
        self.async_set_updated_data(self.data)

    def set_rcth_lw(self, value: float) -> None:
        self.data.rcth_lw = value
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

    def set_rcth_hw(self, value: float) -> None:
        self.data.rcth_hw = value
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

    def set_rpth_lw(self, value: float) -> None:
        self.data.rpth_lw = value
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

    def set_rpth_hw(self, value: float) -> None:
        self.data.rpth_hw = value
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
