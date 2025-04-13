"""Support for MAX! binary sensors via MAX! Cube."""

from __future__ import annotations

import logging

from maxcube.device import MaxDevice # Import MaxDevice

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription, # <--- Import the correct class here
)
from homeassistant.config_entries import ConfigEntry # Import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback # Import callback
from homeassistant.helpers.entity import DeviceInfo # Import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity # Import CoordinatorEntity

from . import MaxCubeDataUpdateCoordinator, get_max_device_info # Import coordinator and helper
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the MAX! Cube binary sensor platform."""
    # Get the coordinator for this config entry
    coordinator: MaxCubeDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    cube = coordinator.cube # Access the cube object from the coordinator

    # Find all devices and create entities
    entities: list[MaxCubeBinarySensorBase] = []
    for device in cube.devices:
        # Create battery sensor for all devices that report battery level
        # Check if battery attribute exists and is not None before creating sensor
        if hasattr(device, 'battery') and device.battery is not None:
             entities.append(MaxCubeBattery(coordinator, device))

        # Create window shutter sensor if applicable
        if device.is_windowshutter():
            entities.append(MaxCubeShutter(coordinator, device))

    if entities:
        async_add_entities(entities)


class MaxCubeBinarySensorBase(CoordinatorEntity[MaxCubeDataUpdateCoordinator], BinarySensorEntity):
    """Base class for maxcube binary sensors."""

    _attr_has_entity_name = True # Use device name as entity name base
    # Associate the entity description defined in subclasses
    entity_description: BinarySensorEntityDescription

    def __init__(self, coordinator: MaxCubeDataUpdateCoordinator, device: MaxDevice) -> None:
        """Initialize MAX! Cube BinarySensorEntity."""
        super().__init__(coordinator) # Initialize CoordinatorEntity
        self._device = device
        self._cube = coordinator.cube # Store cube reference for convenience
        # Generate Device Info
        self._attr_device_info = get_max_device_info(self._cube, self._device)
        # Set unique ID based on device serial and sensor type (from entity_description)
        self._attr_unique_id = f"{self._device.serial}_{self.entity_description.key}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Find the device instance in the coordinator's data
        # Use the rf_address for reliable lookup
        updated_device = self.coordinator.data.device_by_rf(self._device.rf_address)
        if updated_device:
            self._device = updated_device # Update the internal device state
            self.async_write_ha_state() # Update HA state
        else:
            _LOGGER.warning("Device %s (%s) not found after update", self._device.name, self._device.rf_address)
            # Optionally mark the entity as unavailable
            # self._attr_available = False
            # self.async_write_ha_state()


class MaxCubeShutter(MaxCubeBinarySensorBase):
    """Representation of a MAX! Cube Window Shutter sensor."""

    # Use BinarySensorEntityDescription here
    entity_description = BinarySensorEntityDescription(
        key="window_shutter",
        device_class=BinarySensorDeviceClass.WINDOW,
        name="Window Open", # This will be appended to the device name
    )

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on/open."""
        # Ensure device object is up-to-date via coordinator
        # The actual device state is already updated in _handle_coordinator_update
        if self._device and hasattr(self._device, 'is_open'):
             return self._device.is_open
        return None # Return None if state is unknown


class MaxCubeBattery(MaxCubeBinarySensorBase):
    """Representation of a MAX! Cube Battery sensor."""

    # Use BinarySensorEntityDescription here
    entity_description = BinarySensorEntityDescription(
        key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        name="Battery Low", # This will be appended to the device name
    )

    @property
    def is_on(self) -> bool | None:
        """Return true if the battery is low (device.battery == 1)."""
        # Ensure device object is up-to-date via coordinator
        # The actual device state is already updated in _handle_coordinator_update
        if self._device and hasattr(self._device, 'battery'):
            # MAX! Cube reports 1 for low battery, 0 for OK.
            return self._device.battery == 1
        return None # Return None if state is unknown

