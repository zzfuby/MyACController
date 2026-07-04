"""Constants for My AC Controller."""

from typing import Final

DOMAIN: Final = "my_ac_controller"

# --- Config entry data keys (structural, set at creation) ---
CONF_NAME: Final = "name"
CONF_CLIMATE_ENTITY: Final = "climate_entity"
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_MODE: Final = "mode"  # "bp" = inverter, "dp" = fixed-speed

# --- Mode options ---
MODE_BP: Final = "bp"  # inverter / variable-frequency
MODE_DP: Final = "dp"  # fixed-speed / on-off

# --- Config entry options keys (tuning, adjustable via Options) ---
# Inverter mode (bp)
CONF_DIFF_OFF: Final = "diff_off"  # stop threshold: |Diff_actual| <= this → turn off
CONF_DIFF_LOW: Final = "diff_low"  # low-power threshold: |Diff_actual| <= this → low power
CONF_STEP: Final = "step"  # temperature step for low-power target rounding
CONF_ROUND_DIRECTION: Final = "round_direction"  # "up" or "down"

# Fixed-speed mode (dp)
CONF_DIFF_ON: Final = "diff_on"  # turn-on threshold
CONF_DIFF_OFF_DP: Final = "diff_off_dp"  # turn-off threshold

# Common
CONF_POLL_INTERVAL: Final = "poll_interval"  # seconds between control cycles
CONF_AC_TEMP_SENSOR: Final = "ac_temp_sensor"  # optional: external sensor for AC internal temp

# --- Round direction ---
ROUND_UP: Final = "up"
ROUND_DOWN: Final = "down"

# --- Default values ---
DEFAULT_NAME: Final = "My AC Controller"
DEFAULT_DIFF_OFF: Final = 0.3  # °C
DEFAULT_DIFF_LOW: Final = 1.0  # °C
DEFAULT_STEP: Final = 1.0  # °C
DEFAULT_ROUND_DIRECTION: Final = "up"
DEFAULT_DIFF_ON: Final = 1.0  # °C
DEFAULT_DIFF_OFF_DP: Final = 0.3  # °C
DEFAULT_POLL_INTERVAL: Final = 30  # seconds

# --- Bounds ---
MIN_DIFF: Final = 0.1
MAX_DIFF: Final = 10.0
MIN_STEP: Final = 0.5
MAX_STEP: Final = 5.0
MIN_POLL_INTERVAL: Final = 10
MAX_POLL_INTERVAL: Final = 300

# --- Attributes ---
ATTR_CONTROL_STATE: Final = "control_state"
ATTR_DIFF_ACTUAL: Final = "diff_actual"
ATTR_T_AC: Final = "t_ac"
ATTR_T_TRUST: Final = "t_trust"
ATTR_T_EXPECTATION: Final = "t_expectation"
