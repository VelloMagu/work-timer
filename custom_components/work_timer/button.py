"""Start, pause, clear, and reset buttons for each Work Timer user."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, WorkTimerCoordinator

COMMANDS = (
    ("start", "Start"),
    ("pause", "Pause"),
    ("clear", "Clear"),
    ("reset", "Reset"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WorkTimerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WorkTimerButton(coordinator, name, command, label)
        for name in coordinator.data
        for command, label in COMMANDS
    )


class WorkTimerButton(CoordinatorEntity[WorkTimerCoordinator], ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: WorkTimerCoordinator, user: str, command: str, label: str) -> None:
        super().__init__(coordinator)
        self._user = user
        self._command = command
        self._attr_unique_id = f"{user}_{command}"
        self._attr_name = label
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, user)},
            name=f"Work Timer {user}",
            manufacturer="Work Timer",
        )

    async def async_press(self) -> None:
        await self.coordinator.async_command(self._user, self._command)
