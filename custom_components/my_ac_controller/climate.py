"""Climate entity for My AC Controller.

Controls an air conditioner using an external (trusted) temperature sensor
to achieve stable room temperature, compensating for inaccuracies in the
AC's built-in sensor due to installation position.

Supports two modes:
  - Mode_bp (inverter / variable-frequency): modulates AC power level
    based on how far the room is from the desired temperature.
  - Mode_dp (fixed-speed / on-off): standard hysteresis on/off control.

Naming convention (matching the specification):
  T_expectation  — user-set desired temperature
  T_trust        — external (trusted) sensor temperature
  T_ac           — AC built-in sensor temperature
  Diff_actual    — T_trust - T_expectation
"""

from __future__ import annotations

from datetime import timedelta
import logging
from math import ceil, floor
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import ATTR_HVAC_MODE
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    ATTR_CONTROL_STATE,
    ATTR_DIFF_ACTUAL,
    ATTR_T_AC,
    ATTR_T_EXPECTATION,
    ATTR_T_TRUST,
    CONF_CLIMATE_ENTITY,
    CONF_DIFF_LOW,
    CONF_DIFF_OFF,
    CONF_DIFF_OFF_DP,
    CONF_DIFF_ON,
    CONF_MODE,
    CONF_NAME,
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
    MODE_BP,
    MODE_DP,
    ROUND_DOWN,
    ROUND_UP,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the My AC Controller climate entity from a config entry."""
    entity = MyACControllerClimate(hass, entry)
    async_add_entities([entity])
    # Store climate entity reference so other platforms (sensor, number, etc.)
    # can access it via hass.data[DOMAIN][entry.entry_id]["climate"]
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})["climate"] = entity


class MyACControllerClimate(ClimateEntity, RestoreEntity):
    """Climate entity that controls an AC via a trusted external temperature sensor.

    The entity reads T_trust from an external sensor (e.g. a wall thermometer)
    and T_ac from the AC's own sensor. Using T_trust as the authoritative room
    temperature, it decides whether to run the AC at off / low / high power
    (inverter mode) or simply on / off (fixed-speed mode), then sends the
    appropriate target temperature and HVAC mode to the underlying AC entity.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.HEAT_COOL]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_target_temperature_step = 0.5

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the climate entity."""
        super().__init__()
        self.hass = hass
        self._entry = entry

        # --- Config data (structural — set at creation, rarely changed) ---
        data = entry.data
        self._name: str = data[CONF_NAME]
        self._climate_entity: str = data[CONF_CLIMATE_ENTITY]
        self._temp_sensor: str = data[CONF_TEMPERATURE_SENSOR]
        self._mode: str = data[CONF_MODE]  # MODE_BP or MODE_DP

        # --- Options (tunable) ---
        # Options are stored in entry.options; fall back to defaults.
        opts = entry.options
        self._diff_off: float = float(
            opts.get(CONF_DIFF_OFF, DEFAULT_DIFF_OFF)
        )
        self._diff_low: float = float(
            opts.get(CONF_DIFF_LOW, DEFAULT_DIFF_LOW)
        )
        self._step: float = float(
            opts.get(CONF_STEP, DEFAULT_STEP)
        )
        self._round_direction: str = str(
            opts.get(CONF_ROUND_DIRECTION, DEFAULT_ROUND_DIRECTION)
        )
        self._diff_on: float = float(
            opts.get(CONF_DIFF_ON, DEFAULT_DIFF_ON)
        )
        self._diff_off_dp: float = float(
            opts.get(CONF_DIFF_OFF_DP, DEFAULT_DIFF_OFF_DP)
        )
        self._poll_interval: int = int(
            opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )

        # --- Runtime state ---
        self._t_target: float | None = None   # T_expectation
        self._t_trust: float | None = None    # trusted sensor reading
        self._t_ac: float | None = None       # AC internal sensor reading
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._hvac_action: HVACAction = HVACAction.OFF
        self._control_state: str = "off"      # fan / low / high / off / on

        # --- Entity metadata ---
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}"
        self._attr_name = self._name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": self._name,
            "manufacturer": "My AC Controller",
            "model": "AC Controller",
        }

        self._remove_timer: callable | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass — start the periodic control timer."""
        await super().async_added_to_hass()

        # Read initial temperatures from HA state
        await self._read_temperatures()

        # Restore persisted state (target temp, hvac mode) from last run
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            try:
                self._hvac_mode = HVACMode(last_state.state)
            except ValueError:
                self._hvac_mode = HVACMode.OFF
            restored_temp = last_state.attributes.get(ATTR_TEMPERATURE)
            if restored_temp is not None:
                self._t_target = float(restored_temp)

        # Fallback: if target still unset, default to current room temp or 24 °C
        if self._t_target is None:
            if self._t_trust is not None:
                self._t_target = self._t_trust
            else:
                self._t_target = 24.0

        # Start periodic control timer
        self._remove_timer = async_track_time_interval(
            self.hass,
            self._async_control_cycle,
            timedelta(seconds=self._poll_interval),
        )

        _LOGGER.info(
            "%s: Started — mode=%s, poll=%ss, diff_off=%s, diff_low=%s, "
            "step=%s, round=%s, T_target=%.1f",
            self._name,
            self._mode,
            self._poll_interval,
            self._diff_off,
            self._diff_low,
            self._step,
            self._round_direction,
            self._t_target,
        )

    async def async_will_remove_from_hass(self) -> None:
        """When entity is removed — clean up timer."""
        if self._remove_timer:
            self._remove_timer()
            self._remove_timer = None
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------------
    # Properties (Home Assistant interface)
    # ------------------------------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        """Report T_trust as the current room temperature."""
        return self._t_trust

    @property
    def target_temperature(self) -> float | None:
        """Report T_expectation as the target temperature."""
        return self._t_target

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return current HVAC action (heating/cooling/idle/off)."""
        return self._hvac_action

    @property
    def min_temp(self) -> float:
        """Minimum settable temperature."""
        return 16.0

    @property
    def max_temp(self) -> float:
        """Maximum settable temperature."""
        return 32.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Extra attributes for UI visibility and diagnostics."""
        diff = self._diff_actual
        return {
            ATTR_CONTROL_STATE: self._control_state,
            ATTR_DIFF_ACTUAL: round(diff, 2) if diff is not None else None,
            ATTR_T_AC: self._t_ac,
            ATTR_T_TRUST: self._t_trust,
            ATTR_T_EXPECTATION: self._t_target,
        }

    # ------------------------------------------------------------------
    # Diff_actual  (core formula)
    # ------------------------------------------------------------------

    @property
    def _diff_actual(self) -> float | None:
        """Diff_actual = T_trust - T_expectation.

        Positive  → room is warmer than desired (needs cooling).
        Negative  → room is colder than desired (needs heating).
        """
        if self._t_trust is None or self._t_target is None:
            return None
        return self._t_trust - self._t_target

    # ------------------------------------------------------------------
    # User actions (called by HA when user interacts with the climate card)
    # ------------------------------------------------------------------

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Handle user setting a new target temperature (T_expectation).

        The HA climate card may also include hvac_mode in the same call
        when the user switches between cooling and heating modes.
        """
        _LOGGER.debug(
            "%s: async_set_temperature kwargs=%s, current hvac_mode=%s",
            self._name, kwargs, self._hvac_mode,
        )

        # 1. Handle HVAC mode change if bundled with temperature
        if ATTR_HVAC_MODE in kwargs:
            hvac_mode_val = kwargs[ATTR_HVAC_MODE]
            if hvac_mode_val in (HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL):
                self._hvac_mode = HVACMode(hvac_mode_val)
                _LOGGER.info(
                    "%s: HVAC mode → %s (via set_temperature)", self._name, hvac_mode_val
                )
            elif hvac_mode_val == HVACMode.OFF:
                self._hvac_mode = HVACMode.OFF
                await self._async_turn_off_ac()
                self._hvac_action = HVACAction.OFF
                self._control_state = "off"
                await self.async_update_ha_state()
                return

        # 2. Handle target temperature change
        if ATTR_TEMPERATURE in kwargs:
            new_target = float(kwargs[ATTR_TEMPERATURE])
            # Clamp to valid range
            new_target = min(self.max_temp, max(self.min_temp, new_target))
            self._t_target = new_target
            _LOGGER.info(
                "%s: T_expectation set to %.1f °C", self._name, self._t_target
            )

        # 3. React immediately rather than waiting for the next poll
        await self._async_control_cycle(now=None)
        await self.async_update_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Handle user changing the HVAC mode.

        Accepts HEAT, COOL, HEAT_COOL and OFF. HEAT_COOL enables automatic
        heating/cooling selection based on the sign of Diff_actual.
        """
        _LOGGER.info(
            "%s: async_set_hvac_mode → %s (was %s)",
            self._name, hvac_mode, self._hvac_mode,
        )

        if hvac_mode in (HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL):
            self._hvac_mode = hvac_mode
            self.async_write_ha_state()
            await self._async_control_cycle(now=None)

        elif hvac_mode == HVACMode.OFF:
            self._hvac_mode = HVACMode.OFF
            self.async_write_ha_state()
            await self._async_turn_off_ac()
            self._hvac_action = HVACAction.OFF
            self._control_state = "off"

        else:
            _LOGGER.error(
                "%s: Unsupported hvac_mode %s", self._name, hvac_mode
            )
            return

        await self.async_update_ha_state()

    # ------------------------------------------------------------------
    # Temperature reading
    # ------------------------------------------------------------------

    async def _read_temperatures(self) -> None:
        """Read T_trust and T_ac from Home Assistant state machine."""

        # --- T_trust: external (trusted) sensor ---
        sensor_state = self.hass.states.get(self._temp_sensor)
        if sensor_state and sensor_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            try:
                temp = float(sensor_state.state)
                if -50.0 <= temp <= 60.0:          # plausibility bounds
                    self._t_trust = temp
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "%s: Cannot parse T_trust from %s",
                    self._name,
                    self._temp_sensor,
                )

        # --- T_ac: AC built-in sensor ---
        ac_state = self.hass.states.get(self._climate_entity)
        if ac_state and ac_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            ac_temp = ac_state.attributes.get("current_temperature")
            if ac_temp is not None:
                try:
                    temp = float(ac_temp)
                    if -50.0 <= temp <= 60.0:
                        self._t_ac = temp
                except (ValueError, TypeError):
                    _LOGGER.warning(
                        "%s: Cannot parse T_ac from %s",
                        self._name,
                        self._climate_entity,
                    )

        # --- Fallback: if T_trust is unavailable, degrade to T_ac ---
        if self._t_trust is None and self._t_ac is not None:
            self._t_trust = self._t_ac
            _LOGGER.debug(
                "%s: T_trust unavailable → fallback to T_ac = %.1f",
                self._name,
                self._t_ac,
            )

        if self._t_trust is None:
            _LOGGER.warning(
                "%s: Both T_trust and T_ac unavailable — control skipped",
                self._name,
            )

    # ------------------------------------------------------------------
    # Main control cycle
    # ------------------------------------------------------------------

    async def _async_control_cycle(self, now=None) -> None:
        """Run one control cycle.

        Called periodically (by timer) and also on user interaction.
        Reads temperatures, decides the control state, and actuates the AC.
        """
        await self._read_temperatures()

        if self._hvac_mode == HVACMode.OFF:
            self._hvac_action = HVACAction.OFF
            self._control_state = "off"
            _LOGGER.info(
                "%s: ▶ Control cycle — HVAC=OFF, skipping", self._name
            )
            await self.async_update_ha_state()
            return

        if self._diff_actual is None:
            _LOGGER.info(
                "%s: ▶ Control cycle — Diff_actual=None (T_trust=%s, T_target=%s), skipping",
                self._name, self._t_trust, self._t_target,
            )
            await self.async_update_ha_state()
            return

        _LOGGER.info(
            "%s: ▶ Control cycle — mode=%s, T_trust=%.1f, T_ac=%s, "
            "T_target=%.1f, Diff_actual=%.2f, hvac_mode=%s",
            self._name, self._mode,
            self._t_trust if self._t_trust is not None else float('nan'),
            f"{self._t_ac:.1f}" if self._t_ac is not None else "N/A",
            self._t_target,
            self._diff_actual,
            self._hvac_mode,
        )

        if self._mode == MODE_BP:
            await self._control_inverter()
        elif self._mode == MODE_DP:
            await self._control_fixed_speed()
        else:
            _LOGGER.error("%s: Unknown mode '%s'", self._name, self._mode)

        await self.async_update_ha_state()

    # ==================================================================
    # Inverter mode  (Mode_bp)
    # ==================================================================

    async def _control_inverter(self) -> None:
        """Inverter (variable-frequency) AC control logic.

        Zone map (based on |Diff_actual|):

            0 ──── Diff_off ──── Diff_low ──── ∞
            FAN      LOW           HIGH

        - FAN:  temperature is close enough to target; switch to
                fan-only mode (compressor stops, air still circulates)
                to prevent overshooting without fully powering off.
        - LOW:  temperature is near target; run the AC at minimum
                inverter frequency for gentle fine-tuning.
        - HIGH: temperature is far from target; run at full capacity.
        """
        # Round to 2 decimal places to avoid floating-point artifacts
        # (e.g. 25.3 - 25.0 = 0.3000000000000007 instead of 0.3)
        abs_diff = round(abs(self._diff_actual), 2)

        if abs_diff <= self._diff_off:
            await self._async_set_ac_fan_mode()
            self._control_state = "fan"
            self._hvac_action = HVACAction.FAN
            _LOGGER.info(
                "%s: BP → FAN  (|Diff|=%.2f ≤ Diff_off=%.2f) → set_hvac_mode=fan_only",
                self._name, abs_diff, self._diff_off,
            )

        elif abs_diff <= self._diff_low:
            self._control_state = "low"
            await self._set_ac_low_power()
            _LOGGER.info(
                "%s: BP → LOW  (Diff_off=%.2f < |Diff|=%.2f ≤ Diff_low=%.2f)",
                self._name, self._diff_off, abs_diff, self._diff_low,
            )

        else:
            self._control_state = "high"
            await self._set_ac_high_power()
            _LOGGER.info(
                "%s: BP → HIGH (|Diff|=%.2f > Diff_low=%.2f) → target=T_expectation=%.1f",
                self._name, abs_diff, self._diff_low, self._t_target,
            )

    # ------------------------------------------------------------------
    # Low-power mode
    # ------------------------------------------------------------------

    async def _set_ac_low_power(self) -> None:
        """Put the AC into low-power mode.

        Strategy: set the AC target temperature close to T_ac (what the
        AC itself measures).  Because the inverter compressor speed is
        proportional to |T_ac − AC_target|, a small gap means minimum
        frequency → minimum power consumption.

        The target is computed by rounding T_ac to the configured step:
          • round-up   → ceil(T_ac / step) × step   (gentler)
          • round-down → floor(T_ac / step) × step  (slightly more aggressive)

        If T_ac is unavailable we fall back to T_trust.
        """
        # Determine the base temperature for rounding
        base_temp: float | None = self._t_ac
        if base_temp is None:
            base_temp = self._t_trust

        if base_temp is None:
            # Nothing we can do — skip this cycle
            _LOGGER.warning(
                "%s: Low-power mode requires T_ac or T_trust — both unavailable",
                self._name,
            )
            return

        step = self._step
        if self._round_direction == ROUND_UP:
            ac_target = ceil(base_temp / step) * step
            direction = "ceil"
        else:
            ac_target = floor(base_temp / step) * step
            direction = "floor"

        _LOGGER.info(
            "%s: LOW-power → base_temp=%.2f(T_%s), step=%.2f, "
            "round=%s(%s) → %s(%.2f/%.2f)×%.2f = %.2f",
            self._name,
            base_temp,
            "ac" if self._t_ac is not None else "trust",
            step,
            self._round_direction,
            direction,
            direction,
            base_temp,
            step,
            step,
            ac_target,
        )

        await self._async_set_ac_target(ac_target)
        self._update_hvac_action()

    # ------------------------------------------------------------------
    # High-power mode
    # ------------------------------------------------------------------

    async def _set_ac_high_power(self) -> None:
        """Put the AC into high-power (full capacity) mode.

        Strategy: set the AC target directly to T_expectation.  The large
        gap between T_ac and the target causes the inverter to run at
        maximum frequency.
        """
        if self._t_target is None:
            return

        await self._async_set_ac_target(self._t_target)
        self._update_hvac_action()

    # ------------------------------------------------------------------
    # HVAC action helper
    # ------------------------------------------------------------------

    def _update_hvac_action(self) -> None:
        """Derive HVAC action from the sign of Diff_actual."""
        if self._diff_actual is None:
            self._hvac_action = HVACAction.IDLE
        elif self._diff_actual > 0:
            self._hvac_action = HVACAction.COOLING
        elif self._diff_actual < 0:
            self._hvac_action = HVACAction.HEATING
        else:
            self._hvac_action = HVACAction.IDLE

    # ==================================================================
    # Fixed-speed mode  (Mode_dp)
    # ==================================================================

    async def _control_fixed_speed(self) -> None:
        """Fixed-speed (on/off) AC control with hysteresis.

        State machine:

                 ┌──────────────────────────────────┐
                 │                                  │
                 ▼                                  │
             ┌──────┐   |Diff| ≥ Diff_on     ┌────┴───┐
             │ OFF  │ ───────────────────►   │  ON    │
             └──────┘                        └───┬────┘
                 ▲                                  │
                 │   |Diff| ≤ Diff_off_dp           │
                 └──────────────────────────────────┘

        The band between Diff_off_dp and Diff_on provides hysteresis
        that prevents short-cycling.
        """
        # Round to 2 decimal places to avoid floating-point artifacts
        abs_diff = round(abs(self._diff_actual), 2)
        is_running = self._control_state in ("on", "high")

        if not is_running and abs_diff >= self._diff_on:
            await self._async_set_ac_target(self._t_target)
            self._control_state = "on"
            self._update_hvac_action()
            _LOGGER.info(
                "%s: DP → ON  (|Diff|=%.2f ≥ Diff_on=%.2f) → target=%.1f, hvac=%s",
                self._name, abs_diff, self._diff_on,
                self._t_target, self._hvac_action,
            )

        elif is_running and abs_diff <= self._diff_off_dp:
            await self._async_turn_off_ac()
            self._control_state = "off"
            self._hvac_action = HVACAction.IDLE
            _LOGGER.info(
                "%s: DP → OFF (|Diff|=%.2f ≤ Diff_off_dp=%.2f) → AC turned off",
                self._name, abs_diff, self._diff_off_dp,
            )

        else:
            # Hysteresis band — hold current state
            if is_running:
                self._update_hvac_action()
            _LOGGER.info(
                "%s: DP HOLD — state=%s, |Diff|=%.2f ∈ (%.2f, %.2f) hysteresis band",
                self._name,
                self._control_state,
                abs_diff,
                self._diff_off_dp,
                self._diff_on,
            )

    # ==================================================================
    # Low-level AC actuation  (service calls to the underlying AC entity)
    # ==================================================================

    async def _async_set_ac_target(self, temperature: float) -> None:
        """Send target temperature and HVAC mode to the physical AC.

        This calls climate.set_hvac_mode + climate.set_temperature on
        the underlying AC entity.  Both calls are non-blocking.
        """
        if self._hvac_mode == HVACMode.OFF:
            return

        if not self.hass.states.get(self._climate_entity):
            _LOGGER.warning(
                "%s: AC entity '%s' not found in state machine",
                self._name,
                self._climate_entity,
            )
            return

        # 1. Ensure the AC is in the right HVAC mode
        ac_hvac_mode = self._resolve_ac_hvac_mode()
        _LOGGER.info(
            "%s: ▶ set_hvac_mode(%s) + set_temperature(%.1f°C)",
            self._name, ac_hvac_mode, round(temperature, 1),
        )
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {
                "entity_id": self._climate_entity,
                "hvac_mode": ac_hvac_mode,
            },
            blocking=False,
        )

        # 2. Set the target temperature
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {
                "entity_id": self._climate_entity,
                "temperature": round(temperature, 1),
            },
            blocking=False,
        )

    async def _async_turn_off_ac(self) -> None:
        """Turn the physical AC completely off."""
        _LOGGER.info(
            "%s: ▶ set_hvac_mode(OFF) — AC powered off", self._name
        )
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {
                "entity_id": self._climate_entity,
                "hvac_mode": HVACMode.OFF,
            },
            blocking=False,
        )

    async def _async_set_ac_fan_mode(self) -> None:
        """Switch the physical AC to fan-only mode.

        The compressor stops (no cooling/heating) but the indoor unit
        keeps circulating air. This is gentler than fully powering off
        and allows smooth resumption when the temperature drifts again.
        """
        _LOGGER.info(
            "%s: ▶ set_hvac_mode(FAN_ONLY) — compressor off, fan running", self._name
        )
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {
                "entity_id": self._climate_entity,
                "hvac_mode": HVACMode.FAN_ONLY,
            },
            blocking=False,
        )

    # ------------------------------------------------------------------
    # HVAC mode resolution
    # ------------------------------------------------------------------

    def _resolve_ac_hvac_mode(self) -> str:
        """Map our virtual HVAC mode to a mode the physical AC supports.

        When our mode is HEAT_COOL (auto), we check what the underlying
        AC reports as supported and pick the best match based on the sign
        of Diff_actual.
        """
        if self._hvac_mode != HVACMode.HEAT_COOL:
            # Direct pass-through for OFF / HEAT / COOL
            return self._hvac_mode

        ac_state = self.hass.states.get(self._climate_entity)
        if ac_state:
            supported: list[str] = ac_state.attributes.get("hvac_modes", []) or []

            # If the AC natively supports auto mode, use it
            if HVACMode.HEAT_COOL in supported:
                return HVACMode.HEAT_COOL

            # Otherwise choose based on whether we need heating or cooling
            if self._diff_actual is not None:
                if self._diff_actual > 0 and HVACMode.COOL in supported:
                    return HVACMode.COOL
                if self._diff_actual < 0 and HVACMode.HEAT in supported:
                    return HVACMode.HEAT

            # Fallback: use whatever is available
            for fallback in (HVACMode.COOL, HVACMode.HEAT):
                if fallback in supported:
                    return fallback

        return HVACMode.HEAT_COOL
