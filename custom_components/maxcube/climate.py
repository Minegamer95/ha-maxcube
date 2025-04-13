"""Support for MAX! Thermostats via MAX! Cube."""

from __future__ import annotations

import logging
from typing import Any

from maxcube.device import (
    MAX_DEVICE_MODE_AUTOMATIC,
    MAX_DEVICE_MODE_BOOST,
    MAX_DEVICE_MODE_MANUAL,
    MAX_DEVICE_MODE_VACATION,
    MaxDevice, # Import MaxDevice
)

from homeassistant.components.climate import (
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT, # Comfort/Eco are now standard presets
    PRESET_ECO,
    # PRESET_NONE is deprecated, use None instead
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry # Import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback # Import callback
from homeassistant.exceptions import HomeAssistantError # For error handling
from homeassistant.helpers.entity import DeviceInfo # Import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity # Import CoordinatorEntity

from . import MaxCubeDataUpdateCoordinator, get_max_device_info # Import coordinator and helper
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ATTR_VALVE_POSITION = "valve_position"
PRESET_ON = "on" # Keep custom 'on' preset if needed

# Temperature constants
OFF_TEMPERATURE = 4.5
ON_TEMPERATURE = 30.5
MIN_TEMPERATURE = 5.0
MAX_TEMPERATURE = 30.0

# Map MAX! Cube modes to Home Assistant HVAC modes and presets
MODE_TO_HVAC_MODE = {
    MAX_DEVICE_MODE_AUTOMATIC: HVACMode.AUTO,
    MAX_DEVICE_MODE_MANUAL: HVACMode.HEAT, # Treat manual as HEAT unless temp is OFF_TEMPERATURE
    MAX_DEVICE_MODE_VACATION: HVACMode.AUTO, # Or HEAT? Usually vacation mode follows a schedule or fixed temp
    MAX_DEVICE_MODE_BOOST: HVACMode.AUTO, # Boost is temporary, return to AUTO/HEAT afterwards
}

HVAC_MODE_TO_MODE = {
    HVACMode.AUTO: MAX_DEVICE_MODE_AUTOMATIC,
    HVACMode.HEAT: MAX_DEVICE_MODE_MANUAL,
    HVACMode.OFF: MAX_DEVICE_MODE_MANUAL, # Off is set via temperature in manual mode
}

# Define supported presets
SUPPORT_PRESETS = [
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_AWAY,
    PRESET_ON, # Keep custom 'on' preset
]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the MAX! Cube climate platform."""
    # Get the coordinator for this config entry
    coordinator: MaxCubeDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    cube = coordinator.cube # Access the cube object from the coordinator

    # Find all thermostat devices and create entities
    entities = [
        MaxCubeClimate(coordinator, device)
        for device in cube.devices
        if device.is_thermostat() or device.is_wallthermostat()
    ]

    if entities:
        async_add_entities(entities)


class MaxCubeClimate(CoordinatorEntity[MaxCubeDataUpdateCoordinator], ClimateEntity):
    """MAX! Cube ClimateEntity."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        # TURN_ON/TURN_OFF are implicitly supported via HVAC modes
    )
    _attr_has_entity_name = True # Use device name as entity name base
    _attr_name = None # Set to None because _attr_has_entity_name is True

    def __init__(self, coordinator: MaxCubeDataUpdateCoordinator, device: MaxDevice) -> None:
        """Initialize MAX! Cube ClimateEntity."""
        super().__init__(coordinator) # Initialize CoordinatorEntity
        self._device = device
        self._cube = coordinator.cube # Store cube reference for convenience

        # Generate Device Info
        self._attr_device_info = get_max_device_info(self._cube, self._device)
        self._attr_unique_id = self._device.serial # Use device serial as unique ID

        # Set initial state based on device data from coordinator
        self._update_attrs()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Find the device instance in the coordinator's data
        updated_device = self.coordinator.data.device_by_rf(self._device.rf_address)
        if updated_device:
            self._device = updated_device # Update the internal device state
            self._update_attrs() # Update attributes based on new state
            self.async_write_ha_state() # Update HA state
        else:
            _LOGGER.warning("Device %s not found after update", self.unique_id)
            # Optionally mark the entity as unavailable
            # self._attr_available = False
            # self.async_write_ha_state()

    @callback
    def _update_attrs(self) -> None:
        """Update climate entity attributes based on the current device state."""
        # Determine HVAC mode
        mode = self._device.mode
        target_temp = self._device.target_temperature

        if mode == MAX_DEVICE_MODE_MANUAL and target_temp == OFF_TEMPERATURE:
            self._attr_hvac_mode = HVACMode.OFF
        elif mode in MODE_TO_HVAC_MODE:
             # Boost/Vacation might need special handling depending on desired HA representation
             # For now, map them based on MODE_TO_HVAC_MODE
             self._attr_hvac_mode = MODE_TO_HVAC_MODE[mode]
             if self._attr_hvac_mode == HVACMode.HEAT and target_temp == OFF_TEMPERATURE:
                 # If somehow in HEAT mode but temp is OFF, switch to OFF
                 self._attr_hvac_mode = HVACMode.OFF
        else:
             self._attr_hvac_mode = None # Unknown mode

        # Determine HVAC action
        valve = 0
        if self._device.is_thermostat() and hasattr(self._device, 'valve_position'):
            valve = self._device.valve_position
        elif self._device.is_wallthermostat():
            # Find associated thermostat's valve position
            room = self._cube.room_by_id(self._device.room_id)
            if room:
                for dev in self._cube.devices_by_room(room):
                    if dev.is_thermostat() and hasattr(dev, 'valve_position') and dev.valve_position > valve:
                        valve = dev.valve_position

        if self._attr_hvac_mode == HVACMode.OFF:
            self._attr_hvac_action = HVACAction.OFF
        elif valve > 0:
            self._attr_hvac_action = HVACAction.HEATING
        else:
            self._attr_hvac_action = HVACAction.IDLE

        # Update temperatures
        self._attr_current_temperature = self._device.actual_temperature

        # Only set target temp if not OFF
        if self._attr_hvac_mode != HVACMode.OFF:
            # Ensure target temp is within valid HA range
            temp = self._device.target_temperature
            if temp is not None and self.min_temp <= temp <= self.max_temp:
                 self._attr_target_temperature = temp
            else:
                 # If target is invalid (e.g., ON_TEMPERATURE), try to get a sensible default
                 # or leave it None if mode is AUTO
                 if self._attr_hvac_mode == HVACMode.AUTO:
                     self._attr_target_temperature = None # Let schedule decide
                 else: # Manual/Heat mode needs a target
                     # Fallback to comfort temp? Or current temp?
                     self._attr_target_temperature = self._device.comfort_temperature # Example fallback

        else:
            self._attr_target_temperature = None # No target temp when OFF

        # Update preset mode
        self._attr_preset_mode = self._get_current_preset()

        # Update extra state attributes (valve position)
        extra_attrs = {}
        if self._device.is_thermostat() and hasattr(self._device, 'valve_position'):
             extra_attrs[ATTR_VALVE_POSITION] = self._device.valve_position
        self._attr_extra_state_attributes = extra_attrs

    def _get_current_preset(self) -> str | None:
        """Return the current preset mode based on device state."""
        mode = self._device.mode
        target_temp = self._device.target_temperature
        comfort_temp = self._device.comfort_temperature
        eco_temp = self._device.eco_temperature

        if mode == MAX_DEVICE_MODE_BOOST:
            return PRESET_BOOST
        if mode == MAX_DEVICE_MODE_VACATION:
            return PRESET_AWAY
        if mode == MAX_DEVICE_MODE_MANUAL:
            if target_temp == comfort_temp:
                return PRESET_COMFORT
            if target_temp == eco_temp:
                return PRESET_ECO
            if target_temp == ON_TEMPERATURE:
                return PRESET_ON
        # If no specific preset matches, return None (represents 'none' or 'auto' schedule)
        return None

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        # Use device min_temp if available, otherwise default MIN_TEMPERATURE
        min_t = getattr(self._device, 'min_temperature', MIN_TEMPERATURE) or MIN_TEMPERATURE
        # Ensure it's not below the absolute minimum usable value
        return max(min_t, MIN_TEMPERATURE)

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        # Use device max_temp if available, otherwise default MAX_TEMPERATURE
        max_t = getattr(self._device, 'max_temperature', MAX_TEMPERATURE) or MAX_TEMPERATURE
        # Ensure it's not above the absolute maximum usable value
        return min(max_t, MAX_TEMPERATURE)

    @property
    def preset_modes(self) -> list[str] | None:
        """Return a list of available preset modes."""
        return SUPPORT_PRESETS

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        _LOGGER.debug("Setting HVAC mode to %s for %s", hvac_mode, self.unique_id)
        if hvac_mode == HVACMode.OFF:
            # Set temperature to OFF_TEMPERATURE in manual mode
            await self._async_set_temperature_mode(OFF_TEMPERATURE, MAX_DEVICE_MODE_MANUAL)
        elif hvac_mode == HVACMode.HEAT:
            # Set to manual mode with the current target temperature
            # or fallback to comfort temperature if current target is invalid/off
            current_target = self.target_temperature
            temp = current_target if current_target is not None and current_target > OFF_TEMPERATURE else self._device.comfort_temperature
            temp = max(temp, self.min_temp) # Ensure temp is at least min_temp
            await self._async_set_temperature_mode(temp, MAX_DEVICE_MODE_MANUAL)
        elif hvac_mode == HVACMode.AUTO:
            # Set to automatic mode (temperature is controlled by schedule)
            await self._async_set_temperature_mode(None, MAX_DEVICE_MODE_AUTOMATIC)
        else:
            _LOGGER.warning("Unsupported HVAC mode: %s", hvac_mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        _LOGGER.debug("Setting preset mode to %s for %s", preset_mode, self.unique_id)
        mode = None
        temp = None

        if preset_mode == PRESET_BOOST:
            mode = MAX_DEVICE_MODE_BOOST
        elif preset_mode == PRESET_AWAY:
            mode = MAX_DEVICE_MODE_VACATION
            # Vacation temp is usually set separately or uses a default
        elif preset_mode == PRESET_COMFORT:
            mode = MAX_DEVICE_MODE_MANUAL
            temp = self._device.comfort_temperature
        elif preset_mode == PRESET_ECO:
            mode = MAX_DEVICE_MODE_MANUAL
            temp = self._device.eco_temperature
        elif preset_mode == PRESET_ON:
            mode = MAX_DEVICE_MODE_MANUAL
            temp = ON_TEMPERATURE
        # Setting preset to None should revert to AUTO mode
        # elif preset_mode is None: # Check for None explicitly if needed
        #     mode = MAX_DEVICE_MODE_AUTOMATIC
        else:
            _LOGGER.warning("Unsupported preset mode: %s", preset_mode)
            return # Do nothing if preset is unsupported

        await self._async_set_temperature_mode(temp, mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
             _LOGGER.warning("Temperature not provided to set_temperature")
             return

        # Clamp temperature to valid range
        temp = max(self.min_temp, min(self.max_temp, temp))

        _LOGGER.debug("Setting temperature to %.1f for %s", temp, self.unique_id)

        # Setting temperature implies manual mode
        await self._async_set_temperature_mode(temp, MAX_DEVICE_MODE_MANUAL)

    async def _async_set_temperature_mode(self, temperature: float | None, mode: int | None) -> None:
        """Send temperature and mode command to the MAX! Cube.

        Runs the blocking call in the executor.
        """
        # Use the cube object from the coordinator
        cube = self.coordinator.cube

        try:
            # Run blocking network I/O in executor
            await self.hass.async_add_executor_job(
                cube.set_temperature_mode, self._device, temperature, mode
            )
            # After sending command, request a refresh to get updated state
            await self.coordinator.async_request_refresh()
        except (TimeoutError, OSError) as err:
            _LOGGER.error("Error setting temperature/mode for %s: %s", self.unique_id, err)
            # Optionally re-raise or handle specific errors
            raise HomeAssistantError(f"Failed to set mode/temperature: {err}") from err
        except Exception as err: # Catch other potential library errors
             _LOGGER.error("Unexpected error setting temperature/mode for %s: %s", self.unique_id, err)
             raise HomeAssistantError(f"Unexpected error setting mode/temperature: {err}") from err
