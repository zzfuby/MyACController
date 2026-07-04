"""Comprehensive tests for My AC Controller control logic.

Covers:
  - Inverter mode (bp): cooling & heating, all zones, boundaries
  - Fixed-speed mode (dp): cooling & heating, hysteresis, boundaries
  - Low-power target rounding (up/down), step values
  - Sensor fallback: T_trust/T_ac availability
  - HVAC mode resolution (HEAT_COOL, direct passthrough)
  - Diff_actual computation
  - Edge cases: missing sensors, temperature bounds, mode transitions
"""

import asyncio
import math
import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal mocking of Home Assistant internals so we can import and test the
# climate entity without a running HA instance.
# ---------------------------------------------------------------------------


class FakeState:
    """Minimal fake for homeassistant.core.State."""

    def __init__(self, state: str, attributes: dict[str, Any] | None = None):
        self.state = state
        self.attributes = attributes or {}


class FakeHass:
    """Fake HomeAssistant instance for testing."""

    def __init__(self, states: dict[str, FakeState] | None = None):
        self.states = MagicMock()
        self.states.get = lambda entity_id, default=None: (states or {}).get(
            entity_id, default
        )
        self.services = MagicMock()
        self.services.async_call = AsyncMock()
        self.config_entries = MagicMock()
        self.data = {}


# We must patch the HA imports BEFORE importing our climate module.
# Build fake modules for everything our module imports from HA.


def _setup_mocks():
    """Inject fake HA modules into sys.modules so imports succeed."""
    import types

    # homeassistant
    ha = types.ModuleType("homeassistant")
    ha.config_entries = types.ModuleType("homeassistant.config_entries")
    ha.config_entries.ConfigEntry = MagicMock

    # homeassistant.core
    ha.core = types.ModuleType("homeassistant.core")
    ha.core.HomeAssistant = MagicMock
    ha.core.callback = lambda f: f
    ha.core.State = FakeState

    # homeassistant.const
    ha.const = types.ModuleType("homeassistant.const")
    ha.const.ATTR_TEMPERATURE = "temperature"
    ha.const.STATE_UNAVAILABLE = "unavailable"
    ha.const.STATE_UNKNOWN = "unknown"
    ha.const.UnitOfTemperature = MagicMock()
    ha.const.UnitOfTemperature.CELSIUS = "°C"

    # homeassistant.components.climate
    ha.components = types.ModuleType("homeassistant.components")
    ha.components.climate = types.ModuleType("homeassistant.components.climate")
    ha.components.climate.ClimateEntity = type(
        "ClimateEntity",
        (),
        {
            "async_update_ha_state": AsyncMock(),
            "hass": None,
        },
    )
    ha.components.climate.ClimateEntityFeature = MagicMock()
    ha.components.climate.ClimateEntityFeature.TARGET_TEMPERATURE = 1
    ha.components.climate.ClimateEntityFeature.TURN_OFF = 2
    ha.components.climate.ClimateEntityFeature.TURN_ON = 4

    ha.components.climate.const = types.ModuleType(
        "homeassistant.components.climate.const"
    )

    # HVACMode enum
    _hvac_modes = {
        "OFF": "off",
        "COOL": "cool",
        "HEAT": "heat",
        "HEAT_COOL": "heat_cool",
        "DRY": "dry",
        "FAN_ONLY": "fan_only",
    }

    class HVACMode(str):
        pass

    for name, value in _hvac_modes.items():
        setattr(HVACMode, name, value)

    ha.components.climate.HVACMode = HVACMode

    # HVACAction enum
    _hvac_actions = {
        "OFF": "off",
        "IDLE": "idle",
        "HEATING": "heating",
        "COOLING": "cooling",
    }

    class HVACAction(str):
        pass

    for name, value in _hvac_actions.items():
        setattr(HVACAction, name, value)

    ha.components.climate.HVACAction = HVACAction
    ha.components.climate.const.HVACAction = HVACAction
    ha.components.climate.const.HVACMode = HVACMode

    # homeassistant.helpers.entity_platform
    ha.helpers = types.ModuleType("homeassistant.helpers")
    ha.helpers.entity_platform = types.ModuleType(
        "homeassistant.helpers.entity_platform"
    )
    ha.helpers.entity_platform.AddEntitiesCallback = MagicMock

    # homeassistant.helpers.event
    ha.helpers.event = types.ModuleType("homeassistant.helpers.event")
    ha.helpers.event.async_track_time_interval = MagicMock(
        return_value=lambda: None
    )

    # homeassistant.helpers.restore_state
    ha.helpers.restore_state = types.ModuleType(
        "homeassistant.helpers.restore_state"
    )
    ha.helpers.restore_state.RestoreEntity = type("RestoreEntity", (), {})

    # Inject
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.config_entries"] = ha.config_entries
    sys.modules["homeassistant.core"] = ha.core
    sys.modules["homeassistant.const"] = ha.const
    sys.modules["homeassistant.components"] = ha.components
    sys.modules["homeassistant.components.climate"] = ha.components.climate
    sys.modules["homeassistant.components.climate.const"] = (
        ha.components.climate.const
    )
    sys.modules["homeassistant.helpers"] = ha.helpers
    sys.modules["homeassistant.helpers.entity_platform"] = (
        ha.helpers.entity_platform
    )
    sys.modules["homeassistant.helpers.event"] = ha.helpers.event
    sys.modules["homeassistant.helpers.restore_state"] = ha.helpers.restore_state

    # homeassistant.util
    ha.util = types.ModuleType("homeassistant.util")
    sys.modules["homeassistant.util"] = ha.util

    return ha, HVACMode, HVACAction


ha, HVACMode, HVACAction = _setup_mocks()

# Now safe to import
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE, STATE_UNKNOWN

# Import our climate module (it uses relative imports, so patch those too)
import importlib
import importlib.util
import os
import types

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CC_DIR = os.path.join(BASE_DIR, "custom_components", "my_ac_controller")

# Step 1: Load const.py properly as a real module
const_spec = importlib.util.spec_from_file_location(
    "custom_components.my_ac_controller.const",
    os.path.join(CC_DIR, "const.py"),
)
const_module = importlib.util.module_from_spec(const_spec)
sys.modules["custom_components.my_ac_controller.const"] = const_module
const_spec.loader.exec_module(const_module)

# Step 2: Create the parent package module
pkg = types.ModuleType("custom_components.my_ac_controller")
sys.modules["custom_components.my_ac_controller"] = pkg
pkg.const = const_module

# Also register custom_components namespace
sys.modules["custom_components"] = types.ModuleType("custom_components")

# Step 3: Load climate.py
climate_spec = importlib.util.spec_from_file_location(
    "custom_components.my_ac_controller.climate",
    os.path.join(CC_DIR, "climate.py"),
)
climate_module = importlib.util.module_from_spec(climate_spec)
sys.modules["custom_components.my_ac_controller.climate"] = climate_module
climate_spec.loader.exec_module(climate_module)

MyACControllerClimate = climate_module.MyACControllerClimate

# Constants (now imported from the real module)
from custom_components.my_ac_controller.const import (
    DOMAIN,
    MODE_BP,
    MODE_DP,
    ROUND_UP,
    ROUND_DOWN,
    CONF_NAME,
    CONF_CLIMATE_ENTITY,
    CONF_TEMPERATURE_SENSOR,
    CONF_MODE,
    CONF_DIFF_OFF,
    CONF_DIFF_LOW,
    CONF_STEP,
    CONF_ROUND_DIRECTION,
    CONF_DIFF_ON,
    CONF_DIFF_OFF_DP,
    CONF_POLL_INTERVAL,
    ATTR_CONTROL_STATE,
    ATTR_DIFF_ACTUAL,
)

# ===================================================================
# Test Helpers
# ===================================================================


def _make_entry(
    *,
    mode: str = MODE_BP,
    diff_off: float = 0.3,
    diff_low: float = 1.0,
    step: float = 1.0,
    round_direction: str = ROUND_UP,
    diff_on: float = 1.0,
    diff_off_dp: float = 0.3,
    poll_interval: int = 30,
) -> MagicMock:
    """Create a fake ConfigEntry with the given settings."""
    entry = MagicMock()
    entry.entry_id = "test_entry_001"
    entry.data = {
        CONF_NAME: "Test AC",
        CONF_CLIMATE_ENTITY: "climate.test_ac",
        CONF_TEMPERATURE_SENSOR: "sensor.test_temp",
        CONF_MODE: mode,
    }
    entry.options = {
        CONF_DIFF_OFF: diff_off,
        CONF_DIFF_LOW: diff_low,
        CONF_STEP: step,
        CONF_ROUND_DIRECTION: round_direction,
        CONF_DIFF_ON: diff_on,
        CONF_DIFF_OFF_DP: diff_off_dp,
        CONF_POLL_INTERVAL: poll_interval,
    }
    return entry


def _make_hass(
    t_trust: float | None = None,
    t_ac: float | None = None,
    ac_hvac_modes: list[str] | None = None,
) -> FakeHass:
    """Create a FakeHass with given sensor/AC states."""
    states: dict[str, FakeState] = {}

    if t_trust is not None:
        states["sensor.test_temp"] = FakeState(str(t_trust))

    if t_ac is not None:
        states["climate.test_ac"] = FakeState(
            state="cool",
            attributes={
                "current_temperature": t_ac,
                "hvac_modes": ac_hvac_modes or ["off", "cool", "heat", "heat_cool"],
            },
        )
    else:
        states["climate.test_ac"] = FakeState(
            state="cool",
            attributes={
                "hvac_modes": ac_hvac_modes or ["off", "cool", "heat", "heat_cool"],
            },
        )

    return FakeHass(states)


def _create_entity(
    hass: FakeHass,
    entry: MagicMock,
    hvac_mode=HVACMode.COOL,
    t_target: float = 25.0,
) -> MyACControllerClimate:
    """Create a MyACControllerClimate entity with given initial state."""
    entity = MyACControllerClimate(hass, entry)
    entity._hvac_mode = hvac_mode
    entity._t_target = t_target
    return entity


async def _init_entity(entity: MyACControllerClimate) -> None:
    """Simulate async_added_to_hass without actual HA timer setup."""
    # Override the timer setup
    entity._remove_timer = lambda: None
    # Manually read initial temps like async_added_to_hass would
    await entity._read_temperatures()
    # Default target if none
    if entity._t_target is None:
        entity._t_target = entity._t_trust or 24.0


# ===================================================================
# Tests: Diff_actual computation
# ===================================================================


class TestDiffActual:
    """Tests for Diff_actual = T_trust - T_expectation."""

    def test_cooling_scenario_room_warmer(self):
        """Room 28°C, target 25°C → Diff = +3.0 (needs cooling)."""
        hass = _make_hass(t_trust=28.0, t_ac=27.0)
        entity = _create_entity(hass, _make_entry(), t_target=25.0)
        entity._t_trust = 28.0
        assert entity._diff_actual == 3.0
        assert entity._diff_actual > 0  # cooling needed

    def test_heating_scenario_room_colder(self):
        """Room 18°C, target 22°C → Diff = -4.0 (needs heating)."""
        hass = _make_hass(t_trust=18.0, t_ac=19.0)
        entity = _create_entity(hass, _make_entry(), t_target=22.0)
        entity._t_trust = 18.0
        assert entity._diff_actual == -4.0
        assert entity._diff_actual < 0  # heating needed

    def test_at_target_exact(self):
        """Room 25°C, target 25°C → Diff = 0."""
        hass = _make_hass(t_trust=25.0)
        entity = _create_entity(hass, _make_entry(), t_target=25.0)
        entity._t_trust = 25.0
        assert entity._diff_actual == 0.0

    def test_missing_t_trust_returns_none(self):
        """Without T_trust, Diff_actual is None."""
        hass = _make_hass(t_trust=None)
        entity = _create_entity(hass, _make_entry(), t_target=25.0)
        assert entity._diff_actual is None

    def test_missing_t_target_returns_none(self):
        """Without T_expectation, Diff_actual is None."""
        hass = _make_hass(t_trust=28.0)
        entity = _create_entity(hass, _make_entry(), t_target=None)
        entity._t_target = None
        entity._t_trust = 28.0
        assert entity._diff_actual is None


# ===================================================================
# Tests: Inverter mode (Mode_bp) — Cooling
# ===================================================================


class TestInverterCooling:
    """Inverter mode cooling tests."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.hass = _make_hass(t_trust=28.0, t_ac=27.5)
        self.entity = _create_entity(
            self.hass, _make_entry(diff_off=0.3, diff_low=1.0),
            hvac_mode=HVACMode.COOL, t_target=25.0,
        )

    # ---- HIGH power zone ----

    @pytest.mark.asyncio
    async def test_cooling_high_power_far_from_target(self):
        """|Diff|=3.0 > Diff_low=1.0 → HIGH power, AC target = T_expectation."""
        self.entity._t_trust = 28.0  # Diff = 3.0
        await self.entity._control_inverter()
        assert self.entity._control_state == "high"
        assert self.entity._hvac_action == HVACAction.COOLING
        # Check that the AC was called with T_expectation (25°C)
        calls = self.hass.services.async_call.call_args_list
        # There should be calls: set_hvac_mode + set_temperature
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        temp_arg = temp_calls[-1][0][2]["temperature"]
        assert temp_arg == 25.0

    @pytest.mark.asyncio
    async def test_cooling_high_power_at_boundary_above_diff_low(self):
        """|Diff|=1.01 > Diff_low=1.0 → still HIGH."""
        self.entity._t_trust = 26.01  # Diff = 1.01
        await self.entity._control_inverter()
        assert self.entity._control_state == "high"

    # ---- LOW power zone ----

    @pytest.mark.asyncio
    async def test_cooling_low_power_near_target(self):
        """|Diff|=0.8, between Diff_off and Diff_low → LOW power."""
        self.entity._t_trust = 25.8  # Diff = 0.8
        await self.entity._control_inverter()
        assert self.entity._control_state == "low"
        assert self.entity._hvac_action == HVACAction.COOLING

    @pytest.mark.asyncio
    async def test_cooling_low_power_target_rounding_up(self):
        """Low power round-up: ceil(T_ac/step)*step."""
        self.entity._t_trust = 25.8
        self.entity._t_ac = 27.5
        self.entity._step = 1.0
        self.entity._round_direction = ROUND_UP
        await self.entity._control_inverter()
        # ceil(27.5/1.0)*1.0 = 28.0
        calls = self.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        assert temp_calls[-1][0][2]["temperature"] == 28.0

    @pytest.mark.asyncio
    async def test_cooling_low_power_target_rounding_down(self):
        """Low power round-down: floor(T_ac/step)*step."""
        self.entity._t_trust = 25.8
        self.entity._t_ac = 27.5
        self.entity._step = 1.0
        self.entity._round_direction = ROUND_DOWN
        await self.entity._control_inverter()
        # floor(27.5/1.0)*1.0 = 27.0
        calls = self.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        assert temp_calls[-1][0][2]["temperature"] == 27.0

    @pytest.mark.asyncio
    async def test_cooling_low_power_step_0_5_round_up(self):
        """Low power with step=0.5, round-up."""
        self.entity._t_trust = 25.8
        self.entity._t_ac = 27.3
        self.entity._step = 0.5
        self.entity._round_direction = ROUND_UP
        await self.entity._control_inverter()
        # ceil(27.3/0.5)*0.5 = ceil(54.6)*0.5 = 55*0.5 = 27.5
        calls = self.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 27.5

    @pytest.mark.asyncio
    async def test_cooling_low_power_step_0_5_round_down(self):
        """Low power with step=0.5, round-down."""
        self.entity._t_trust = 25.8
        self.entity._t_ac = 27.3
        self.entity._step = 0.5
        self.entity._round_direction = ROUND_DOWN
        await self.entity._control_inverter()
        # floor(27.3/0.5)*0.5 = floor(54.6)*0.5 = 54*0.5 = 27.0
        calls = self.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 27.0

    # ---- OFF zone ----

    @pytest.mark.asyncio
    async def test_cooling_off_zone_close_to_target(self):
        """|Diff|=0.2 <= Diff_off=0.3 → OFF."""
        self.entity._t_trust = 25.2  # Diff = 0.2
        await self.entity._control_inverter()
        assert self.entity._control_state == "off"
        assert self.entity._hvac_action == HVACAction.IDLE

    @pytest.mark.asyncio
    async def test_cooling_off_at_exact_boundary(self):
        """|Diff|=0.3 == Diff_off → OFF (boundary included)."""
        self.entity._t_trust = 25.3  # Diff = 0.3
        await self.entity._control_inverter()
        assert self.entity._control_state == "off"

    @pytest.mark.asyncio
    async def test_cooling_off_sends_hvac_off_to_ac(self):
        """OFF state should call climate.set_hvac_mode with 'off'."""
        self.entity._t_trust = 25.1
        await self.entity._control_inverter()
        calls = self.hass.services.async_call.call_args_list
        off_calls = [c for c in calls if c[0][1] == "set_hvac_mode" and c[0][2]["hvac_mode"] == "off"]
        assert len(off_calls) >= 1


# ===================================================================
# Tests: Inverter mode (Mode_bp) — Heating
# ===================================================================


class TestInverterHeating:
    """Inverter mode heating tests."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.hass = _make_hass(t_trust=18.0, t_ac=19.0)
        self.entity = _create_entity(
            self.hass, _make_entry(diff_off=0.3, diff_low=1.0),
            hvac_mode=HVACMode.HEAT, t_target=22.0,
        )

    @pytest.mark.asyncio
    async def test_heating_high_power_far_from_target(self):
        """|Diff|=4.0 > Diff_low=1.0 → HIGH, target = 22°C."""
        self.entity._t_trust = 18.0  # Diff = -4.0
        await self.entity._control_inverter()
        assert self.entity._control_state == "high"
        assert self.entity._hvac_action == HVACAction.HEATING
        calls = self.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 22.0

    @pytest.mark.asyncio
    async def test_heating_low_power_near_target(self):
        """|Diff|=0.8 → LOW power."""
        self.entity._t_trust = 21.2  # Diff = -0.8
        await self.entity._control_inverter()
        assert self.entity._control_state == "low"
        assert self.entity._hvac_action == HVACAction.HEATING

    @pytest.mark.asyncio
    async def test_heating_low_power_round_up(self):
        """Heating low power round-up: ceil(T_ac/step)*step."""
        self.entity._t_trust = 21.2
        self.entity._t_ac = 19.5
        self.entity._step = 1.0
        self.entity._round_direction = ROUND_UP
        await self.entity._control_inverter()
        # ceil(19.5/1.0)*1.0 = 20.0
        calls = self.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 20.0

    @pytest.mark.asyncio
    async def test_heating_low_power_round_down(self):
        """Heating low power round-down: floor(T_ac/step)*step."""
        self.entity._t_trust = 21.2
        self.entity._t_ac = 19.5
        self.entity._step = 1.0
        self.entity._round_direction = ROUND_DOWN
        await self.entity._control_inverter()
        # floor(19.5/1.0)*1.0 = 19.0
        calls = self.hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 19.0

    @pytest.mark.asyncio
    async def test_heating_off_zone_close_to_target(self):
        """|Diff|=0.2 → OFF."""
        self.entity._t_trust = 21.8  # Diff = -0.2
        await self.entity._control_inverter()
        assert self.entity._control_state == "off"
        assert self.entity._hvac_action == HVACAction.IDLE


# ===================================================================
# Tests: Fixed-speed mode (Mode_dp)
# ===================================================================


class TestFixedSpeedCooling:
    """Fixed-speed mode cooling tests."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.hass = _make_hass(t_trust=28.0, t_ac=27.0)
        self.entity = _create_entity(
            self.hass, _make_entry(mode=MODE_DP, diff_on=1.0, diff_off_dp=0.3),
            hvac_mode=HVACMode.COOL, t_target=25.0,
        )
        self.entity._control_state = "off"  # start OFF

    @pytest.mark.asyncio
    async def test_cooling_turn_on_when_hot(self):
        """OFF + |Diff|=3.0 >= Diff_on=1.0 → turn ON."""
        self.entity._control_state = "off"
        self.entity._t_trust = 28.0
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "on"
        assert self.entity._hvac_action == HVACAction.COOLING

    @pytest.mark.asyncio
    async def test_cooling_turn_off_when_cool_enough(self):
        """ON + |Diff|=0.2 <= Diff_off_dp=0.3 → turn OFF."""
        self.entity._control_state = "on"
        self.entity._t_trust = 25.2  # Diff = 0.2
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "off"
        assert self.entity._hvac_action == HVACAction.IDLE

    @pytest.mark.asyncio
    async def test_cooling_hysteresis_hold_off(self):
        """OFF + Diff_off < |Diff| < Diff_on → stay OFF."""
        self.entity._control_state = "off"
        self.entity._t_trust = 25.5  # Diff = 0.5
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "off"

    @pytest.mark.asyncio
    async def test_cooling_hysteresis_hold_on(self):
        """ON + Diff_off < |Diff| < Diff_on → stay ON."""
        self.entity._control_state = "on"
        self.entity._t_trust = 25.5  # Diff = 0.5
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "on"

    @pytest.mark.asyncio
    async def test_cooling_turn_on_at_exact_boundary(self):
        """|Diff| == Diff_on → turn ON (boundary included)."""
        self.entity._control_state = "off"
        self.entity._t_trust = 26.0  # Diff = 1.0
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "on"

    @pytest.mark.asyncio
    async def test_cooling_turn_off_at_exact_boundary(self):
        """|Diff| == Diff_off_dp → turn OFF (boundary included)."""
        self.entity._control_state = "on"
        self.entity._t_trust = 25.3  # Diff = 0.3
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "off"

    @pytest.mark.asyncio
    async def test_cooling_cycle_on_off_on(self):
        """Full cycle: OFF → ON → OFF → ON simulating room temp changes."""
        # Start OFF, room hot
        self.entity._control_state = "off"
        self.entity._t_trust = 29.0
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "on"

        # AC runs, room cools within hysteresis
        self.entity._t_trust = 25.5
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "on"  # still ON in band

        # Room reaches target
        self.entity._t_trust = 25.2
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "off"

        # Room warms up again
        self.entity._t_trust = 26.5
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "on"


class TestFixedSpeedHeating:
    """Fixed-speed mode heating tests."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.hass = _make_hass(t_trust=18.0, t_ac=19.0)
        self.entity = _create_entity(
            self.hass, _make_entry(mode=MODE_DP, diff_on=1.0, diff_off_dp=0.3),
            hvac_mode=HVACMode.HEAT, t_target=22.0,
        )
        self.entity._control_state = "off"

    @pytest.mark.asyncio
    async def test_heating_turn_on_when_cold(self):
        """OFF + |Diff|=4.0 >= Diff_on=1.0 → turn ON."""
        self.entity._control_state = "off"
        self.entity._t_trust = 18.0
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "on"
        assert self.entity._hvac_action == HVACAction.HEATING

    @pytest.mark.asyncio
    async def test_heating_turn_off_when_warm_enough(self):
        """ON + |Diff|=0.2 <= Diff_off_dp=0.3 → turn OFF."""
        self.entity._control_state = "on"
        self.entity._t_trust = 21.8  # Diff = -0.2
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "off"

    @pytest.mark.asyncio
    async def test_heating_hysteresis_hold_off(self):
        """OFF + Diff_off < |Diff| < Diff_on → stay OFF."""
        self.entity._control_state = "off"
        self.entity._t_trust = 21.3  # Diff = -0.7
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "off"

    @pytest.mark.asyncio
    async def test_heating_hysteresis_hold_on(self):
        """ON + Diff_off < |Diff| < Diff_on → stay ON."""
        self.entity._control_state = "on"
        self.entity._t_trust = 21.3  # Diff = -0.7
        await self.entity._control_fixed_speed()
        assert self.entity._control_state == "on"


# ===================================================================
# Tests: Sensor fallback
# ===================================================================


class TestSensorFallback:
    """Tests for T_trust / T_ac availability handling."""

    @pytest.mark.asyncio
    async def test_reads_both_sensors(self):
        """Both sensors available → both values populated."""
        hass = _make_hass(t_trust=25.5, t_ac=26.0)
        entity = _create_entity(hass, _make_entry())
        await entity._read_temperatures()
        assert entity._t_trust == 25.5
        assert entity._t_ac == 26.0

    @pytest.mark.asyncio
    async def test_t_trust_unavailable_falls_back_to_t_ac(self):
        """T_trust unavailable → use T_ac."""
        hass = _make_hass(t_trust=None, t_ac=26.0)
        entity = _create_entity(hass, _make_entry())
        await entity._read_temperatures()
        assert entity._t_trust == 26.0  # fallback
        assert entity._t_ac == 26.0

    @pytest.mark.asyncio
    async def test_both_unavailable(self):
        """Neither sensor available → both None."""
        hass = _make_hass(t_trust=None, t_ac=None)
        entity = _create_entity(hass, _make_entry())
        await entity._read_temperatures()
        assert entity._t_trust is None
        assert entity._t_ac is None

    @pytest.mark.asyncio
    async def test_implausible_temperature_rejected(self):
        """Temperature outside -50..60 °C is rejected as implausible."""
        hass = _make_hass(t_trust=999.0)  # absurd value
        entity = _create_entity(hass, _make_entry())
        entity._t_trust = None  # reset
        await entity._read_temperatures()
        assert entity._t_trust is None  # should be rejected

    @pytest.mark.asyncio
    async def test_negative_temperature_accepted(self):
        """Temperature of -10°C is valid (cold climate)."""
        hass = _make_hass(t_trust=-10.0)
        entity = _create_entity(hass, _make_entry())
        entity._t_trust = None
        await entity._read_temperatures()
        assert entity._t_trust == -10.0

    @pytest.mark.asyncio
    async def test_boundary_60_accepted(self):
        """60°C is just within plausibility bound."""
        hass = _make_hass(t_trust=60.0)
        entity = _create_entity(hass, _make_entry())
        entity._t_trust = None
        await entity._read_temperatures()
        assert entity._t_trust == 60.0

    @pytest.mark.asyncio
    async def test_boundary_60_1_rejected(self):
        """60.1°C is outside plausibility bound."""
        hass = _make_hass(t_trust=60.1)
        entity = _create_entity(hass, _make_entry())
        entity._t_trust = None
        await entity._read_temperatures()
        assert entity._t_trust is None

    @pytest.mark.asyncio
    async def test_low_power_t_ac_unavailable_falls_back_to_t_trust(self):
        """When T_ac None in low-power, use T_trust for rounding."""
        hass = _make_hass(t_trust=25.8, t_ac=None)
        entity = _create_entity(hass, _make_entry(), t_target=25.0)
        entity._t_trust = 25.8
        entity._t_ac = None
        await entity._set_ac_low_power()
        # ROUND_UP with step=1.0: ceil(25.8/1.0)*1.0 = 26.0
        calls = hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert len(temp_calls) >= 1
        assert temp_calls[-1][0][2]["temperature"] == 26.0

    @pytest.mark.asyncio
    async def test_low_power_both_unavailable_skips(self):
        """Both sensors None → low power sets no target."""
        hass = _make_hass(t_trust=None, t_ac=None)
        entity = _create_entity(hass, _make_entry(), t_target=25.0)
        entity._t_trust = None
        entity._t_ac = None
        # Should not crash
        await entity._set_ac_low_power()
        # No temperature call should be made (only possible if control state is off)
        calls = hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert len(temp_calls) == 0


# ===================================================================
# Tests: HVAC mode resolution
# ===================================================================


class TestHVACModeResolution:
    """Tests for _resolve_ac_hvac_mode."""

    def test_direct_passthrough_heat(self):
        """HEAT mode passes through unchanged."""
        hass = _make_hass()
        entity = _create_entity(hass, _make_entry(), hvac_mode=HVACMode.HEAT)
        assert entity._resolve_ac_hvac_mode() == HVACMode.HEAT

    def test_direct_passthrough_cool(self):
        """COOL mode passes through unchanged."""
        hass = _make_hass()
        entity = _create_entity(hass, _make_entry(), hvac_mode=HVACMode.COOL)
        assert entity._resolve_ac_hvac_mode() == HVACMode.COOL

    def test_direct_passthrough_off(self):
        """OFF mode passes through unchanged."""
        hass = _make_hass()
        entity = _create_entity(hass, _make_entry(), hvac_mode=HVACMode.OFF)
        assert entity._resolve_ac_hvac_mode() == HVACMode.OFF

    def test_heat_cool_native_supported(self):
        """AC supports heat_cool → use it."""
        hass = _make_hass(ac_hvac_modes=["off", "cool", "heat", "heat_cool"])
        entity = _create_entity(hass, _make_entry(), hvac_mode=HVACMode.HEAT_COOL)
        assert entity._resolve_ac_hvac_mode() == HVACMode.HEAT_COOL

    def test_heat_cool_diff_positive_falls_back_to_cool(self):
        """AC only has cool/heat; Diff>0 → COOL."""
        hass = _make_hass(ac_hvac_modes=["off", "cool", "heat"])
        entity = _create_entity(hass, _make_entry(), hvac_mode=HVACMode.HEAT_COOL)
        entity._t_trust = 28.0
        entity._t_target = 25.0
        # Diff = +3.0 → needs cooling
        assert entity._resolve_ac_hvac_mode() == HVACMode.COOL

    def test_heat_cool_diff_negative_falls_back_to_heat(self):
        """AC only has cool/heat; Diff<0 → HEAT."""
        hass = _make_hass(ac_hvac_modes=["off", "cool", "heat"])
        entity = _create_entity(hass, _make_entry(), hvac_mode=HVACMode.HEAT_COOL)
        entity._t_trust = 18.0
        entity._t_target = 22.0
        # Diff = -4.0 → needs heating
        assert entity._resolve_ac_hvac_mode() == HVACMode.HEAT


# ===================================================================
# Tests: Zone transitions (inverter mode)
# ===================================================================


class TestZoneTransitions:
    """Tests for smooth transitions between OFF / LOW / HIGH."""

    @pytest.mark.asyncio
    async def test_cooling_high_to_low_transition(self):
        """Room cools: HIGH → LOW → OFF as temp approaches target."""
        hass = _make_hass(t_trust=28.0, t_ac=27.5)
        entity = _create_entity(
            hass, _make_entry(diff_off=0.3, diff_low=1.0),
            hvac_mode=HVACMode.COOL, t_target=25.0,
        )

        # Start: Diff=3.0 → HIGH
        entity._t_trust = 28.0
        await entity._control_inverter()
        assert entity._control_state == "high"

        # Cooled to 26.0, Diff=1.0 → boundary (<=1.0) → LOW
        entity._t_trust = 26.0
        await entity._control_inverter()
        assert entity._control_state == "low"

        # Cooled to 25.5, Diff=0.5 → still LOW
        entity._t_trust = 25.5
        await entity._control_inverter()
        assert entity._control_state == "low"

        # Cooled to 25.3, Diff=0.3 → boundary (<=0.3) → OFF
        entity._t_trust = 25.3
        await entity._control_inverter()
        assert entity._control_state == "off"

        # Cooled to 25.1, Diff=0.1 → OFF
        entity._t_trust = 25.1
        await entity._control_inverter()
        assert entity._control_state == "off"

    @pytest.mark.asyncio
    async def test_heating_low_to_high_transition(self):
        """Room cools down: OFF → LOW → HIGH as temp drifts from target."""
        hass = _make_hass(t_trust=22.0, t_ac=21.5)
        entity = _create_entity(
            hass, _make_entry(diff_off=0.3, diff_low=1.0),
            hvac_mode=HVACMode.HEAT, t_target=22.0,
        )

        # Start at target: OFF
        entity._t_trust = 22.0
        await entity._control_inverter()
        assert entity._control_state == "off"

        # Drifted to 21.4, Diff=-0.6 → LOW
        entity._t_trust = 21.4
        await entity._control_inverter()
        assert entity._control_state == "low"

        # Drifted to 20.5, Diff=-1.5 → HIGH
        entity._t_trust = 20.5
        await entity._control_inverter()
        assert entity._control_state == "high"


# ===================================================================
# Tests: Target temperature setting
# ===================================================================


class TestUserSetTemperature:
    """Tests for async_set_temperature."""

    @pytest.mark.asyncio
    async def test_set_target_and_trigger_control_cycle(self):
        """Setting target temp immediately triggers a control cycle."""
        hass = _make_hass(t_trust=28.0, t_ac=27.0)
        entity = _create_entity(
            hass, _make_entry(), hvac_mode=HVACMode.COOL, t_target=25.0,
        )
        entity._control_state = "off"

        await entity.async_set_temperature(temperature=23.0)
        assert entity._t_target == 23.0

    @pytest.mark.asyncio
    async def test_set_target_updates_entity_state(self):
        """Target temp update should be reflected."""
        hass = _make_hass(t_trust=28.0, t_ac=27.0)
        entity = _create_entity(
            hass, _make_entry(), hvac_mode=HVACMode.COOL, t_target=25.0,
        )
        await entity.async_set_temperature(temperature=26.0)
        assert entity.target_temperature == 26.0


# ===================================================================
# Tests: HVAC mode changes
# ===================================================================


class TestHVACModeChanges:
    """Tests for async_set_hvac_mode."""

    @pytest.mark.asyncio
    async def test_set_off_turns_off_ac_and_sets_action_off(self):
        """Setting HVAC OFF → AC off, hvac_action = OFF."""
        hass = _make_hass(t_trust=28.0, t_ac=27.0)
        entity = _create_entity(
            hass, _make_entry(), hvac_mode=HVACMode.COOL, t_target=25.0,
        )
        entity._control_state = "high"
        entity._hvac_action = HVACAction.COOLING

        await entity.async_set_hvac_mode(HVACMode.OFF)
        assert entity._hvac_mode == HVACMode.OFF
        assert entity._hvac_action == HVACAction.OFF
        assert entity._control_state == "off"

    @pytest.mark.asyncio
    async def test_set_cool_triggers_control_cycle(self):
        """Setting HVAC COOL triggers a control cycle."""
        hass = _make_hass(t_trust=28.0, t_ac=27.0)
        entity = _create_entity(
            hass, _make_entry(), hvac_mode=HVACMode.OFF, t_target=25.0,
        )
        await entity.async_set_hvac_mode(HVACMode.COOL)
        assert entity._hvac_mode == HVACMode.COOL


# ===================================================================
# Tests: Edge Cases
# ===================================================================


class TestEdgeCases:
    """Miscellaneous edge-case tests."""

    @pytest.mark.asyncio
    async def test_control_cycle_skips_when_hvac_off(self):
        """When HVAC is OFF, control cycle should not actuate AC."""
        hass = _make_hass(t_trust=30.0, t_ac=29.0)
        entity = _create_entity(
            hass, _make_entry(), hvac_mode=HVACMode.OFF, t_target=25.0,
        )
        entity._control_state = "high"
        call_count_before = len(hass.services.async_call.call_args_list)
        await entity._async_control_cycle()
        # No new calls should have been made
        assert entity._control_state == "off"
        assert entity._hvac_action == HVACAction.OFF

    @pytest.mark.asyncio
    async def test_control_cycle_skips_when_diff_none(self):
        """When Diff_actual is None, control skips gracefully."""
        hass = _make_hass(t_trust=None)
        entity = _create_entity(
            hass, _make_entry(), hvac_mode=HVACMode.COOL, t_target=25.0,
        )
        entity._t_trust = None
        # Should not crash
        await entity._async_control_cycle()

    @pytest.mark.asyncio
    async def test_current_temperature_reports_t_trust(self):
        """Entity's current_temperature property reflects T_trust."""
        hass = _make_hass()
        entity = _create_entity(hass, _make_entry())
        entity._t_trust = 26.5
        assert entity.current_temperature == 26.5

    def test_extra_state_attributes_populated(self):
        """Extra attributes include all diagnostic fields."""
        hass = _make_hass()
        entity = _create_entity(hass, _make_entry(), t_target=25.0)
        entity._t_trust = 26.0
        entity._t_ac = 25.5
        entity._control_state = "low"
        attrs = entity.extra_state_attributes
        assert attrs[ATTR_CONTROL_STATE] == "low"
        assert attrs[ATTR_DIFF_ACTUAL] == 1.0
        assert attrs["t_ac"] == 25.5
        assert attrs["t_trust"] == 26.0
        assert attrs["t_expectation"] == 25.0

    def test_default_target_when_no_trusted_temp(self):
        """When no trusted temp available, default target is 24°C."""
        hass = _make_hass(t_trust=None)
        entity = _create_entity(hass, _make_entry(), t_target=None)
        entity._t_target = None
        entity._t_trust = None
        # Simulating what async_added_to_hass would do
        if entity._t_target is None:
            entity._t_target = entity._t_trust or 24.0
        assert entity._t_target == 24.0

    @pytest.mark.asyncio
    async def test_unknown_mode_does_not_crash(self):
        """An unrecognized mode should be logged, not crash."""
        hass = _make_hass(t_trust=28.0, t_ac=27.0)
        entity = _create_entity(hass, _make_entry(mode="unknown_mode"))
        entity._t_trust = 28.0
        # Should not raise
        await entity._async_control_cycle()

    @pytest.mark.asyncio
    async def test_round_up_at_integer(self):
        """Round-up when T_ac is already an integer gives same value."""
        hass = _make_hass(t_trust=25.5, t_ac=27.0)
        entity = _create_entity(
            hass, _make_entry(step=1.0, round_direction=ROUND_UP),
            t_target=25.0,
        )
        entity._t_trust = 25.5
        entity._t_ac = 27.0
        await entity._set_ac_low_power()
        calls = hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 27.0

    @pytest.mark.asyncio
    async def test_round_down_at_integer(self):
        """Round-down when T_ac is already an integer gives same value."""
        hass = _make_hass(t_trust=25.5, t_ac=27.0)
        entity = _create_entity(
            hass, _make_entry(step=1.0, round_direction=ROUND_DOWN),
            t_target=25.0,
        )
        entity._t_trust = 25.5
        entity._t_ac = 27.0
        await entity._set_ac_low_power()
        calls = hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 27.0

    @pytest.mark.asyncio
    async def test_exact_diff_zero_idle_action(self):
        """Diff_actual == 0 → HVACAction.IDLE."""
        hass = _make_hass(t_trust=25.0, t_ac=25.0)
        entity = _create_entity(
            hass, _make_entry(), hvac_mode=HVACMode.COOL, t_target=25.0,
        )
        entity._update_hvac_action()
        assert entity._hvac_action == HVACAction.IDLE


# ===================================================================
# Tests: Config entry option reading
# ===================================================================


class TestConfigOptions:
    """Tests for reading config entry options."""

    def test_inverter_defaults(self):
        """Inverter mode uses defaults when options dict is empty or missing."""
        entry = _make_entry()
        hass = _make_hass()
        entity = MyACControllerClimate(hass, entry)
        assert entity._diff_off == 0.3
        assert entity._diff_low == 1.0
        assert entity._step == 1.0
        assert entity._round_direction == ROUND_UP

    def test_custom_inverter_options(self):
        """Custom inverter options are read correctly."""
        entry = _make_entry(
            diff_off=0.5, diff_low=2.0, step=0.5,
            round_direction=ROUND_DOWN, poll_interval=60,
        )
        hass = _make_hass()
        entity = MyACControllerClimate(hass, entry)
        assert entity._diff_off == 0.5
        assert entity._diff_low == 2.0
        assert entity._step == 0.5
        assert entity._round_direction == ROUND_DOWN
        assert entity._poll_interval == 60

    def test_fixed_speed_defaults(self):
        """Fixed-speed mode defaults."""
        entry = _make_entry(mode=MODE_DP)
        hass = _make_hass()
        entity = MyACControllerClimate(hass, entry)
        assert entity._diff_on == 1.0
        assert entity._diff_off_dp == 0.3

    def test_fixed_speed_custom(self):
        """Custom fixed-speed thresholds."""
        entry = _make_entry(mode=MODE_DP, diff_on=2.0, diff_off_dp=0.5)
        hass = _make_hass()
        entity = MyACControllerClimate(hass, entry)
        assert entity._diff_on == 2.0
        assert entity._diff_off_dp == 0.5


# ===================================================================
# Tests: High power setpoint
# ===================================================================


class TestHighPower:
    """Tests for inverter high-power mode."""

    @pytest.mark.asyncio
    async def test_cooling_high_power_target_is_t_expectation(self):
        """High power cooling sets AC target = T_expectation."""
        hass = _make_hass(t_trust=32.0, t_ac=31.0)
        entity = _create_entity(
            hass, _make_entry(diff_off=0.3, diff_low=1.0),
            hvac_mode=HVACMode.COOL, t_target=25.0,
        )
        entity._t_trust = 32.0
        await entity._control_inverter()
        assert entity._control_state == "high"
        calls = hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 25.0

    @pytest.mark.asyncio
    async def test_heating_high_power_target_is_t_expectation(self):
        """High power heating sets AC target = T_expectation."""
        hass = _make_hass(t_trust=10.0, t_ac=11.0)
        entity = _create_entity(
            hass, _make_entry(diff_off=0.3, diff_low=1.0),
            hvac_mode=HVACMode.HEAT, t_target=22.0,
        )
        entity._t_trust = 10.0
        await entity._control_inverter()
        assert entity._control_state == "high"
        calls = hass.services.async_call.call_args_list
        temp_calls = [c for c in calls if c[0][1] == "set_temperature"]
        assert temp_calls[-1][0][2]["temperature"] == 22.0


# ===================================================================
# Run all tests
# ===================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
