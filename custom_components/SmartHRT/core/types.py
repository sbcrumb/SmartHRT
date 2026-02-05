"""Dataclasses partagées pour le modèle thermique SmartHRT.

ADR-026: Types Pure Python sans dépendance Home Assistant.

Ces dataclasses représentent:
- ThermalState: État thermique instantané (températures, vent)
- ThermalCoefficients: Coefficients appris (RCth, RPth avec interpolation vent)
- ThermalConfig: Configuration statique (seuils de vent, constantes)
"""

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time


@dataclass
class ThermalConfig:
    """Configuration statique pour les calculs thermiques.

    Ces valeurs sont généralement définies une fois et ne changent pas
    pendant l'exécution (sauf via reconfiguration).
    """

    # Seuils de vent pour interpolation (ADR-007)
    wind_low_kmh: float = 10.0  # Vent faible
    wind_high_kmh: float = 60.0  # Vent fort

    # Nombre d'itérations pour le calcul de recovery time (ADR-022)
    recovery_iterations: int = 20

    # Valeurs par défaut des coefficients
    default_rcth: float = 50.0
    default_rpth: float = 50.0
    default_relaxation_factor: float = 2.0

    # Limites des coefficients
    coef_min: float = 0.1
    coef_max: float = 19999.0


@dataclass
class ThermalCoefficients:
    """Coefficients thermiques appris (ADR-006).

    Représente les coefficients RCth et RPth avec leurs variantes
    pour différentes conditions de vent (interpolation ADR-007).
    """

    # Coefficients principaux
    rcth: float = 50.0  # Constante de temps de refroidissement
    rpth: float = 50.0  # Constante de puissance de chauffage

    # Coefficients interpolés selon le vent (ADR-007)
    rcth_lw: float = 50.0  # RCth par vent faible (low wind)
    rcth_hw: float = 50.0  # RCth par vent fort (high wind)
    rpth_lw: float = 50.0  # RPth par vent faible
    rpth_hw: float = 50.0  # RPth par vent fort

    # Derniers coefficients calculés (avant relaxation)
    rcth_calculated: float = 0.0
    rpth_calculated: float = 0.0

    # RCth dynamique calculé pendant le refroidissement
    rcth_fast: float = 0.0

    # Facteur de relaxation pour l'apprentissage (ADR-006)
    relaxation_factor: float = 2.0

    # Erreurs du dernier cycle (pour diagnostic)
    last_rcth_error: float = 0.0
    last_rpth_error: float = 0.0


@dataclass
class ThermalState:
    """État thermique instantané (ADR-026).

    Représente l'ensemble des données nécessaires pour un calcul
    thermique à un instant donné. Aucune référence à Home Assistant,
    utilise datetime standard.
    """

    # Températures actuelles
    interior_temp: float | None = None  # Température intérieure (°C)
    exterior_temp: float | None = None  # Température extérieure (°C)
    windchill: float | None = None  # Température ressentie (°C)

    # Vent actuel
    wind_speed_ms: float = 0.0  # Vitesse du vent (m/s)
    wind_speed_avg_ms: float = 0.0  # Moyenne 4h du vent (m/s) - ADR-013

    # Prévisions météo
    temperature_forecast_avg: float = 0.0  # Moyenne température prévue (°C)
    wind_speed_forecast_avg_kmh: float = 0.0  # Moyenne vent prévu (km/h)

    # Consigne
    tsp: float = 19.0  # Température de consigne (setpoint)

    # Heures cibles
    target_hour: dt_time = field(default_factory=lambda: dt_time(6, 0, 0))

    # Timestamps de référence pour les calculs
    now: datetime | None = None  # Instant actuel

    # Températures de référence (snapshots)
    temp_recovery_calc: float = 17.0  # Temp intérieure au début refroidissement
    text_recovery_calc: float = 0.0  # Temp extérieure au début refroidissement
    temp_recovery_start: float = 17.0  # Temp intérieure au démarrage relance
    text_recovery_start: float = 0.0  # Temp extérieure au démarrage relance
    temp_recovery_end: float = 17.0  # Temp intérieure à la fin relance
    text_recovery_end: float = 0.0  # Temp extérieure à la fin relance

    # Timestamps de référence
    time_recovery_calc: datetime | None = None  # Début du refroidissement
    time_recovery_start: datetime | None = None  # Démarrage de la relance
    time_recovery_end: datetime | None = None  # Fin de la relance


@dataclass
class RecoveryResult:
    """Résultat d'un calcul de temps de relance.

    Retourné par ThermalSolver.calculate_recovery_duration().
    """

    recovery_start_hour: datetime  # Heure calculée pour démarrer la relance
    duration_hours: float  # Durée estimée de la relance (heures)


@dataclass
class CoefficientUpdateResult:
    """Résultat d'une mise à jour de coefficients.

    Retourné par ThermalSolver.update_coefficients().
    """

    # Nouveaux coefficients low/high wind
    coef_lw: float
    coef_hw: float
    # Nouveau coefficient principal (avec relaxation)
    coef_main: float
    # Erreur observée (pour diagnostic)
    error: float
