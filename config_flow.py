"""Config flow for My AC Controller.

Multi-step setup wizard:
  1. user        — name, AC entity, temperature sensor, mode
  2. bp_settings — inverter-mode thresholds (only for bp mode)
  3. dp_settings — fixed-speed thresholds (only for dp mode)
  4. common      — poll interval
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_DIFF_LOW,
    CONF_DIFF_OFF,
    CONF_DIFF_OFF_DP,
    CONF_DIFF_ON,
    CONF_MODE,
    CONF_POLL_INTERVAL,
    CONF_ROUND_DIRECTION,
    CONF_STEP,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_DIFF_LOW,
    DEFAULT_DIFF_OFF,
    DEFAULT_DIFF_OFF_DP,
    DEFAULT_DIFF_ON,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_ROUND_DIRECTION,
    DEFAULT_STEP,
    DOMAIN,
    MAX_DIFF,
    MAX_POLL_INTERVAL,
    MAX_STEP,
    MIN_DIFF,
    MIN_POLL_INTERVAL,
    MIN_STEP,
    MODE_BP,
    MODE_DP,
    ROUND_DOWN,
    ROUND_UP,
)

_LOGGER = logging.getLogger(__name__)


# --- UI Selectors ---

MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value=MODE_BP, label="Inverter (variable-frequency)"),
            selector.SelectOptionDict(value=MODE_DP, label="Fixed-speed (on/off)"),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

ROUND_DIRECTION_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value=ROUND_UP, label="Round Up"),
            selector.SelectOptionDict(value=ROUND_DOWN, label="Round Down"),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


def _climate_entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="climate",
            multiple=False,
        )
    )


def _temperature_sensor_selector() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=["sensor", "number", "input_number"],
            device_class="temperature",
            multiple=False,
        )
    )


# ------------------------------------------------------------------
# Config Flow (initial setup)
# ------------------------------------------------------------------


class MyACControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for My AC Controller."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: Name, AC entity, temperature sensor, mode."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)

            if not user_input.get(CONF_NAME, "").strip():
                errors[CONF_NAME] = "name_required"

            if not errors:
                mode = user_input[CONF_MODE]
                if mode == MODE_BP:
                    return await self.async_step_bp_settings()
                else:
                    return await self.async_step_dp_settings()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._data.get(CONF_NAME, "")): str,
                vol.Required(
                    CONF_CLIMATE_ENTITY,
                    default=self._data.get(CONF_CLIMATE_ENTITY),
                ): _climate_entity_selector(),
                vol.Required(
                    CONF_TEMPERATURE_SENSOR,
                    default=self._data.get(CONF_TEMPERATURE_SENSOR),
                ): _temperature_sensor_selector(),
                vol.Required(
                    CONF_MODE,
                    default=self._data.get(CONF_MODE, MODE_BP),
                ): MODE_SELECTOR,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_bp_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2a: Inverter mode thresholds."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._options.update(user_input)

            # Validate: diff_off should typically be <= diff_low
            diff_off = float(user_input[CONF_DIFF_OFF])
            diff_low = float(user_input[CONF_DIFF_LOW])
            if diff_off > diff_low:
                errors[CONF_DIFF_OFF] = "diff_off_greater_than_diff_low"

            if not errors:
                return await self.async_step_common()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DIFF_OFF,
                    default=self._options.get(CONF_DIFF_OFF, DEFAULT_DIFF_OFF),
                ): vol.All(vol.Coerce(float), vol.Range(min=MIN_DIFF, max=MAX_DIFF)),
                vol.Required(
                    CONF_DIFF_LOW,
                    default=self._options.get(CONF_DIFF_LOW, DEFAULT_DIFF_LOW),
                ): vol.All(vol.Coerce(float), vol.Range(min=MIN_DIFF, max=MAX_DIFF)),
                vol.Required(
                    CONF_STEP,
                    default=self._options.get(CONF_STEP, DEFAULT_STEP),
                ): vol.All(vol.Coerce(float), vol.Range(min=MIN_STEP, max=MAX_STEP)),
                vol.Required(
                    CONF_ROUND_DIRECTION,
                    default=self._options.get(CONF_ROUND_DIRECTION, DEFAULT_ROUND_DIRECTION),
                ): ROUND_DIRECTION_SELECTOR,
            }
        )

        return self.async_show_form(
            step_id="bp_settings",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_dp_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2b: Fixed-speed mode thresholds."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._options.update(user_input)

            # Validate: diff_on should be >= diff_off_dp for proper hysteresis
            diff_on = float(user_input[CONF_DIFF_ON])
            diff_off = float(user_input[CONF_DIFF_OFF_DP])
            if diff_on < diff_off:
                errors[CONF_DIFF_ON] = "diff_on_less_than_diff_off"

            if not errors:
                return await self.async_step_common()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DIFF_ON,
                    default=self._options.get(CONF_DIFF_ON, DEFAULT_DIFF_ON),
                ): vol.All(vol.Coerce(float), vol.Range(min=MIN_DIFF, max=MAX_DIFF)),
                vol.Required(
                    CONF_DIFF_OFF_DP,
                    default=self._options.get(CONF_DIFF_OFF_DP, DEFAULT_DIFF_OFF_DP),
                ): vol.All(vol.Coerce(float), vol.Range(min=MIN_DIFF, max=MAX_DIFF)),
            }
        )

        return self.async_show_form(
            step_id="dp_settings",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_common(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 3: Common settings (poll interval)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._options.update(user_input)

            # Merge options into data for initial creation
            # (options are stored as entry options, data is structural)
            title = self._data.pop(CONF_NAME, "My AC Controller")

            return self.async_create_entry(
                title=title,
                data=self._data,
                options=self._options,
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=self._options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                ),
            }
        )

        return self.async_show_form(
            step_id="common",
            data_schema=schema,
            errors=errors,
            last_step=True,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow handler."""
        return MyACControllerOptionsFlow(config_entry)


# ------------------------------------------------------------------
# Options Flow (reconfiguration)
# ------------------------------------------------------------------


class MyACControllerOptionsFlow(config_entries.OptionsFlow):
    """Handle options update for My AC Controller."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize."""
        self._entry = config_entry
        self._options: dict[str, Any] = dict(config_entry.options)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Entry point — show a menu of option groups."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["mode_settings", "common_settings"],
        )

    async def async_step_mode_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit mode-specific thresholds."""
        mode = self._entry.data.get(CONF_MODE, MODE_BP)
        errors: dict[str, str] = {}

        if user_input is not None:
            self._options.update(user_input)

            if mode == MODE_BP:
                diff_off = float(user_input[CONF_DIFF_OFF])
                diff_low = float(user_input[CONF_DIFF_LOW])
                if diff_off > diff_low:
                    errors[CONF_DIFF_OFF] = "diff_off_greater_than_diff_low"
            else:
                diff_on = float(user_input[CONF_DIFF_ON])
                diff_off = float(user_input[CONF_DIFF_OFF_DP])
                if diff_on < diff_off:
                    errors[CONF_DIFF_ON] = "diff_on_less_than_diff_off"

            if not errors:
                return self.async_create_entry(
                    title="",
                    data=self._options,
                )

        if mode == MODE_BP:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_DIFF_OFF,
                        default=self._options.get(CONF_DIFF_OFF, DEFAULT_DIFF_OFF),
                    ): vol.All(vol.Coerce(float), vol.Range(min=MIN_DIFF, max=MAX_DIFF)),
                    vol.Required(
                        CONF_DIFF_LOW,
                        default=self._options.get(CONF_DIFF_LOW, DEFAULT_DIFF_LOW),
                    ): vol.All(vol.Coerce(float), vol.Range(min=MIN_DIFF, max=MAX_DIFF)),
                    vol.Required(
                        CONF_STEP,
                        default=self._options.get(CONF_STEP, DEFAULT_STEP),
                    ): vol.All(vol.Coerce(float), vol.Range(min=MIN_STEP, max=MAX_STEP)),
                    vol.Required(
                        CONF_ROUND_DIRECTION,
                        default=self._options.get(CONF_ROUND_DIRECTION, DEFAULT_ROUND_DIRECTION),
                    ): ROUND_DIRECTION_SELECTOR,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_DIFF_ON,
                        default=self._options.get(CONF_DIFF_ON, DEFAULT_DIFF_ON),
                    ): vol.All(vol.Coerce(float), vol.Range(min=MIN_DIFF, max=MAX_DIFF)),
                    vol.Required(
                        CONF_DIFF_OFF_DP,
                        default=self._options.get(CONF_DIFF_OFF_DP, DEFAULT_DIFF_OFF_DP),
                    ): vol.All(vol.Coerce(float), vol.Range(min=MIN_DIFF, max=MAX_DIFF)),
                }
            )

        return self.async_show_form(
            step_id="mode_settings",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_common_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit common settings (poll interval)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(
                title="",
                data=self._options,
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=self._options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                ),
            }
        )

        return self.async_show_form(
            step_id="common_settings",
            data_schema=schema,
            errors=errors,
        )
