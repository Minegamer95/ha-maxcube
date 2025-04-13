"""Config flow for eQ-3 MAX! Cube."""

import logging
from typing import Any

from maxcube.cube import MaxCube
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.dt import now # Import now from Home Assistant utils

from .const import DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    }
)

async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    host = data[CONF_HOST]
    port = data[CONF_PORT]

    try:
        # Run blocking network I/O in executor
        cube = await hass.async_add_executor_job(MaxCube, host, port, now)
        # Ensure connection is closed after validation
        await hass.async_add_executor_job(cube.disconnect)

    except TimeoutError as exc:
        _LOGGER.error("Unable to connect to Max!Cube gateway: %s", exc)
        raise CannotConnect from exc
    except Exception as exc: # Catch other potential exceptions during connection
        _LOGGER.error("Unexpected error connecting to Max!Cube gateway: %s", exc)
        raise CannotConnect from exc # Raise specific error for flow

    # Return info that you want to store in the config entry.
    # Use cube serial as unique ID to prevent duplicate entries
    return {"title": f"MAX! Cube ({host})", "unique_id": cube.serial}


class MaxCubeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for eQ-3 MAX! Cube."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                # Validate the user input
                info = await validate_input(self.hass, user_input)
                # Set unique ID before creating entry
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(title=info["title"], data=user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Show the form to the user
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
