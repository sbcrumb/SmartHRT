"""Modèle thermique Pure Python pour SmartHRT.

ADR-026: Extraction du modèle thermique en Pure Python.

Ce module contient la classe ThermalSolver qui implémente tous les calculs
de physique thermique sans aucune dépendance à Home Assistant:
- Calcul du temps de relance (ADR-005, ADR-022)
- Interpolation selon le vent (ADR-007)
- Apprentissage des coefficients (ADR-006)
- Calcul du windchill
"""

import math
from datetime import datetime, timedelta, time as dt_time
from typing import Tuple

from .types import (
    ThermalState,
    ThermalCoefficients,
    ThermalConfig,
    RecoveryResult,
    CoefficientUpdateResult,
)


class ThermalSolver:
    """Solveur thermique Pure Python (ADR-026).

    Implémente tous les calculs de physique thermique:
    - calculate_recovery_duration: temps nécessaire pour atteindre la consigne
    - calculate_windchill: température ressentie
    - interpolate_for_wind: interpolation des coefficients selon le vent
    - update_coefficients: apprentissage avec relaxation (ADR-006)
    - calculate_rcth_fast: RCth dynamique pendant refroidissement
    - calculate_rcth_at_recovery: RCth au moment de la relance
    - calculate_rpth_at_recovery: RPth à la fin de la relance

    Cette classe ne dépend d'aucun élément Home Assistant et peut être
    testée unitairement avec des données synthétiques.
    """

    def __init__(self, config: ThermalConfig | None = None) -> None:
        """Initialise le solveur avec une configuration optionnelle.

        Args:
            config: Configuration statique (seuils de vent, etc.)
                    Si None, utilise les valeurs par défaut.
        """
        self.config = config or ThermalConfig()

    # ─────────────────────────────────────────────────────────────────────────
    # Calcul du windchill (température ressentie)
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_windchill(self, temp_celsius: float, wind_speed_ms: float) -> float:
        """Calcule la température ressentie (windchill).

        Utilise la formule JAG/TI (Joint Action Group for Temperature Indices):
        - Active si temp < 10°C et vent > 1.34 m/s (4.824 km/h)
        - Sinon retourne la température brute

        Args:
            temp_celsius: Température de l'air en °C
            wind_speed_ms: Vitesse du vent en m/s

        Returns:
            Température ressentie en °C, arrondie à 0.1°C
        """
        wind_kmh = wind_speed_ms * 3.6

        # Formule de windchill (JAG/TI)
        # Active si temp < 10°C et vent > 1.34 m/s (4.824 km/h)
        if temp_celsius < 10 and wind_speed_ms > 1.34:
            windchill = (
                13.12
                + 0.6215 * temp_celsius
                - 11.37 * wind_kmh**0.16
                + 0.3965 * temp_celsius * wind_kmh**0.16
            )
            return round(windchill, 1)
        else:
            return temp_celsius

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-007: Interpolation linéaire selon le vent
    # ─────────────────────────────────────────────────────────────────────────

    def interpolate_for_wind(
        self,
        value_low_wind: float,
        value_high_wind: float,
        wind_kmh: float,
    ) -> float:
        """Interpole une valeur en fonction du vent (ADR-007).

        Pour une vitesse de vent donnée, calcule une valeur interpolée
        entre la valeur par vent faible et la valeur par vent fort.

        Args:
            value_low_wind: Valeur à utiliser par vent faible (WIND_LOW)
            value_high_wind: Valeur à utiliser par vent fort (WIND_HIGH)
            wind_kmh: Vitesse du vent actuelle en km/h

        Returns:
            Valeur interpolée, minimum 0.1
        """
        wind_low = self.config.wind_low_kmh
        wind_high = self.config.wind_high_kmh

        # Clamp la vitesse du vent entre les bornes
        wind_clamped = max(wind_low, min(wind_high, wind_kmh))

        # Ratio: 1.0 à wind_low (utilise low), 0.0 à wind_high (utilise high)
        ratio = (wind_high - wind_clamped) / (wind_high - wind_low)

        # Interpolation linéaire
        result = value_high_wind + (value_low_wind - value_high_wind) * ratio

        return max(self.config.coef_min, result)

    def get_interpolated_rcth(
        self, coefficients: ThermalCoefficients, wind_kmh: float
    ) -> float:
        """Retourne RCth interpolé selon le vent."""
        return self.interpolate_for_wind(
            coefficients.rcth_lw, coefficients.rcth_hw, wind_kmh
        )

    def get_interpolated_rpth(
        self, coefficients: ThermalCoefficients, wind_kmh: float
    ) -> float:
        """Retourne RPth interpolé selon le vent."""
        return self.interpolate_for_wind(
            coefficients.rpth_lw, coefficients.rpth_hw, wind_kmh
        )

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-005 & ADR-022: Calcul du temps de relance
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_recovery_duration(
        self,
        state: ThermalState,
        coefficients: ThermalCoefficients,
        now: datetime,
    ) -> RecoveryResult:
        """Calcule l'heure de démarrage de la relance (ADR-005, ADR-022).

        Utilise les prévisions météo et 20 itérations pour affiner la prédiction.
        Le calcul prend en compte:
        - La température intérieure actuelle
        - Les prévisions météo (température, vent)
        - Les coefficients RCth/RPth interpolés selon le vent

        Args:
            state: État thermique actuel
            coefficients: Coefficients thermiques appris
            now: Instant actuel (datetime)

        Returns:
            RecoveryResult avec l'heure de démarrage et la durée estimée
        """
        # Utiliser 17°C par défaut si la température intérieure n'est pas disponible
        tint = state.interior_temp if state.interior_temp is not None else 17.0

        # Utiliser les prévisions météo
        text = (
            state.temperature_forecast_avg
            if state.temperature_forecast_avg
            else (state.exterior_temp or 0.0)
        )
        tsp = state.tsp

        # Utiliser les prévisions de vent
        wind_kmh = (
            state.wind_speed_forecast_avg_kmh
            if state.wind_speed_forecast_avg_kmh
            else (state.wind_speed_ms * 3.6)
        )

        rcth = self.get_interpolated_rcth(coefficients, wind_kmh)
        rpth = self.get_interpolated_rpth(coefficients, wind_kmh)

        # Calculer l'heure cible (target_hour)
        target_dt = now.replace(
            hour=state.target_hour.hour,
            minute=state.target_hour.minute,
            second=0,
            microsecond=0,
        )
        if target_dt < now:
            target_dt += timedelta(days=1)

        time_remaining = (target_dt - now).total_seconds() / 3600
        max_duration = max(time_remaining - 1 / 6, 0)  # 10 min de marge

        # Calcul initial de la durée de relance
        try:
            ratio = (rpth + text - tint) / (rpth + text - tsp)
            duree_relance = min(max(rcth * math.log(max(ratio, 0.1)), 0), max_duration)
        except (ValueError, ZeroDivisionError):
            duree_relance = max_duration

        # Prédiction itérative (ADR-022: 20 itérations)
        for _ in range(self.config.recovery_iterations):
            try:
                # Estimer la température intérieure au moment du démarrage
                tint_start = text + (tint - text) / math.exp(
                    (time_remaining - duree_relance) / rcth
                )
                ratio = (rpth + text - tint_start) / (rpth + text - tsp)
                if ratio > 0.1:
                    # Moyenne pondérée pour éviter les oscillations
                    duree_relance = min(
                        (duree_relance + 2 * max(rcth * math.log(ratio), 0)) / 3,
                        max_duration,
                    )
            except (ValueError, ZeroDivisionError):
                break

        recovery_start_hour = target_dt - timedelta(seconds=int(duree_relance * 3600))

        return RecoveryResult(
            recovery_start_hour=recovery_start_hour,
            duration_hours=duree_relance,
        )

    def calculate_recovery_update_time(
        self,
        recovery_start_hour: datetime,
        now: datetime,
    ) -> datetime | None:
        """Calcule l'heure de la prochaine mise à jour du calcul de relance.

        La logique:
        - Reconstruit recoverystart_time à partir de l'heure de recovery_start_hour
        - Calcule le temps restant avant la relance
        - Reprogramme dans max(time_remaining/3, 0) secondes, plafonné à 1200s (20min)
        - À moins de 30min avant la relance, arrête en programmant dans 3600s

        Args:
            recovery_start_hour: Heure prévue de démarrage de la relance
            now: Instant actuel

        Returns:
            Datetime de la prochaine mise à jour, ou None si non calculable
        """
        if recovery_start_hour is None:
            return None

        # Reconstruire recoverystart_time depuis l'heure
        recovery_hour = recovery_start_hour.hour
        recovery_minute = recovery_start_hour.minute
        recoverystart_time = now.replace(
            hour=recovery_hour,
            minute=recovery_minute,
            second=0,
            microsecond=0,
        )
        if recoverystart_time < now:
            recoverystart_time += timedelta(days=1)

        time_remaining = (recoverystart_time - now).total_seconds()

        # Recalcule pas plus tard que dans 1200s (20min)
        # À moins de 30min avant la relance on arrête
        if time_remaining < 1800:
            seconds = 3600  # Impose un calcul après la relance
        else:
            seconds = min(max(time_remaining / 3, 0), 1200)

        return now + timedelta(seconds=seconds)

    # ─────────────────────────────────────────────────────────────────────────
    # Calcul dynamique des coefficients
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_rcth_fast(
        self,
        interior_temp: float,
        exterior_temp: float,
        temp_at_start: float,
        text_at_start: float,
        time_since_start_hours: float,
    ) -> float | None:
        """Calcule l'évolution dynamique de RCth pendant le refroidissement.

        Ce calcul permet de suivre l'évolution du coefficient thermique
        en temps réel pendant la phase de refroidissement.

        Args:
            interior_temp: Température intérieure actuelle (°C)
            exterior_temp: Température extérieure actuelle (°C)
            temp_at_start: Température intérieure au début du refroidissement (°C)
            text_at_start: Température extérieure au début du refroidissement (°C)
            time_since_start_hours: Temps écoulé depuis le début (heures)

        Returns:
            RCth calculé dynamiquement, ou None si calcul impossible
        """
        if time_since_start_hours < 0:
            time_since_start_hours += 24

        avg_text = (text_at_start + exterior_temp) / 2

        if interior_temp < temp_at_start and interior_temp > avg_text:
            try:
                rcth_fast = time_since_start_hours / max(
                    0.0001,
                    math.log((avg_text - temp_at_start) / (avg_text - interior_temp)),
                )
                return rcth_fast
            except (ValueError, ZeroDivisionError):
                return None
        return None

    def calculate_rcth_at_recovery(
        self,
        temp_recovery_calc: float,
        temp_recovery_start: float,
        text_recovery_calc: float,
        text_recovery_start: float,
        time_recovery_calc: datetime,
        time_recovery_start: datetime,
    ) -> float | None:
        """Calcule RCth au démarrage de la relance.

        Ce calcul utilise les températures et timestamps de référence
        pour déterminer le coefficient de refroidissement réel.

        Args:
            temp_recovery_calc: Température intérieure au début du refroidissement
            temp_recovery_start: Température intérieure au démarrage de la relance
            text_recovery_calc: Température extérieure au début du refroidissement
            text_recovery_start: Température extérieure au démarrage de la relance
            time_recovery_calc: Timestamp du début du refroidissement
            time_recovery_start: Timestamp du démarrage de la relance

        Returns:
            RCth calculé, ou None si calcul impossible
        """
        dt_hours = (
            time_recovery_start.timestamp() - time_recovery_calc.timestamp()
        ) / 3600
        avg_text = (text_recovery_start + text_recovery_calc) / 2

        try:
            rcth = min(
                self.config.coef_max,
                dt_hours
                / math.log(
                    (avg_text - temp_recovery_calc) / (avg_text - temp_recovery_start)
                ),
            )
            return rcth
        except (ValueError, ZeroDivisionError):
            return None

    def calculate_rpth_at_recovery(
        self,
        temp_recovery_start: float,
        temp_recovery_end: float,
        text_recovery_start: float,
        text_recovery_end: float,
        time_recovery_start: datetime,
        time_recovery_end: datetime,
        rcth_interpolated: float,
    ) -> float | None:
        """Calcule RPth à la fin de la relance.

        Ce calcul utilise les températures et timestamps de référence
        ainsi que le RCth interpolé pour déterminer le coefficient
        de puissance de chauffage réel.

        Args:
            temp_recovery_start: Température intérieure au démarrage de la relance
            temp_recovery_end: Température intérieure à la fin de la relance
            text_recovery_start: Température extérieure au démarrage de la relance
            text_recovery_end: Température extérieure à la fin de la relance
            time_recovery_start: Timestamp du démarrage de la relance
            time_recovery_end: Timestamp de la fin de la relance
            rcth_interpolated: RCth interpolé selon le vent

        Returns:
            RPth calculé, ou None si calcul impossible
        """
        dt_hours = (
            time_recovery_end.timestamp() - time_recovery_start.timestamp()
        ) / 3600
        avg_text = (text_recovery_start + text_recovery_end) / 2

        try:
            exp_term = math.exp(dt_hours / rcth_interpolated)
            numerator = (avg_text - temp_recovery_end) * exp_term - (
                avg_text - temp_recovery_start
            )
            rpth = min(
                self.config.coef_max,
                max(self.config.coef_min, numerator / (1 - exp_term)),
            )
            return rpth
        except (ValueError, ZeroDivisionError):
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # ADR-006: Apprentissage des coefficients avec relaxation
    # ─────────────────────────────────────────────────────────────────────────

    def update_coefficients(
        self,
        coef_type: str,
        current_lw: float,
        current_hw: float,
        current_main: float,
        calculated_value: float,
        wind_kmh: float,
        relaxation_factor: float,
    ) -> CoefficientUpdateResult:
        """Met à jour les coefficients avec relaxation (ADR-006).

        Ce calcul:
        1. Calcule l'erreur entre valeur mesurée et interpolée
        2. Applique une formule de mise à jour pour rcth_lw/hw ou rpth_lw/hw
        3. Utilise la relaxation pour éviter les oscillations

        Args:
            coef_type: Type de coefficient ("rcth" ou "rpth")
            current_lw: Valeur actuelle low wind
            current_hw: Valeur actuelle high wind
            current_main: Valeur principale actuelle
            calculated_value: Valeur mesurée/calculée
            wind_kmh: Vitesse du vent en km/h
            relaxation_factor: Facteur de relaxation

        Returns:
            CoefficientUpdateResult avec les nouvelles valeurs
        """
        wind_low = self.config.wind_low_kmh
        wind_high = self.config.wind_high_kmh
        coef_min = self.config.coef_min
        coef_max = self.config.coef_max

        # Position relative du vent entre les bornes (centré sur 0)
        x = (wind_kmh - wind_low) / (wind_high - wind_low) - 0.5
        relax = relaxation_factor

        # Valeur interpolée actuelle
        interpol = max(coef_min, current_lw + (current_hw - current_lw) * (x + 0.5))

        # Erreur observée
        err = calculated_value - interpol

        # Formules de mise à jour (polynômes cubiques pour répartition lw/hw)
        lw_new = max(
            coef_min,
            current_lw + err * (1 - 5 / 3 * x - 2 * x * x + 8 / 3 * x * x * x),
        )
        hw_new = max(
            coef_min,
            current_hw + err * (1 + 5 / 3 * x - 2 * x * x - 8 / 3 * x * x * x),
        )

        # Application de la relaxation
        new_lw = min(coef_max, (current_lw + relax * lw_new) / (1 + relax))
        new_hw = (current_hw + relax * hw_new) / (1 + relax)

        # Contrainte: hw <= lw (plus de pertes thermiques par vent fort)
        new_hw = min(new_lw, new_hw)

        # Mise à jour du coefficient principal avec relaxation
        new_main = (current_main + relax * calculated_value) / (1 + relax)

        # Appliquer les limites selon le type
        if coef_type == "rpth":
            new_main = min(coef_max, max(coef_min, new_main))

        return CoefficientUpdateResult(
            coef_lw=new_lw,
            coef_hw=new_hw,
            coef_main=max(coef_min, new_main),
            error=round(err, 3),
        )
