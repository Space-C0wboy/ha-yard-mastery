"""Base entity: one Yard Mastery device per account."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import YmCoordinator


class YmEntity(CoordinatorEntity[YmCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: YmCoordinator, key: str) -> None:
        super().__init__(coordinator)
        unique = coordinator.config_entry.unique_id
        self._attr_unique_id = f"{unique}_{key}"
        program_info = coordinator.data.user_status.get("program")
        program = program_info.get("program") if isinstance(program_info, dict) else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique)},
            name="Yard Mastery",
            manufacturer="Yard Mastery (unofficial)",
            model=program,
        )
