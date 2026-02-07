"""Modèles Pydantic pour la validation des données SmartHRT (ADR-029).

Ce module définit les schémas de validation pour :
- Les données persistées (coefficients thermiques, état, etc.)
- Les entrées de configuration (config_flow)

Utilise Pydantic v2 pour la validation automatique des types et ranges.
"""

from datetime import datetime, time as dt_time
from typing import Any
from collections import deque
import logging

from pydantic import BaseModel, Field, field_validator, model_validator

from .const import (
    DEFAULT_RCTH,
    DEFAULT_RPTH,
    DEFAULT_RCTH_MIN,
    DEFAULT_RCTH_MAX,
    DEFAULT_RELAXATION_FACTOR,
    DEFAULT_TSP,
    DEFAULT_TSP_MIN,
    DEFAULT_TSP_MAX,
)

_LOGGER = logging.getLogger(__name__)


class PersistedDataModel(BaseModel):
    """Modèle Pydantic pour les données persistées (ADR-029).

    Valide automatiquement les types et ranges des données chargées
    depuis le stockage persistant.
    """

    # Coefficients thermiques avec validation de range
    rcth: float = Field(default=DEFAULT_RCTH, ge=DEFAULT_RCTH_MIN, le=DEFAULT_RCTH_MAX)
    rpth: float = Field(default=DEFAULT_RPTH, ge=DEFAULT_RCTH_MIN, le=DEFAULT_RCTH_MAX)
    rcth_lw: float = Field(default=DEFAULT_RCTH, ge=0)
    rcth_hw: float = Field(default=DEFAULT_RCTH, ge=0)
    rpth_lw: float = Field(default=DEFAULT_RPTH, ge=0)
    rpth_hw: float = Field(default=DEFAULT_RPTH, ge=0)
    last_rcth_error: float = Field(default=0.0)
    last_rpth_error: float = Field(default=0.0)

    # État machine (string, sera converti en SmartHRTState par le coordinator)
    current_state: str = Field(default="heating_on")

    # Durée du lag de température
    stop_lag_duration: float = Field(default=0.0, ge=0)

    # Heures configurées (optionnelles)
    target_hour: dt_time | None = Field(default=None)
    recoverycalc_hour: dt_time | None = Field(default=None)

    # Snapshots de session
    time_recovery_calc: datetime | None = Field(default=None)
    temp_recovery_calc: float = Field(default=17.0)
    text_recovery_calc: float = Field(default=0.0)

    # Trigger programmé
    recovery_start_hour: datetime | None = Field(default=None)

    # Prévisions météo
    temperature_forecast_avg: float = Field(default=0.0)
    wind_speed_forecast_avg: float = Field(default=0.0, ge=0)

    # Historique vent (liste, sera convertie en deque)
    wind_speed_history: list[float] = Field(default_factory=list)

    model_config = {
        "extra": "ignore",  # Ignore les champs inconnus (migration)
        "validate_assignment": True,  # Valide aussi lors des assignations
    }

    @field_validator("current_state")
    @classmethod
    def validate_state(cls, v: str) -> str:
        """Valide que l'état est connu, sinon fallback sur heating_on."""
        valid_states = {
            "heating_on",
            "detecting_lag",
            "monitoring",
            "recovery",
            "heating_process",
        }
        if v not in valid_states:
            _LOGGER.warning("État invalide '%s', utilisation de 'heating_on'", v)
            return "heating_on"
        return v

    @field_validator("rcth", "rpth", "rcth_lw", "rcth_hw", "rpth_lw", "rpth_hw")
    @classmethod
    def clamp_coefficients(cls, v: float) -> float:
        """S'assure que les coefficients sont dans les limites."""
        if v < DEFAULT_RCTH_MIN:
            return DEFAULT_RCTH_MIN
        if v > DEFAULT_RCTH_MAX:
            return DEFAULT_RCTH_MAX
        return v

    @field_validator("wind_speed_history", mode="before")
    @classmethod
    def ensure_list(cls, v: Any) -> list[float]:
        """Convertit en liste si nécessaire."""
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [float(x) for x in v if x is not None]
        if isinstance(v, deque):
            return list(v)
        return []


class ConfigFlowDataModel(BaseModel):
    """Modèle Pydantic pour les données du config flow (ADR-032).

    Valide les entrées utilisateur lors de la configuration.
    """

    name: str = Field(min_length=1, max_length=100)
    target_hour: str  # Format "HH:MM:SS"
    recoverycalc_hour: str = Field(default="23:00:00")
    sensor_interior_temperature: str  # Entity ID
    weather_entity: str  # Entity ID
    tsp: float = Field(default=DEFAULT_TSP, ge=DEFAULT_TSP_MIN, le=DEFAULT_TSP_MAX)

    model_config = {
        "extra": "ignore",
    }

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Valide que le nom n'est pas vide."""
        v = v.strip()
        if not v:
            raise ValueError("Le nom ne peut pas être vide")
        return v

    @field_validator("target_hour", "recoverycalc_hour")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Valide le format de l'heure."""
        try:
            parts = v.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Heure invalide")
        except (ValueError, IndexError) as e:
            raise ValueError(f"Format d'heure invalide: {v}") from e
        return v

    @model_validator(mode="after")
    def validate_time_sequence(self) -> "ConfigFlowDataModel":
        """Valide que recoverycalc_hour précède target_hour (passage à minuit).

        La logique: recoverycalc (23:00) doit être le soir, target (06:00) le matin.
        Si recoverycalc < target (même journée), c'est une erreur.
        """
        try:
            rc_parts = self.recoverycalc_hour.split(":")
            tg_parts = self.target_hour.split(":")
            rc_minutes = int(rc_parts[0]) * 60 + int(
                rc_parts[1] if len(rc_parts) > 1 else 0
            )
            tg_minutes = int(tg_parts[0]) * 60 + int(
                tg_parts[1] if len(tg_parts) > 1 else 0
            )

            # Cas invalide: recoverycalc (ex: 05:00) < target (06:00) sur même journée
            # Cas valide: recoverycalc (23:00) > target (06:00) implique passage à minuit
            # Ou target très tôt (avant midi) est OK même si recoverycalc proche
            if rc_minutes < tg_minutes and tg_minutes >= 12 * 60:
                raise ValueError(
                    "L'heure de coupure doit précéder l'heure cible (passage à minuit)"
                )
        except (ValueError, IndexError):
            pass  # Les erreurs de parsing sont gérées par validate_time_format
        return self


def validate_persisted_data(data: dict[str, Any]) -> dict[str, Any]:
    """Valide et normalise les données persistées.

    Args:
        data: Dictionnaire de données brutes depuis le stockage

    Returns:
        Dictionnaire validé avec valeurs corrigées si nécessaire

    Note:
        Ne lève pas d'exception, utilise des valeurs par défaut en cas d'erreur.
    """
    try:
        model = PersistedDataModel.model_validate(data)
        return model.model_dump()
    except Exception as e:
        _LOGGER.warning("Erreur de validation des données persistées: %s", e)
        # Retourne les valeurs par défaut
        return PersistedDataModel().model_dump()


def validate_config_flow_data(
    data: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Valide les données du config flow.

    Args:
        data: Données saisies par l'utilisateur

    Returns:
        Tuple (données_validées, erreurs)
        - données_validées: None si erreurs, sinon dict validé
        - erreurs: dict {champ: code_erreur} pour affichage UI
    """
    errors: dict[str, str] = {}

    try:
        model = ConfigFlowDataModel.model_validate(data)
        return model.model_dump(), errors
    except Exception as e:
        # Parser les erreurs Pydantic pour les mapper aux champs
        error_str = str(e)
        if "tsp" in error_str.lower():
            errors["tsp"] = "tsp_out_of_range"
        elif "name" in error_str.lower():
            errors["name"] = "invalid_name"
        elif "heure" in error_str.lower() or "time" in error_str.lower():
            errors["base"] = "invalid_time_sequence"
        else:
            errors["base"] = "unknown_error"

        _LOGGER.debug("Erreur de validation config flow: %s", e)
        return None, errors
