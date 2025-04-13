"""Support for the MAX! Cube LAN Gateway."""

import asyncio
import logging
from datetime import timedelta
from threading import Lock
import time

from maxcube.cube import MaxCube
from maxcube.device import MaxDevice
from maxcube.room import MaxRoom

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr # Import device registry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now # Import now from Home Assistant utils

from .const import DATA_MAXCUBE_HANDLE, DOMAIN, MANUFACTURER

_LOGGER = logging.getLogger(__name__)

# Define platforms to be set up
PLATFORMS = [Platform.CLIMATE, Platform.BINARY_SENSOR]

# Define a reasonable default scan interval
DEFAULT_SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MAX! Cube from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]

    # Use a DataUpdateCoordinator for handling updates
    # We use a relatively long scan interval by default.
    # The coordinator will manage the background updates.
    coordinator = MaxCubeDataUpdateCoordinator(hass, host, port, DEFAULT_SCAN_INTERVAL)

    # Fetch initial data so we have data when entities subscribe
    # Raises ConfigEntryNotReady on failure
    await coordinator.async_config_entry_first_refresh()

    # Store the coordinator object for platforms to access
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Register the Gateway device itself
    device_registry = dr.async_get(hass)
    gateway_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, coordinator.cube.serial)}, # Use cube serial as identifier
        name=f"MAX! Cube ({host})",
        manufacturer=MANUFACTURER,
        model="MAX! Cube LAN Gateway",
        sw_version=coordinator.cube.firmware_version, # Get firmware from the cube itself
        configuration_url=f"http://{host}", # Add configuration URL if accessible
    )
    _LOGGER.debug("Registered gateway device: %s", gateway_device.id)


    # Set up platforms (climate, binary_sensor)
    # This might trigger the blocking import warning on first load
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for Home Assistant stop event to disconnect
    @callback
    def _async_disconnect(event: Event) -> None:
        """Disconnect from MAX! Cube on HA stop."""
        _LOGGER.info("Disconnecting from MAX! Cube %s", host)
        # Run blocking disconnect in executor
        hass.async_add_executor_job(coordinator.cube.disconnect)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_disconnect)
    )
    # Add listener to disconnect when entry is unloaded
    entry.async_on_unload(coordinator.async_unload)


    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Forward the unload request to platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Remove the coordinator from hass.data
        coordinator: MaxCubeDataUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Ensure disconnection happens in executor
        await hass.async_add_executor_job(coordinator.cube.disconnect)
        # Clean up hass.data[DOMAIN] if it's empty
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)

    return unload_ok


class MaxCubeDataUpdateCoordinator(DataUpdateCoordinator[MaxCube]):
    """Class to manage fetching MAX! Cube data."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        update_interval: timedelta,
    ) -> None:
        """Initialize."""
        self.cube = MaxCube(host, port, now=now) # Pass now function
        self._host = host # Store host for logging
        self._hass = hass # Store hass instance
        self._update_lock = asyncio.Lock() # Use asyncio lock

        # Determine if persistent connection should be used based on update interval
        self.cube.use_persistent_connection = update_interval <= timedelta(seconds=300)

        super().__init__(
            hass,
            _LOGGER,
            name=f"MAX! Cube {host}",
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> MaxCube:
        """Fetch data from MAX! Cube.

        This is the core function of the coordinator.
        It runs in the event loop and schedules the blocking update call.
        """
        async with self._update_lock: # Prevent concurrent updates
            try:
                _LOGGER.debug("Updating data from MAX! Cube %s", self._host)
                # Run the blocking update call in the executor
                await self._hass.async_add_executor_job(self.cube.update)
                _LOGGER.debug("Update finished for %s. Found %d devices.", self._host, len(self.cube.devices))
                # Return the updated cube object (contains all device data)
                return self.cube
            except TimeoutError as err:
                # Let UpdateFailed handle the exception and logging
                _LOGGER.warning("Timeout communicating with MAX! Cube %s: %s", self._host, err)
                raise UpdateFailed(f"Timeout communicating with MAX! Cube {self._host}: {err}") from err
            except Exception as err:
                # Catch unexpected errors during update
                _LOGGER.error("Error communicating with MAX! Cube %s: %s", self._host, err, exc_info=True)
                raise UpdateFailed(f"Error communicating with MAX! Cube {self._host}: {err}") from err

    @callback
    def async_unload(self) -> None:
        """Clean up resources when the coordinator is unloaded."""
        # This method is called when the entry is unloaded
        _LOGGER.debug("Initiating disconnection for %s", self._host)
        # Run the blocking disconnect call in the executor
        # We don't await this directly as unload should be quick
        self._hass.async_add_executor_job(self.cube.disconnect)

# Helper function to get device info - used by platforms
def get_max_device_info(cube: MaxCube, device: MaxDevice) -> DeviceInfo:
    """Get device info for a MAX! device."""
    room = cube.room_by_id(device.room_id)
    room_name = room.name if room else "Unknown Room"
    device_name = f"{room_name} {device.name}"

    # Determine model based on type (add more types as needed)
    model = "Unknown Device"
    # Use constants from maxcube library if available, otherwise strings
    try:
        from maxcube.device import (
             MAX_CUBE, MAX_THERMOSTAT, MAX_THERMOSTAT_PLUS,
             MAX_WALL_THERMOSTAT, MAX_WINDOW_SHUTTER, MAX_ECO_SWITCH
        )
        DEVICE_TYPE_MAP = {
            MAX_THERMOSTAT: "Thermostat",
            MAX_THERMOSTAT_PLUS: "Thermostat+",
            MAX_WALL_THERMOSTAT: "Wall Thermostat",
            MAX_WINDOW_SHUTTER: "Window Shutter",
            MAX_ECO_SWITCH: "Eco Switch",
            MAX_CUBE: "Cube" # Should not happen here, but include for completeness
        }
        model = DEVICE_TYPE_MAP.get(device.type, f"Unknown Type ({device.type})")
    except ImportError:
        # Fallback if constants are not available in the installed library version
        if device.is_thermostat():
            model = "Thermostat"
        elif device.is_wallthermostat():
            model = "Wall Thermostat"
        elif device.is_windowshutter():
            model = "Window Shutter"
        # Add other types if needed

    # Safely get firmware_version using getattr, default to None if not present
    sw_version = getattr(device, 'firmware_version', None)

    return DeviceInfo(
        identifiers={(DOMAIN, device.serial)},
        name=device_name,
        manufacturer=MANUFACTURER,
        model=model,
        sw_version=sw_version, # Use the safely obtained version (can be None)
        # Link device to the gateway device
        via_device=(DOMAIN, cube.serial),
    )

