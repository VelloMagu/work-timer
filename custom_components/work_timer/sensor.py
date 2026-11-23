"""Status and hours sensors for each Work Timer user."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, WorkTimerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WorkTimerCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for name in coordinator.data:
        entities.append(WorkTimerStatusSensor(coordinator, name))
        entities.append(WorkTimerHoursSensor(coordinator, name))
    async_add_entities(entities)


class _WorkTimerSensor(CoordinatorEntity[WorkTimerCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WorkTimerCoordinator, user: str, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._user = user
        self._key = key
        self._attr_unique_id = f"{user}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, user)},
            name=f"Work Timer {user}",
            manufacturer="Work Timer",
        )

    @property
    def available(self) -> bool:
        return self._user in self.coordinator.data


class WorkTimerStatusSensor(_WorkTimerSensor):
    def __init__(self, coordinator: WorkTimerCoordinator, user: str) -> None:
        super().__init__(coordinator, user, "status", "Status")

    @property
    def native_value(self) -> str | None:
        row = self.coordinator.data.get(self._user)
        if not row:
            return None
        return str(row.get("status") or "stopped")


class WorkTimerHoursSensor(_WorkTimerSensor):
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: WorkTimerCoordinator, user: str) -> None:
        super().__init__(coordinator, user, "hours", "Hours worked")

    @property
    def native_value(self) -> float | None:
        row = self.coordinator.data.get(self._user)
        if not row:
            return None
        return float(row.get("hours") or 0)
