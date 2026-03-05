"""Implements the SmartHRT number entities.

ADR implémentées dans ce module:
- ADR-006: Apprentissage continu (SmartHRTRelaxationNumber pour le facteur)
- ADR-007: Compensation météo (RCth/RPth LW/HW pour interpolation vent)
- ADR-012: Exposition entités pour Lovelace (numbers comme entités HA)
- ADR-027: Utilisation de CoordinatorEntity pour synchronisation automatique
"""

import logging

from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.device_registry import DeviceInfo, DeviceEntryType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DEVICE_MANUFACTURER,
    CONF_NAME,
    DATA_COORDINATOR,
    DEFAULT_TSP_MIN,
    DEFAULT_TSP_MAX,
    DEFAULT_TSP_STEP,
    DEFAULT_RCTH_MIN,
    DEFAULT_RCTH_MAX,
    DEFAULT_RPTH_MIN,
    DEFAULT_RPTH_MAX,
    DEFAULT_TSP_COOL_MIN,
    DEFAULT_TSP_COOL_MAX,
    DEFAULT_RCCU_MIN,
    DEFAULT_RCCU_MAX,
    DEFAULT_RPCU_MIN,
    DEFAULT_RPCU_MAX,
)
from .coordinator import SmartHRTCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Configuration des entités number à partir de la configuration ConfigEntry"""

    _LOGGER.debug("Calling number async_setup_entry entry=%s", entry)

    coordinator: SmartHRTCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]

    entities = [
        SmartHRTSetPointNumber(coordinator, entry),
        SmartHRTRCthNumber(coordinator, entry),
        SmartHRTRPthNumber(coordinator, entry),
        SmartHRTRCthLWNumber(coordinator, entry),
        SmartHRTRCthHWNumber(coordinator, entry),
        SmartHRTRPthLWNumber(coordinator, entry),
        SmartHRTRPthHWNumber(coordinator, entry),
        SmartHRTRelaxationNumber(coordinator, entry),
        # Cool recovery
        SmartHRTCoolSetPointNumber(coordinator, entry),
        SmartHRTRCcuNumber(coordinator, entry),
        SmartHRTRPcuNumber(coordinator, entry),
        SmartHRTRCcuLWNumber(coordinator, entry),
        SmartHRTRCcuHWNumber(coordinator, entry),
        SmartHRTRPcuLWNumber(coordinator, entry),
        SmartHRTRPcuHWNumber(coordinator, entry),
        SmartHRTCoolRelaxationNumber(coordinator, entry),
    ]
    async_add_entities(entities, True)


class SmartHRTBaseNumber(CoordinatorEntity[SmartHRTCoordinator], NumberEntity):
    """Classe de base pour les number SmartHRT (ADR-027: CoordinatorEntity)."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        """Initialisation de base"""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._device_id = config_entry.entry_id
        self._device_name = config_entry.data.get(CONF_NAME, "SmartHRT")
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Retourne les informations du device"""
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, self._device_id)},
            name=self._device_name,
            manufacturer=DEVICE_MANUFACTURER,
            model="Smart Heating Regulator",
        )


class SmartHRTSetPointNumber(SmartHRTBaseNumber):
    """Entité number pour la consigne de température (Set Point)"""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "Consigne"
        self._attr_unique_id = f"{self._device_id}_setpoint"
        self._attr_native_min_value = DEFAULT_TSP_MIN
        self._attr_native_max_value = DEFAULT_TSP_MAX
        self._attr_native_step = DEFAULT_TSP_STEP
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return self.coordinator.data.tsp

    @property
    def icon(self) -> str | None:
        return "mdi:thermometer"

    async def async_set_native_value(self, value: float) -> None:
        """Mise à jour de la valeur de consigne"""
        _LOGGER.info("Set point changed to: %s", value)
        self.coordinator.set_tsp(value)


class SmartHRTRCthNumber(SmartHRTBaseNumber):
    """Entité number pour RCth"""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RCth"
        self._attr_unique_id = f"{self._device_id}_rcth"
        self._attr_native_min_value = DEFAULT_RCTH_MIN
        self._attr_native_max_value = DEFAULT_RCTH_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTime.HOURS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rcth, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-battery-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RCth changed to: %s", value)
        self.coordinator.set_rcth(value)


class SmartHRTRPthNumber(SmartHRTBaseNumber):
    """Entité number pour RPth"""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RPth"
        self._attr_unique_id = f"{self._device_id}_rpth"
        self._attr_native_min_value = DEFAULT_RPTH_MIN
        self._attr_native_max_value = DEFAULT_RPTH_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rpth, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-lightning-bolt-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RPth changed to: %s", value)
        self.coordinator.set_rpth(value)


class SmartHRTRCthLWNumber(SmartHRTBaseNumber):
    """Entité number pour RCth low wind.

    ADR-007: Compensation météo - coefficient de refroidissement par vent faible.
    Utilisé pour l'interpolation linéaire selon la vitesse du vent.
    """

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RCth (vent faible)"
        self._attr_unique_id = f"{self._device_id}_rcth_lw"
        self._attr_native_min_value = DEFAULT_RCTH_MIN
        self._attr_native_max_value = DEFAULT_RCTH_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTime.HOURS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rcth_lw, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-battery-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RCth LW changed to: %s", value)
        self.coordinator.set_rcth_lw(value)


class SmartHRTRCthHWNumber(SmartHRTBaseNumber):
    """Entité number pour RCth high wind"""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RCth (vent fort)"
        self._attr_unique_id = f"{self._device_id}_rcth_hw"
        self._attr_native_min_value = DEFAULT_RCTH_MIN
        self._attr_native_max_value = DEFAULT_RCTH_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTime.HOURS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rcth_hw, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-battery-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RCth HW changed to: %s", value)
        self.coordinator.set_rcth_hw(value)


class SmartHRTRPthLWNumber(SmartHRTBaseNumber):
    """Entité number pour RPth low wind"""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RPth (vent faible)"
        self._attr_unique_id = f"{self._device_id}_rpth_lw"
        self._attr_native_min_value = DEFAULT_RPTH_MIN
        self._attr_native_max_value = DEFAULT_RPTH_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rpth_lw, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-lightning-bolt-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RPth LW changed to: %s", value)
        self.coordinator.set_rpth_lw(value)


class SmartHRTRPthHWNumber(SmartHRTBaseNumber):
    """Entité number pour RPth high wind"""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RPth (vent fort)"
        self._attr_unique_id = f"{self._device_id}_rpth_hw"
        self._attr_native_min_value = DEFAULT_RPTH_MIN
        self._attr_native_max_value = DEFAULT_RPTH_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rpth_hw, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-lightning-bolt-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RPth HW changed to: %s", value)
        self.coordinator.set_rpth_hw(value)


class SmartHRTRelaxationNumber(SmartHRTBaseNumber):
    """Entité number pour le facteur de relaxation.

    ADR-006: Apprentissage continu - contrôle la vitesse de convergence.
    Plus la valeur est élevée, plus l'apprentissage est lent mais stable.
    """

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "Facteur de relaxation"
        self._attr_unique_id = f"{self._device_id}_relaxation"
        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 15.0
        self._attr_native_step = 0.05
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return self.coordinator.data.relaxation_factor

    @property
    def icon(self) -> str | None:
        return "mdi:brain"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("Relaxation factor changed to: %s", value)
        self.coordinator.set_relaxation_factor(value)


# ─────────────────────────────────────────────────────────────────────────────
# Cool Recovery Number Entities
# ─────────────────────────────────────────────────────────────────────────────


class SmartHRTCoolSetPointNumber(SmartHRTBaseNumber):
    """Entité number pour la consigne de température de fraîcheur."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "Consigne fraîcheur"
        self._attr_unique_id = f"{self._device_id}_tsp_cool"
        self._attr_native_min_value = DEFAULT_TSP_COOL_MIN
        self._attr_native_max_value = DEFAULT_TSP_COOL_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return self.coordinator.data.tsp_cool

    @property
    def icon(self) -> str | None:
        return "mdi:snowflake-thermometer"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("Cool set point changed to: %s", value)
        self.coordinator.set_tsp_cool(value)


class SmartHRTRCcuNumber(SmartHRTBaseNumber):
    """Entité number pour RCcu (constante de temps de réchauffement passif)."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RCcu"
        self._attr_unique_id = f"{self._device_id}_rccu"
        self._attr_native_min_value = DEFAULT_RCCU_MIN
        self._attr_native_max_value = DEFAULT_RCCU_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTime.HOURS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rccu, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-battery-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RCcu changed to: %s", value)
        self.coordinator.set_rccu(value)


class SmartHRTRPcuNumber(SmartHRTBaseNumber):
    """Entité number pour RPcu (puissance de refroidissement clim)."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RPcu"
        self._attr_unique_id = f"{self._device_id}_rpcu"
        self._attr_native_min_value = DEFAULT_RPCU_MIN
        self._attr_native_max_value = DEFAULT_RPCU_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rpcu, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:air-conditioner"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RPcu changed to: %s", value)
        self.coordinator.set_rpcu(value)


class SmartHRTRCcuLWNumber(SmartHRTBaseNumber):
    """Entité number pour RCcu low wind."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RCcu (vent faible)"
        self._attr_unique_id = f"{self._device_id}_rccu_lw"
        self._attr_native_min_value = DEFAULT_RCCU_MIN
        self._attr_native_max_value = DEFAULT_RCCU_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTime.HOURS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rccu_lw, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-battery-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RCcu LW changed to: %s", value)
        self.coordinator.set_rccu_lw(value)


class SmartHRTRCcuHWNumber(SmartHRTBaseNumber):
    """Entité number pour RCcu high wind."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RCcu (vent fort)"
        self._attr_unique_id = f"{self._device_id}_rccu_hw"
        self._attr_native_min_value = DEFAULT_RCCU_MIN
        self._attr_native_max_value = DEFAULT_RCCU_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTime.HOURS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rccu_hw, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:home-battery-outline"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RCcu HW changed to: %s", value)
        self.coordinator.set_rccu_hw(value)


class SmartHRTRPcuLWNumber(SmartHRTBaseNumber):
    """Entité number pour RPcu low wind."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RPcu (vent faible)"
        self._attr_unique_id = f"{self._device_id}_rpcu_lw"
        self._attr_native_min_value = DEFAULT_RPCU_MIN
        self._attr_native_max_value = DEFAULT_RPCU_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rpcu_lw, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:air-conditioner"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RPcu LW changed to: %s", value)
        self.coordinator.set_rpcu_lw(value)


class SmartHRTRPcuHWNumber(SmartHRTBaseNumber):
    """Entité number pour RPcu high wind."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "RPcu (vent fort)"
        self._attr_unique_id = f"{self._device_id}_rpcu_hw"
        self._attr_native_min_value = DEFAULT_RPCU_MIN
        self._attr_native_max_value = DEFAULT_RPCU_MAX
        self._attr_native_step = 0.5
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return round(self.coordinator.data.rpcu_hw, 2)

    @property
    def icon(self) -> str | None:
        return "mdi:air-conditioner"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("RPcu HW changed to: %s", value)
        self.coordinator.set_rpcu_hw(value)


class SmartHRTCoolRelaxationNumber(SmartHRTBaseNumber):
    """Entité number pour le facteur de relaxation des coefficients cool."""

    def __init__(
        self, coordinator: SmartHRTCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_name = "Facteur de relaxation (fraîcheur)"
        self._attr_unique_id = f"{self._device_id}_relaxation_cool"
        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 15.0
        self._attr_native_step = 0.05
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float:
        return self.coordinator.data.relaxation_factor_cool

    @property
    def icon(self) -> str | None:
        return "mdi:brain"

    async def async_set_native_value(self, value: float) -> None:
        _LOGGER.info("Cool relaxation factor changed to: %s", value)
        self.coordinator.set_relaxation_factor_cool(value)
