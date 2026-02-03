"""Gestion centralisée des services SmartHRT.

Ce module gère l'enregistrement et le désenregistrement des services
au niveau du domaine, évitant les race conditions lorsque plusieurs
instances de l'intégration sont configurées.
"""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .const import (
    DOMAIN,
    DATA_COORDINATOR,
    SERVICE_CALCULATE_RECOVERY_TIME,
    SERVICE_CALCULATE_RECOVERY_UPDATE_TIME,
    SERVICE_CALCULATE_RCTH_FAST,
    SERVICE_ON_HEATING_STOP,
    SERVICE_ON_RECOVERY_START,
    SERVICE_ON_RECOVERY_END,
    SERVICE_RESET_LEARNING,
    SERVICE_TRIGGER_CALCULATION,
)

# Nouveaux services simplifiés
SERVICE_START_HEATING_CYCLE = "start_heating_cycle"
SERVICE_STOP_HEATING = "stop_heating"
SERVICE_START_RECOVERY = "start_recovery"
SERVICE_END_RECOVERY = "end_recovery"
SERVICE_GET_STATE = "get_state"

_LOGGER = logging.getLogger(__name__)

# Clé pour stocker le flag d'enregistrement des services
DATA_SERVICES_REGISTERED = "services_registered"

# Liste des services disponibles
SERVICES = [
    # Services simplifiés (recommandés)
    SERVICE_START_HEATING_CYCLE,
    SERVICE_STOP_HEATING,
    SERVICE_START_RECOVERY,
    SERVICE_END_RECOVERY,
    SERVICE_GET_STATE,
    # Services utilitaires
    SERVICE_RESET_LEARNING,
    SERVICE_TRIGGER_CALCULATION,
    # Services historiques (conservés pour compatibilité)
    SERVICE_CALCULATE_RECOVERY_TIME,
    SERVICE_CALCULATE_RECOVERY_UPDATE_TIME,
    SERVICE_CALCULATE_RCTH_FAST,
    SERVICE_ON_HEATING_STOP,
    SERVICE_ON_RECOVERY_START,
    SERVICE_ON_RECOVERY_END,
]


def _get_coordinator(hass: HomeAssistant, entry_id: str | None):
    """Récupère le coordinator depuis un appel de service.

    Args:
        hass: Instance Home Assistant
        entry_id: ID optionnel de l'entrée de configuration

    Returns:
        Le coordinateur SmartHRT ou None si non trouvé
    """
    if DOMAIN not in hass.data:
        _LOGGER.error("Aucune instance SmartHRT configurée")
        return None

    # Collecter tous les coordinateurs disponibles
    available_coordinators = {
        key: data[DATA_COORDINATOR]
        for key, data in hass.data[DOMAIN].items()
        if isinstance(data, dict) and DATA_COORDINATOR in data
    }

    if not available_coordinators:
        _LOGGER.error("Aucun coordinateur SmartHRT trouvé")
        return None

    # Si entry_id est fourni, l'utiliser
    if entry_id:
        if entry_id in available_coordinators:
            _LOGGER.debug("Utilisation du coordinateur pour entry_id: %s", entry_id)
            return available_coordinators[entry_id]
        else:
            _LOGGER.error(
                "Entry ID '%s' non trouvé. Instances disponibles: %s",
                entry_id,
                list(available_coordinators.keys()),
            )
            return None

    # Si pas d'entry_id et plusieurs instances, avertir l'utilisateur
    if len(available_coordinators) > 1:
        _LOGGER.warning(
            "Plusieurs instances SmartHRT détectées (%d) mais aucun entry_id fourni. "
            "Utilisation de la première instance. Instances disponibles: %s. "
            "Veuillez spécifier 'entry_id' pour cibler une instance spécifique.",
            len(available_coordinators),
            list(available_coordinators.keys()),
        )

    # Retourner le premier coordinateur (comportement par défaut)
    first_entry_id = next(iter(available_coordinators))
    _LOGGER.debug("Utilisation de l'instance par défaut: %s", first_entry_id)
    return available_coordinators[first_entry_id]


async def async_setup_services(hass: HomeAssistant) -> None:
    """Enregistre les services SmartHRT.

    Cette fonction est appelée une seule fois lors du setup de la
    première instance de l'intégration. Les services sont partagés
    entre toutes les instances.
    """
    # Vérifier si les services sont déjà enregistrés
    if hass.data.get(DOMAIN, {}).get(DATA_SERVICES_REGISTERED):
        _LOGGER.debug("Services SmartHRT déjà enregistrés")
        return

    schema = vol.Schema({vol.Optional("entry_id"): str})

    async def handle_calculate_recovery_time(call: ServiceCall) -> dict[str, Any]:
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}
        coord.calculate_recovery_time()
        return {
            "recovery_start_hour": (
                coord.data.recovery_start_hour.isoformat()
                if coord.data.recovery_start_hour
                else None
            ),
            "success": True,
        }

    async def handle_calculate_recovery_update_time(
        call: ServiceCall,
    ) -> dict[str, Any]:
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}
        result = coord.calculate_recovery_update_time()
        if result:
            coord.data.recovery_update_hour = result
            coord._schedule_recovery_update(result)
            coord._notify_listeners()
        return {
            "recovery_update_hour": result.isoformat() if result else None,
            "success": True,
        }

    async def handle_calculate_rcth_fast(call: ServiceCall) -> dict[str, Any]:
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}
        coord.calculate_rcth_fast()
        return {"rcth_fast": coord.data.rcth_fast, "success": True}

    async def handle_on_heating_stop(call: ServiceCall) -> dict[str, Any]:
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}
        coord.on_heating_stop()
        return {
            "time_recovery_calc": (
                coord.data.time_recovery_calc.isoformat()
                if coord.data.time_recovery_calc
                else None
            ),
            "success": True,
        }

    async def handle_on_recovery_start(call: ServiceCall) -> dict[str, Any]:
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}
        coord.on_recovery_start()
        return {
            "time_recovery_start": (
                coord.data.time_recovery_start.isoformat()
                if coord.data.time_recovery_start
                else None
            ),
            "rcth_calculated": coord.data.rcth_calculated,
            "success": True,
        }

    async def handle_on_recovery_end(call: ServiceCall) -> dict[str, Any]:
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}
        coord.on_recovery_end()
        return {
            "time_recovery_end": (
                coord.data.time_recovery_end.isoformat()
                if coord.data.time_recovery_end
                else None
            ),
            "rpth_calculated": coord.data.rpth_calculated,
            "success": True,
        }

    async def handle_reset_learning(call: ServiceCall) -> dict[str, Any]:
        """Reset all learned thermal coefficients to defaults."""
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}

        await coord.reset_learning()
        return {
            "rcth": coord.data.rcth,
            "rpth": coord.data.rpth,
            "rcth_lw": coord.data.rcth_lw,
            "rcth_hw": coord.data.rcth_hw,
            "rpth_lw": coord.data.rpth_lw,
            "rpth_hw": coord.data.rpth_hw,
            "success": True,
            "message": "Learning reset to defaults",
        }

    async def handle_trigger_calculation(call: ServiceCall) -> dict[str, Any]:
        """Manually trigger a recovery time calculation."""
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}

        await hass.async_add_executor_job(coord.calculate_recovery_time)
        coord._notify_listeners()

        return {
            "recovery_start_hour": (
                coord.data.recovery_start_hour.isoformat()
                if coord.data.recovery_start_hour
                else None
            ),
            "time_to_recovery_hours": coord.get_time_to_recovery_hours(),
            "success": True,
        }

    # Nouveaux handlers simplifiés
    async def handle_start_heating_cycle(call: ServiceCall) -> dict[str, Any]:
        """Démarre un nouveau cycle de chauffage (HEATING_ON)."""
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}

        # ADR-028: Forcer l'état via force_state() pour traçabilité
        from .coordinator import SmartHRTState

        coord.force_state(SmartHRTState.HEATING_ON)
        coord.data.rp_calc_mode = False
        coord.data.recovery_calc_mode = False
        coord.data.temp_lag_detection_active = False
        coord._notify_listeners()
        await coord._save_learned_data()

        _LOGGER.info("%s Cycle de chauffage démarré manuellement", coord._log_prefix())

        return {
            "success": True,
            "state": str(coord.data.current_state),
            "message": "Cycle de chauffage démarré",
        }

    async def handle_stop_heating(call: ServiceCall) -> dict[str, Any]:
        """Arrête le chauffage et démarre la surveillance (DETECTING_LAG → MONITORING)."""
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}

        # Appel à la méthode _on_recoverycalc_hour qui gère la transition proprement
        await coord._on_recoverycalc_hour(None)

        return {
            "success": True,
            "state": str(coord.data.current_state),
            "recovery_start_hour": (
                coord.data.recovery_start_hour.isoformat()
                if coord.data.recovery_start_hour
                else None
            ),
            "message": "Chauffage arrêté, surveillance démarrée",
        }

    async def handle_start_recovery(call: ServiceCall) -> dict[str, Any]:
        """Démarre la relance de chauffage (RECOVERY → HEATING_PROCESS)."""
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}

        coord.on_recovery_start()

        return {
            "success": True,
            "state": str(coord.data.current_state),
            "time_recovery_start": (
                coord.data.time_recovery_start.isoformat()
                if coord.data.time_recovery_start
                else None
            ),
            "rcth_calculated": coord.data.rcth_calculated,
            "message": "Relance démarrée",
        }

    async def handle_end_recovery(call: ServiceCall) -> dict[str, Any]:
        """Termine la relance de chauffage (HEATING_PROCESS → HEATING_ON)."""
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}

        coord.on_recovery_end()

        return {
            "success": True,
            "state": str(coord.data.current_state),
            "time_recovery_end": (
                coord.data.time_recovery_end.isoformat()
                if coord.data.time_recovery_end
                else None
            ),
            "rpth_calculated": coord.data.rpth_calculated,
            "message": "Relance terminée",
        }

    async def handle_get_state(call: ServiceCall) -> dict[str, Any]:
        """Retourne l'état actuel de la machine à états avec détails."""
        entry_id = call.data.get("entry_id")
        coord = _get_coordinator(hass, entry_id)
        if not coord:
            error_msg = (
                f"Coordinateur non trouvé pour entry_id={entry_id}"
                if entry_id
                else "Aucun coordinateur SmartHRT disponible"
            )
            _LOGGER.error(error_msg)
            return {"success": False, "error": error_msg}

        return {
            "success": True,
            "state": str(coord.data.current_state),
            "smartheating_mode": coord.data.smartheating_mode,
            "recovery_calc_mode": coord.data.recovery_calc_mode,
            "rp_calc_mode": coord.data.rp_calc_mode,
            "temp_lag_detection_active": coord.data.temp_lag_detection_active,
            "interior_temp": coord.data.interior_temp,
            "exterior_temp": coord.data.exterior_temp,
            "target_hour": coord.data.target_hour.isoformat(),
            "recoverycalc_hour": coord.data.recoverycalc_hour.isoformat(),
            "recovery_start_hour": (
                coord.data.recovery_start_hour.isoformat()
                if coord.data.recovery_start_hour
                else None
            ),
            "time_to_recovery_hours": coord.get_time_to_recovery_hours(),
            "rcth": coord.data.rcth,
            "rpth": coord.data.rpth,
        }

    # Mapping des services vers leurs handlers
    handlers = {
        # Services simplifiés (recommandés)
        SERVICE_START_HEATING_CYCLE: handle_start_heating_cycle,
        SERVICE_STOP_HEATING: handle_stop_heating,
        SERVICE_START_RECOVERY: handle_start_recovery,
        SERVICE_END_RECOVERY: handle_end_recovery,
        SERVICE_GET_STATE: handle_get_state,
        # Services utilitaires
        SERVICE_RESET_LEARNING: handle_reset_learning,
        SERVICE_TRIGGER_CALCULATION: handle_trigger_calculation,
        # Services historiques (conservés pour compatibilité)
        SERVICE_CALCULATE_RECOVERY_TIME: handle_calculate_recovery_time,
        SERVICE_CALCULATE_RECOVERY_UPDATE_TIME: handle_calculate_recovery_update_time,
        SERVICE_CALCULATE_RCTH_FAST: handle_calculate_rcth_fast,
        SERVICE_ON_HEATING_STOP: handle_on_heating_stop,
        SERVICE_ON_RECOVERY_START: handle_on_recovery_start,
        SERVICE_ON_RECOVERY_END: handle_on_recovery_end,
    }

    # Enregistrer les services
    for service_name, handler in handlers.items():
        hass.services.async_register(
            DOMAIN,
            service_name,
            handler,
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL,
        )
        _LOGGER.debug("Service enregistré: %s.%s", DOMAIN, service_name)

    # Marquer les services comme enregistrés
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_SERVICES_REGISTERED] = True
    _LOGGER.info("Services SmartHRT enregistrés avec succès")


async def async_unload_services(hass: HomeAssistant) -> None:
    """Désenregistre les services SmartHRT.

    Cette fonction est appelée uniquement lorsque la dernière instance
    de l'intégration est déchargée.
    """
    if DOMAIN not in hass.data:
        return

    # Compter les coordinateurs restants (exclure les clés spéciales)
    remaining_coordinators = sum(
        1
        for key, data in hass.data[DOMAIN].items()
        if isinstance(data, dict) and DATA_COORDINATOR in data
    )

    # Ne désenregistrer que si c'est la dernière instance
    if remaining_coordinators > 0:
        _LOGGER.debug(
            "Services SmartHRT conservés (%d instance(s) restante(s))",
            remaining_coordinators,
        )
        return

    # Désenregistrer les services
    for service_name in SERVICES:
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)

    # Supprimer le flag
    if DATA_SERVICES_REGISTERED in hass.data.get(DOMAIN, {}):
        del hass.data[DOMAIN][DATA_SERVICES_REGISTERED]

    _LOGGER.info("Services SmartHRT désenregistrés")
