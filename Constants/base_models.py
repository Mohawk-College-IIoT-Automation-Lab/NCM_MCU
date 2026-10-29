from enum import StrEnum
from typing import NamedTuple

from pydantic import BaseModel


# --------------------------------------------------------------------------- #
# MQTT payloads (published on NCM/MCU/DisplayData)
# --------------------------------------------------------------------------- #


class Location(StrEnum):
    LL = "LL"
    LQ = "LQ"
    RQ = "RQ"
    RR = "RR"


class Pressure(BaseModel):
    DP: float  # avg diff_mbar
    CH1: float  # avg p_1_mbar
    CH2: float  # avg p_2_mbar


class MCUData(BaseModel):
    Location: Location
    Pressure: Pressure
    Velocity: float  # m/s, from the averaged diff_mbar


# --------------------------------------------------------------------------- #
# Serial messages (lines tagged with '@' from the MCU)
# --------------------------------------------------------------------------- #


class CalibrationMsg(BaseModel):
    """@ {"type": "cal", "location": "LL", "ref1_mbar": .., "ref2_mbar": .., "pair_offset_mbar": ..}"""

    location: Location
    ref1_mbar: float
    ref2_mbar: float
    pair_offset_mbar: float


class SyncMsg(BaseModel):
    """@ {"type": "sync", "server_reference_time_us": ..}  (absolute time of t_ms == 0)"""

    server_reference_time_us: int


class Sample(NamedTuple):
    """One data line: t_ms,p_1_mbar,p_2_mbar,diff_mbar (+ t_unix_ms and velocity computed here).
    """

    t_ms: int
    t_unix_ms: float
    p_1_mbar: float
    p_2_mbar: float
    diff_mbar: float
    velocity: float
