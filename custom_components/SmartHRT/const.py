"""Les constantes pour l'intégration SmartHRT.

ADR implémentées dans ce module:
- ADR-004: Définition PERSISTED_FIELDS pour persistance hybride
- ADR-009: Mapping centralisé des champs persistés (coefficients)
"""

from homeassistant.const import Platform

DOMAIN = "smarthrt"
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.TIME,
    Platform.SWITCH,
]

# Configuration keys
CONF_NAME = "name"
CONF_DEVICE_ID = "device_id"
CONF_TARGET_HOUR = "target_hour"
CONF_RECOVERYCALC_HOUR = "recoverycalc_hour"
CONF_SENSOR_INTERIOR_TEMP = "sensor_interior_temperature"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_TSP = "tsp"

# Default values
DEFAULT_TSP = 19.0
DEFAULT_TSP_MIN = 13.0
DEFAULT_TSP_MAX = 26.0
DEFAULT_TSP_STEP = 0.1

# Thermal coefficients defaults
DEFAULT_RCTH = 50.0
DEFAULT_RPTH = 50.0
DEFAULT_RCTH_MIN = 0.0
DEFAULT_RCTH_MAX = 19999.0
DEFAULT_RPTH_MIN = 0.0
DEFAULT_RPTH_MAX = 19999.0
DEFAULT_RELAXATION_FACTOR = 2.0

# ADR-007: Compensation météo - seuils de vent pour interpolation
# WIND_LOW: vent faible (utilise rcth_lw), WIND_HIGH: vent fort (utilise rcth_hw)
WIND_HIGH = 60.0
WIND_LOW = 10.0

# Device info
DEVICE_MANUFACTURER = "SmartHRT"

# Data keys for hass.data[DOMAIN][entry_id]
DATA_COORDINATOR = "coordinator"

# Service names - Nouveaux services simplifiés
SERVICE_START_HEATING_CYCLE = "start_heating_cycle"
SERVICE_STOP_HEATING = "stop_heating"
SERVICE_START_RECOVERY = "start_recovery"
SERVICE_END_RECOVERY = "end_recovery"
SERVICE_GET_STATE = "get_state"

# Service names - Services utilitaires
SERVICE_RESET_LEARNING = "reset_learning"
SERVICE_TRIGGER_CALCULATION = "trigger_calculation"

# Service names - Services historiques (conservés pour compatibilité)
SERVICE_CALCULATE_RECOVERY_TIME = "calculate_recovery_time"
SERVICE_CALCULATE_RECOVERY_UPDATE_TIME = "calculate_recovery_update_time"
SERVICE_CALCULATE_RCTH_FAST = "calculate_rcth_fast"
SERVICE_ON_HEATING_STOP = "on_heating_stop"
SERVICE_ON_RECOVERY_START = "on_recovery_start"
SERVICE_ON_RECOVERY_END = "on_recovery_end"

# Weather forecast settings
FORECAST_HOURS = 3

# ADR-008: Validation arrêt par détection lag
# Seuil de baisse de température pour confirmer l'arrêt réel du chauffage
TEMP_DECREASE_THRESHOLD = 0.2  # °C

# Default recoverycalc hour (23:00)
DEFAULT_RECOVERYCALC_HOUR = "23:00:00"

# ADR-004 & ADR-009: Mapping centralisé pour persistance hybride
# Chaque tuple définit: (clé stockage, attribut data, valeur par défaut, type)
# Les types supportés: "float", "bool", "str", "datetime" (isoformat), "time" (HH:MM:SS)
# Ce mapping est utilisé par coordinator._save/_restore_learned_data()
PERSISTED_FIELDS: list[tuple[str, str, object, str]] = [
    # Heures configurables (modifiables via l'interface)
    ("target_hour", "target_hour", None, "time"),
    ("recoverycalc_hour", "recoverycalc_hour", None, "time"),
    # Coefficients thermiques
    ("rcth", "rcth", DEFAULT_RCTH, "float"),
    ("rpth", "rpth", DEFAULT_RPTH, "float"),
    ("rcth_lw", "rcth_lw", DEFAULT_RCTH, "float"),
    ("rcth_hw", "rcth_hw", DEFAULT_RCTH, "float"),
    ("rpth_lw", "rpth_lw", DEFAULT_RPTH, "float"),
    ("rpth_hw", "rpth_hw", DEFAULT_RPTH, "float"),
    ("last_rcth_error", "last_rcth_error", 0.0, "float"),
    ("last_rpth_error", "last_rpth_error", 0.0, "float"),
    # État de la machine à états
    ("current_state", "current_state", "heating_on", "str"),
    ("recovery_calc_mode", "recovery_calc_mode", False, "bool"),
    ("rp_calc_mode", "rp_calc_mode", False, "bool"),
    ("temp_lag_detection_active", "temp_lag_detection_active", False, "bool"),
    ("stop_lag_duration", "stop_lag_duration", 0.0, "float"),
    # Historique vent (ADR-013: moyenne 4h pour calcul coefficients)
    ("wind_speed_history", "wind_speed_history", [], "list"),
    # Prévisions météo (ADR-002: persistées pour continuité au redémarrage)
    ("temperature_forecast_avg", "temperature_forecast_avg", 0.0, "float"),
    ("wind_speed_forecast_avg", "wind_speed_forecast_avg", 0.0, "float"),
    # Données de session
    ("recovery_start_hour", "recovery_start_hour", None, "datetime"),
    ("time_recovery_calc", "time_recovery_calc", None, "datetime"),
    ("temp_recovery_calc", "temp_recovery_calc", 17.0, "float"),
    ("text_recovery_calc", "text_recovery_calc", 0.0, "float"),
]
