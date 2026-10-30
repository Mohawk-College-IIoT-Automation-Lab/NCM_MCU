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
    CH1: float  # avg p_sen_mbar
    CH2: float  # avg p_side_mbar


class MCUData(BaseModel):
    Location: Location
    Pressure: Pressure
    Velocity: float  # m/s, from the averaged diff_mbar


# --------------------------------------------------------------------------- #
# Serial messages (lines tagged with '@' from the MCU)
# --------------------------------------------------------------------------- #


class CalibrationMsg(BaseModel):
    """@ {"type": "cal", "location": "LL", "ref_sen_mbar": .., "ref_side_mbar": .., "pair_offset_mbar": ..}"""

    location: Location
    ref_sen_mbar: float
    ref_side_mbar: float
    pair_offset_mbar: float


class SyncMsg(BaseModel):
    """@ {"type": "sync", "server_reference_time_us": ..}  (absolute time of t_us == 0)"""

    server_reference_time_us: int


class Sample(NamedTuple):
    """One data line: t_us,p_sen_mbar,p_side_mbar,diff_mbar (+ t_unix_us and velocity computed here).
    """

    t_us: int
    t_unix_us: int
    p_sen_mbar: float
    p_side_mbar: float
    diff_mbar: float
    velocity: float
