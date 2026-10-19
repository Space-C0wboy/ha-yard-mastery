"""Yard Mastery sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import YmConfigEntry, YmCoordinator, YmData
from .entity import YmEntity
from .models import SoilTest, Window, lawn_square_feet, season_totals, soil_history, whole_lawn_amount

PARALLEL_UPDATES = 0

_NOTES_LIMIT = 4000  # keeps one attribute far below the recorder's size limit
_MAIN_NUTRIENTS = {"ph", "n", "p", "k", "om"}
_NUTRIENT_LABELS = {"ph": "pH", "om": "OM"}


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _today() -> date:
    return dt_util.now().date()


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _window_items(window: Window, square_feet: Any) -> list[dict]:
    sq_ft = square_feet if isinstance(square_feet, (int, float)) and not isinstance(square_feet, bool) else None
    return [
        {
            "id": item.id,
            "product": item.product_name,
            "type": item.product_type,
            "material": item.material_type,
            "rate": item.rate,
            "unit": item.measure,
            "whole_lawn": whole_lawn_amount(item, sq_ft) if sq_ft else None,
            "n": item.nitrogen,
            "p": item.phosphorus,
            "k": item.potassium,
            "status": item.status,
            "journal_date": _iso(item.journal_date),
        }
        for item in window.items
    ]


def _window_attrs(window: Window | None, data: YmData, *, details: bool) -> dict:
    if window is None:
        return {}
    attrs: dict[str, Any] = {
        "start": _iso(window.start),
        "end": _iso(window.end),
        "description": window.description,
    }
    if details:
        attrs.update(
            youtube_id=window.youtube_id,
            image_url=window.image_url,
            blackout_treatment=window.blackout_treatment,
        )
    attrs["items"] = _window_items(window, lawn_square_feet(data.yard))
    return attrs


def selected_soil_test(data: YmData) -> SoilTest | None:
    """The yard's selected test, else the most recently updated completed one."""
    tests = data.snapshot.soil_tests
    selected = _d(data.yard.get("selectedTest")).get("barcode")
    if selected in tests:
        return tests[selected]
    completed = [t for t in tests.values() if t.status.lower() == "completed"]
    return max(completed, key=lambda t: t.updated or t.created or date.min, default=None)


def _latest_entry(data: YmData, *, applied: bool = False):
    entries = [e for e in data.snapshot.journal.values() if e.applications or not applied]
    return max(entries, key=lambda e: e.day, default=None)


def _season(data: YmData) -> dict:
    items = {}
    for window in data.snapshot.windows.values():
        for item in window.items:
            if item.product_id not in items or (item.measure and not items[item.product_id].measure):
                items[item.product_id] = item
    return season_totals(data.snapshot.journal.values(), items, _today().year, lawn_square_feet(data.yard))


def _steps(data: YmData) -> tuple[int | None, int, int]:
    home = data.home_bundle
    steps = home.get("steps")
    return (
        steps if isinstance(steps, int) and not isinstance(steps, bool) else None,
        home.get("stepsCompleted") or 0,
        home.get("stepsSkipped") or 0,
    )


def _progress(data: YmData) -> int | None:
    steps, completed, skipped = _steps(data)
    # A skipped step is a decision made, so it counts as done.
    return round(100 * (completed + skipped) / steps) if steps else None


def _progress_attrs(data: YmData, latest: dict) -> dict:
    steps, completed, skipped = _steps(data)
    home = data.home_bundle
    return {
        "steps": steps,
        "completed": completed,
        "skipped": skipped,
        "remaining": None if steps is None else steps - completed - skipped,
        "window_id": home.get("bundleId"),
        "window_name": home.get("bundleName"),
        "window_date": home.get("bundleDate"),
        "next_window_id": home.get("nextBundleId"),
        "next_window_date": home.get("nextBundleDate"),
        "program_status": home.get("programStatus"),
    }


def _soil_temp(data: YmData) -> float | None:
    value = data.soil_temp.get("soilTemp")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _program_state(data: YmData) -> str | None:
    return _d(data.yard.get("program")).get("programType") or _d(data.user_status.get("program")).get("program")


def _program_attrs(data: YmData, latest: dict) -> dict:
    yard, user, home = data.yard, data.user_status, data.home_bundle
    status = _d(latest.get("lawn_program_status"))
    return {
        "program_name": home.get("programName"),
        "program_status": home.get("programStatus"),
        "lawn_type": yard.get("lawnType"),
        "product_type_preference": yard.get("productTypePreference"),
        "square_feet": yard.get("squareFt"),
        "square_feet_measured": yard.get("squareFtMeasured"),
        "will_overseed": yard.get("willOverseed"),
        "blackout_opt_in": yard.get("blackoutOptIn"),
        "blackout_eligible": yard.get("blackoutEligible"),
        "recalculating": yard.get("recalculatingLawnProgram"),
        "lawn_id": yard.get("lawnId"),
        "has_buy_all_purchase": yard.get("hasBuyAllPurchase"),
        "buy_all_purchase_date": yard.get("buyAllPurchaseDate"),
        "soil_test_product_id": yard.get("shopifySoilTestProductId"),
        "complete_date": status.get("completeDate"),
        "queue_date": status.get("queueDate"),
        "has_address": status.get("hasAddress"),
        "has_valid_zip": status.get("hasValidZip"),
        "is_processing": status.get("isProcessing"),
        "account_type": _d(user.get("user")).get("type"),
        "app_version": _d(user.get("app")).get("version"),
        "dug_ai_photos": user.get("dugAiPhotos"),
        "dug_ai_rollout_percent": user.get("dugAiRolloutPercent"),
    }


def _last_application_attrs(data: YmData, latest: dict) -> dict:
    entry = _latest_entry(data, applied=True)
    if entry is None:
        return {}
    return {
        "products": [{"product": a.product_name, "amount": a.rate} for a in entry.applications],
        "soil_temp": entry.soil_temp,
        "soil_temp_unit": entry.temp_unit,
        "areas": list(entry.segments),
        "notes": (entry.body or "")[:_NOTES_LIMIT] or None,
    }


def _last_journal_attrs(data: YmData, latest: dict) -> dict:
    entry = _latest_entry(data)
    if entry is None:
        return {}
    return {
        "name": entry.name,
        "body": (entry.body or "")[:_NOTES_LIMIT] or None,
        "activities": list(entry.activities),
        "photo_count": entry.photo_count,
    }


def _nitrogen_attrs(data: YmData, latest: dict) -> dict:
    totals = _season(data)
    return {
        "phosphorus": totals["p"],
        "potassium": totals["k"],
        "by_product": totals["by_product"],
        "not_counted": totals["not_counted"],
    }


def _soil_test_attrs(data: YmData, latest: dict) -> dict:
    test = selected_soil_test(data)
    if test is None:
        return {}
    selected = _d(data.yard.get("selectedTest"))
    return {
        "test_id": selected.get("id"),
        "description": selected.get("description"),
        "test_type": selected.get("type"),
        "can_inform_program": selected.get("canInformLawnProgram"),
        "test_date": _iso(test.created),
        "updated": _iso(test.updated),
        "days_old": (_today() - test.created).days if test.created else None,
        "informs_program": test.informs_program,
        "recommended_products": list(test.recommended),
        "history": soil_history(data.snapshot.soil_tests.values()),
    }


def _next_start(data: YmData) -> date | None:
    return data.next_window.start if data.next_window else None


def _days_until(data: YmData) -> int | None:
    start = _next_start(data)
    return (start - _today()).days if start else None


@dataclass(frozen=True, kw_only=True)
class YmSensorDescription(SensorEntityDescription):
    value_fn: Callable[[YmData], Any]
    # (data, raw store payloads) -> attributes; the raw payloads carry what YmData does not (lawn program status).
    attrs_fn: Callable[[YmData, dict], dict] | None = None


SENSORS: tuple[YmSensorDescription, ...] = (
    YmSensorDescription(
        key="soil_temperature",
        translation_key="soil_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        value_fn=_soil_temp,
        attrs_fn=lambda d, _: {
            "zip_status": d.soil_temp.get("zipCodeStatus"),
            "ambient_icon": d.soil_temp.get("ambientTempIcon"),
        },
    ),
    YmSensorDescription(key="program", translation_key="program", value_fn=_program_state, attrs_fn=_program_attrs),
    YmSensorDescription(
        key="current_window",
        translation_key="current_window",
        value_fn=lambda d: d.current_window.name if d.current_window else None,
        attrs_fn=lambda d, _: _window_attrs(d.current_window, d, details=True),
    ),
    YmSensorDescription(
        key="current_window_progress",
        translation_key="current_window_progress",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_progress,
        attrs_fn=_progress_attrs,
    ),
    YmSensorDescription(
        key="next_window",
        translation_key="next_window",
        value_fn=lambda d: d.next_window.name if d.next_window else None,
        attrs_fn=lambda d, _: _window_attrs(d.next_window, d, details=False),
    ),
    YmSensorDescription(
        key="next_window_start",
        translation_key="next_window_start",
        device_class=SensorDeviceClass.DATE,
        value_fn=_next_start,
    ),
    YmSensorDescription(
        key="days_until_next_window",
        translation_key="days_until_next_window",
        native_unit_of_measurement="d",
        value_fn=_days_until,
    ),
    YmSensorDescription(
        key="last_application",
        translation_key="last_application",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: e.day if (e := _latest_entry(d, applied=True)) else None,
        attrs_fn=_last_application_attrs,
    ),
    YmSensorDescription(
        key="last_journal_entry",
        translation_key="last_journal_entry",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: e.day if (e := _latest_entry(d)) else None,
        attrs_fn=_last_journal_attrs,
    ),
    YmSensorDescription(
        key="applications_this_year",
        translation_key="applications_this_year",
        value_fn=lambda d: _season(d)["applications"],
    ),
    YmSensorDescription(
        key="nitrogen_this_year",
        translation_key="nitrogen_this_year",
        native_unit_of_measurement="lb N/1000 ft²",
        value_fn=lambda d: _season(d)["n"],
        attrs_fn=_nitrogen_attrs,
    ),
    YmSensorDescription(
        key="soil_test",
        translation_key="soil_test",
        value_fn=lambda d: t.status if (t := selected_soil_test(d)) else None,
        attrs_fn=_soil_test_attrs,
    ),
    YmSensorDescription(
        key="dug_ai_turns",
        translation_key="dug_ai_turns",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _d(d.user_status.get("dugAi")).get("turnsUsed"),
        attrs_fn=lambda d, _: {
            "limit": _d(d.user_status.get("dugAi")).get("turnsLimit"),
            "reset_at": _d(d.user_status.get("dugAi")).get("turnsResetAt"),
        },
    ),
    YmSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.last_sync,
        # request_count is the client's lifetime count of requests, not per poll.
        attrs_fn=lambda d, _: {"request_count": d.request_count, "last_error": d.last_error},
    ),
)


class YmSensor(YmEntity, SensorEntity):
    entity_description: YmSensorDescription

    def __init__(self, coordinator: YmCoordinator, description: YmSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.key == "soil_temperature":
            unit = self.coordinator.data.soil_temp.get("soilTempUnit")
            return UnitOfTemperature.CELSIUS if unit == "C" else UnitOfTemperature.FAHRENHEIT
        return self.entity_description.native_unit_of_measurement

    @property
    def native_value(self) -> str | int | float | date | datetime | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data, self.coordinator.store.latest) if fn else None


class YmNutrientSensor(YmEntity, SensorEntity):
    """One measured value of the selected soil test."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:flask-outline"

    def __init__(self, coordinator: YmCoordinator, nutrient_key: str) -> None:
        super().__init__(coordinator, f"soil_{nutrient_key}")
        self._nutrient = nutrient_key
        group, _, rest = nutrient_key.partition("_")
        label = _NUTRIENT_LABELS.get(rest) or (rest.replace("_", " ").upper() if len(rest) <= 3 else rest.replace("_", " ").title())
        self._attr_name = f"Soil {label}" + ("" if group == "ppm" else f" ({group})")
        self._attr_entity_registry_enabled_default = rest in _MAIN_NUTRIENTS
        nutrient = self._current()
        if nutrient is not None and rest != "ph":
            self._attr_native_unit_of_measurement = nutrient.unit
        if nutrient is not None and isinstance(nutrient.value, (int, float)):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    def _current(self):
        test = selected_soil_test(self.coordinator.data)
        return test.nutrients.get(self._nutrient) if test else None

    @property
    def native_value(self):
        nutrient = self._current()
        return nutrient.value if nutrient else None

    @property
    def extra_state_attributes(self) -> dict | None:
        nutrient = self._current()
        if nutrient is None:
            return None
        group = self._nutrient.partition("_")[0]
        depth = selected_soil_test(self.coordinator.data).nutrients.get(f"{group}_depth")
        return {"optimal": nutrient.optimal, "status": nutrient.status, "depth": depth.value if depth else None}


class YmSegmentSensor(YmEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "ft²"
    _attr_icon = "mdi:grass"

    def __init__(self, coordinator: YmCoordinator, segment_id: int) -> None:
        super().__init__(coordinator, f"segment_{segment_id}")
        self._segment_id = segment_id
        segment = self._segment()
        self._attr_name = segment.name if segment and segment.name else f"Segment {segment_id}"

    def _segment(self):
        return next((s for s in self.coordinator.data.segments if s.id == self._segment_id), None)

    @property
    def native_value(self) -> int | None:
        segment = self._segment()
        return segment.square_feet if segment else None

    @property
    def extra_state_attributes(self) -> dict | None:
        segment = self._segment()
        return {"color": segment.color, "segment_type": segment.segment_type} if segment else None


async def async_setup_entry(
    hass: HomeAssistant, entry: YmConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(YmSensor(coordinator, description) for description in SENSORS)
    known: set[str] = set()

    def _discover() -> None:
        """Nutrient and segment sensors follow the data: new ones appear once a report or segment shows up."""
        new: list[SensorEntity] = []
        test = selected_soil_test(coordinator.data)
        for key in test.nutrients if test else ():
            if f"soil_{key}" not in known:
                known.add(f"soil_{key}")
                new.append(YmNutrientSensor(coordinator, key))
        for segment in coordinator.data.segments:
            if f"segment_{segment.id}" not in known:
                known.add(f"segment_{segment.id}")
                new.append(YmSegmentSensor(coordinator, segment.id))
        if new:
            async_add_entities(new)

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))
