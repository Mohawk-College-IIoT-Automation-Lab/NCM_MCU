import bisect
import json
import logging
import os
from pathlib import Path
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from .base_models import Location

# Folder that holds main.py; relative paths in the config resolve against it
PROJECT_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Fixed constants
# --------------------------------------------------------------------------- #


class ExperimentMqttConfig:
    base_topic: str = "NCM/Experiment/"
    prepare_topic: str = "Prepare"
    start_topic: str = "Start"
    stop_topic: str = "Stop"
    rename_topic: str = "Rename"


class MCUMqttConfig:
    display_data_topic: str = "NCM/MCU/DisplayData"


class ErrorCodes:
    """Passed to logging via extra={"error_code": ...} (shows up in the MQTT log)."""

    LOCATION_MISMATCH: int = 4001
    PREPARE_TIMEOUT: int = 4002
    MCU_ERROR: int = 4003
    SERIAL_ERROR: int = 4004
    BAD_MESSAGE: int = 4005
    FILE_ERROR: int = 4006


class FlowConstants:
    MBAR_TO_PA: float = 100.0


# --------------------------------------------------------------------------- #
# Error correction functions
#
#   corrected = sign(d) * (|d| - err(|d|))
#
# err() is always evaluated on |diff_mbar|, so the correction is the same for
# flow in either direction. Pick one by "type" in the config file.
# --------------------------------------------------------------------------- #


class PolynomialCorrection(BaseModel):
    """err(x) = c0 + c1*x + c2*x^2 + ...   (coefficients in ascending order).

    Covers linear, quadratic and weighted-quadratic fits.
    """

    type: Literal["polynomial"] = "polynomial"
    coeffs: List[float] = Field(min_length=1)

    def evaluate(self, x: float) -> float:
        result = 0.0
        for c in reversed(self.coeffs):  # Horner's method
            result = result * x + c
        return result


class PiecewiseLinearCorrection(BaseModel):
    """err(x) by linear interpolation between (x[i], y[i]) points.

    Covers a double linear spline (3 points) or a full calibration table.
    Outside the table the first / last segment is extended linearly
    (not clamped).
    """

    type: Literal["piecewise_linear"] = "piecewise_linear"
    x: List[float] = Field(min_length=2)
    y: List[float] = Field(min_length=2)

    @model_validator(mode="after")
    def _check_points(self):
        if len(self.x) != len(self.y):
            raise ValueError("x and y must be the same length")
        if any(b <= a for a, b in zip(self.x, self.x[1:])):
            raise ValueError("x must be strictly increasing")
        return self

    def evaluate(self, x: float) -> float:
        i = bisect.bisect_right(self.x, x) - 1
        i = min(max(i, 0), len(self.x) - 2)  # clamp to a valid segment
        x0, x1 = self.x[i], self.x[i + 1]
        y0, y1 = self.y[i], self.y[i + 1]
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


ErrorCorrection = Annotated[
    Union[PolynomialCorrection, PiecewiseLinearCorrection],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# Runtime configs (loaded from JSON)
# --------------------------------------------------------------------------- #


class MQTTConfig(BaseModel):
    host_name: str = "localhost"
    host_port: int = 1883


class LoggerConfig(BaseModel):
    log_name: str = "Log"
    mqtt_config: MQTTConfig


class MCUConfig(BaseModel):
    # Serial
    port: str = "/dev/ttyACM0"
    baud_rate: Optional[int] = 115200  # ignored by native USB CDC, kept for completeness
    serial_read_timeout_s: float = 0.1  # how often the loop checks MQTT events

    # Expected location for this port. A mismatch is logged; the MCU wins.
    expected_location: Optional[Location] = None

    # Experiment
    batch_size: int = Field(default=20, ge=1)
    prepare_timeout_s: float = Field(default=60.0, gt=0)  # NTP alone can take 30 s
    display_decimals: int = Field(default=2, ge=0)

    # Output
    data_dir: str = "./data"  # relative paths resolve against the project folder
    default_file_name: str = "mcu"  # -> mcu_<LOC>_<YYYYmmdd_HHMMSS>.csv
    sample_decimals: int = Field(default=4, ge=0)  # rounding of computed values in the CSV

    # server_reference_time_us is counted from the Unix epoch (1970) or the
    # NTP epoch (1900), depending on how the firmware converts it
    sync_time_epoch: Literal["unix", "ntp"] = "ntp"

    # Flow
    fluid_density_kg_m3: float = Field(default=1000.0, gt=0)
    error_correction: ErrorCorrection = PolynomialCorrection(coeffs=[0.0, 0.0666, 6e-6])

    # MQTT / logging. client_name defaults to MCU-<port basename>, e.g. MCU-ttyACM0
    mqtt_config: MQTTConfig = MQTTConfig()
    client_name: Optional[str] = None

    @model_validator(mode="after")
    def _default_client_name(self):
        if not self.client_name:
            self.client_name = f"MCU-{os.path.basename(self.port)}"
        return self

    @property
    def data_path(self) -> Path:
        path = Path(self.data_dir).expanduser()
        return path if path.is_absolute() else PROJECT_DIR / path

    @classmethod
    def load(cls, file: Path) -> "MCUConfig":
        """Load the config, creating a default one if the file does not exist."""
        file = Path(file)
        if not file.exists():
            logging.warning(f"[MCU][Config] {file} not found, creating a default config")
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(cls().model_dump_json(indent=2))
        return cls.model_validate(json.loads(file.read_text()))
