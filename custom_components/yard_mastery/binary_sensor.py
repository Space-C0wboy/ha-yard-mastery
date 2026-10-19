"""Yard Mastery binary sensors."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_APP_VERSION, DEFAULT_APP_VERSION
from .coordinator import YmConfigEntry, YmCoordinator
from .entity import YmEntity

PARALLEL_UPDATES = 0


class YmTreatmentDue(YmEntity, BinarySensorEntity):
    """On while the current window still has items that are not done."""

    _attr_translation_key = "treatment_due"

    def __init__(self, coordinator: YmCoordinator) -> None:
        super().__init__(coordinator, "treatment_due")

    def _due(self) -> list[str]:
        window = self.coordinator.data.current_window
        return [i.product_name for i in window.items if i.status == "CREATED"] if window else []

    @property
    def is_on(self) -> bool | None:
        return bool(self._due()) if self.coordinator.data.current_window else None

    @property
    def extra_state_attributes(self) -> dict | None:
        window = self.coordinator.data.current_window
        if window is None:
            return None
        return {"due_items": self._due(), "window_end": window.end.isoformat()}


class YmAppVersionValid(YmEntity, BinarySensorEntity):
    _attr_translation_key = "app_version_valid"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YmCoordinator) -> None:
        super().__init__(coordinator, "app_version_valid")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.version_valid

    @property
    def extra_state_attributes(self) -> dict:
        return {"version": self.coordinator.config_entry.options.get(CONF_APP_VERSION, DEFAULT_APP_VERSION)}


async def async_setup_entry(
    hass: HomeAssistant, entry: YmConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([YmTreatmentDue(entry.runtime_data), YmAppVersionValid(entry.runtime_data)])
