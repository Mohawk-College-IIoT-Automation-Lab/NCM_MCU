import csv
import json
import logging
import math
import os
import queue
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from statistics import fmean
from threading import Event
from typing import List, Optional, TextIO

import serial  # pyserial
from pydantic import ValidationError

from Constants.base_models import CalibrationMsg, Location, MCUData, Pressure, Sample, SyncMsg
from Constants.configs import (
    ErrorCodes,
    ExperimentMqttConfig,
    FlowConstants,
    LoggerConfig,
    MCUConfig,
    MCUMqttConfig,
)
from .GenericMqtteLogger.davids_logger import initialize_logging
from .GenericMqtteLogger.generic_mqtt import GenericMQTT  # For MQTT communication

NTP_TO_UNIX_EPOCH_US = 2_208_988_800 * 1_000_000  # 1900-01-01 -> 1970-01-01


class State(Enum):
    IDLE = "IDLE"  # waiting for NCM/Experiment/Prepare, serial closed
    PREPARING = "PREPARING"  # serial open, waiting for "@ ready" from the MCU
    RUNNING = "RUNNING"  # logging + publishing until NCM/Experiment/Stop


class MCU(GenericMQTT):
    """Bridge between one MCU (serial) and the NCM experiment (MQTT).

    Threading: paho callbacks only put events on a queue. All state, the serial
    port and the CSV file are owned by the thread that calls step() / run(), so
    no locks are needed.

    Serial line protocol (one message per line):
        # <text>      info message, logged
        ! <text>      error, logged; aborts a Prepare in progress
        @ <json>      structured message, "type" is one of: cal, sync, ready
        t_ms,...      CSV header, ignored
        <csv>         data: t_ms,p_1_mbar,p_2_mbar,diff_mbar
    """

    _instance = None

    CSV_COLUMNS = ["t_ms", "t_unix_ms", "p_1_mbar", "p_2_mbar", "diff_mbar", "velocity_m_s"]
    MAX_BAD_LINE_LOGS = 10  # log the first N malformed lines, then every 1000th

    @classmethod
    def get_instance(cls, config: MCUConfig, logger_config: LoggerConfig):
        if cls._instance is None:
            cls._instance = cls(config, logger_config)
        return cls._instance

    def __init__(self, config: MCUConfig, logger_config: LoggerConfig):
        if getattr(self, "_initialized", False):
            return

        self._initialized = True

        super().__init__(
            client_name=config.client_name,
            log_name=logger_config.log_name,
            host_name=logger_config.mqtt_config.host_name,
            host_port=logger_config.mqtt_config.host_port,
        )
        logging.debug(f"[MCU] Initializing {config.client_name} on {config.port}")

        self._config = config
        self._state = State.IDLE
        self._events: "queue.Queue[tuple]" = queue.Queue()

        # Serial
        self._serial: Optional[serial.Serial] = None
        self._rx_buffer = b""
        self._prepare_deadline = 0.0
        self._bad_lines = 0

        # Start-up info from the MCU (reset for every Prepare)
        self._cal: Optional[CalibrationMsg] = None
        self._sync: Optional[SyncMsg] = None
        self._sync_unix_ms = 0.0

        # File. A renamed file name sticks for later runs.
        self._file_name = config.default_file_name
        self._file: Optional[TextIO] = None
        self._file_path: Optional[Path] = None
        self._csv_writer = None

        # Measurements
        self._batch: List[Sample] = []
        self._velocity_scale = (
            2.0 * FlowConstants.MBAR_TO_PA / config.fluid_density_kg_m3
        )
        self._correction = config.error_correction

        logging.debug(
            f"[MCU][init] Batch size: {config.batch_size}, Prepare timeout: {config.prepare_timeout_s} s, "
            f"Correction: {self._correction.model_dump_json()}"
        )

        logging.debug(f"[MCU][MQTT][init] Connecting to MQTT")
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_connect()

    # ------------------------------------------------------------------ #
    # MQTT (runs on the paho thread - only queue events here)
    # ------------------------------------------------------------------ #

    def _on_connect(self, client, userdata, flags, rc, props=None):
        logging.info(f"[MCU][MQTT] Connected to broker (rc: {rc})")

        callbacks = {
            ExperimentMqttConfig.prepare_topic: self._mqtt_prepare_callback,
            ExperimentMqttConfig.stop_topic: self._mqtt_stop_callback,
            ExperimentMqttConfig.rename_topic: self._mqtt_rename_callback,
        }

        for sub_topic, callback in callbacks.items():
            topic = f"{ExperimentMqttConfig.base_topic}{sub_topic}"
            client.subscribe(topic)
            client.message_callback_add(topic, callback)
            logging.debug(
                f"[MCU][MQTT][init] Subbing and setting up callback for topic: {topic}"
            )

    def _mqtt_prepare_callback(self, client, userdata, msg):
        logging.debug(f"[MCU][MQTT] Prepare command received")
        self._events.put(("prepare",))

    def _mqtt_stop_callback(self, client, userdata, msg):
        logging.debug(f"[MCU][MQTT] Stop command received")
        self._events.put(("stop",))

    def _mqtt_rename_callback(self, client, userdata, msg):
        file_name = msg.payload.decode(errors="replace")
        logging.debug(f"[MCU][MQTT] Rename command received, data received: {file_name}")
        self._events.put(("rename", file_name))

    def _publish(self, topic: str, payload) -> None:
        info = self.mqtt_client.publish(topic, payload)
        if info.rc != 0:
            logging.warning(f"[MCU][MQTT] Publish to {topic} failed (rc: {info.rc})")

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def step(self) -> None:
        """One pass of the loop: handle queued MQTT events, then read serial."""
        self._handle_events()

        if self._serial is None:
            time.sleep(self._config.serial_read_timeout_s)
            return

        if self._state == State.PREPARING and time.monotonic() > self._prepare_deadline:
            logging.error(
                f"[MCU] MCU was not ready within {self._config.prepare_timeout_s} s, aborting Prepare",
                extra={"error_code": ErrorCodes.PREPARE_TIMEOUT},
            )
            self._end_experiment()
            return

        try:
            # Read whatever has arrived; block up to the read timeout for at least 1 byte
            data = self._serial.read(self._serial.in_waiting or 1)
        except (serial.SerialException, OSError) as e:
            logging.error(
                f"[MCU][Serial] Read failed ({e}), closing the experiment",
                extra={"error_code": ErrorCodes.SERIAL_ERROR},
            )
            self._end_experiment()
            return

        if not data:
            return

        self._rx_buffer += data
        *lines, self._rx_buffer = self._rx_buffer.split(b"\n")
        for raw in lines:
            self._handle_line(raw.decode("utf-8", errors="replace").strip())
            if self._serial is None:  # the line ended the experiment (e.g. '!' while preparing)
                break

    def _handle_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return

            match event:
                case ("prepare",):
                    self._prepare()
                case ("stop",):
                    self._stop()
                case ("rename", file_name):
                    self._rename(file_name)

    # ------------------------------------------------------------------ #
    # State transitions
    # ------------------------------------------------------------------ #

    def _prepare(self) -> None:
        if self._state != State.IDLE:
            logging.debug(f"[MCU] Prepare ignored, state is {self._state.value}")
            return

        try:
            # Opening the port (DTR asserted) releases the MCU from its wait in setup()
            self._serial = serial.Serial(
                port=self._config.port,
                baudrate=self._config.baud_rate,
                timeout=self._config.serial_read_timeout_s,
            )
            self._serial.reset_input_buffer()
        except (serial.SerialException, OSError) as e:
            logging.error(
                f"[MCU][Serial] Could not open {self._config.port}: {e}",
                extra={"error_code": ErrorCodes.SERIAL_ERROR},
            )
            self._serial = None
            return

        self._rx_buffer = b""
        self._bad_lines = 0
        self._cal = None
        self._sync = None
        self._batch.clear()
        self._prepare_deadline = time.monotonic() + self._config.prepare_timeout_s
        self._state = State.PREPARING
        logging.info(f"[MCU] Opened {self._config.port}, waiting for the MCU to be ready")

    def _ready(self) -> None:
        if self._state != State.PREPARING:
            logging.warning(f"[MCU] Ready received while {self._state.value}, ignored")
            return

        if self._cal is None or self._sync is None:
            missing = [n for n, v in (("cal", self._cal), ("sync", self._sync)) if v is None]
            logging.error(
                f"[MCU] Ready received without {', '.join(missing)} message(s), aborting Prepare",
                extra={"error_code": ErrorCodes.BAD_MESSAGE},
            )
            self._end_experiment()
            return

        if not self._open_file():
            self._end_experiment()
            return

        self._state = State.RUNNING
        logging.info(f"[MCU] {self._cal.location.value} ready, starting experiment")
        self._publish(f"{ExperimentMqttConfig.base_topic}{ExperimentMqttConfig.start_topic}", "")

    def _stop(self) -> None:
        if self._state == State.IDLE:
            logging.debug("[MCU] Stop ignored, not running")
            return
        logging.info(f"[MCU] Stopping ({self._state.value})")
        self._end_experiment()

    def _end_experiment(self) -> None:
        """Save what is left, close the file and the serial port, go back to IDLE.

        Closing the port drops DTR, which makes the MCU reset and wait in setup().
        """
        if self._batch:
            self._flush_batch(publish=False)  # leftovers are saved, not published
        self._close_file()

        if self._serial is not None:
            try:
                self._serial.close()
                logging.debug(f"[MCU][Serial] Closed {self._config.port}")
            except (serial.SerialException, OSError) as e:
                logging.warning(f"[MCU][Serial] Error closing {self._config.port}: {e}")
            self._serial = None

        self._rx_buffer = b""
        self._state = State.IDLE

    def _rename(self, file_name: str) -> None:
        # Same idea as the DAQ: the name is used for the next file and sticks
        # for later runs. If a file is open, it is closed and a new one is
        # opened under the new name. Logging carries on without a gap.
        name = os.path.basename(file_name.strip())
        for ext in (".csv", ".tdms"):
            if name.lower().endswith(ext):
                name = name[: -len(ext)]
        if not name:
            logging.error(
                f"[MCU] File name '{file_name}' is empty, cannot rename",
                extra={"error_code": ErrorCodes.FILE_ERROR},
            )
            return

        self._file_name = name
        logging.info(f"[MCU] File name set to '{name}'")

        if self._file is not None:
            self._close_file()
            if not self._open_file():
                logging.error(
                    "[MCU] Could not open the renamed file, stopping",
                    extra={"error_code": ErrorCodes.FILE_ERROR},
                )
                self._end_experiment()

    # ------------------------------------------------------------------ #
    # Serial parsing
    # ------------------------------------------------------------------ #

    def _handle_line(self, line: str) -> None:
        if not line:
            return

        tag, body = line[0], line[1:].strip()

        if tag == "#":
            logging.info(f"[MCU][Serial] {body}")
        elif tag == "!":
            logging.error(
                f"[MCU][Serial] MCU error: {body}", extra={"error_code": ErrorCodes.MCU_ERROR}
            )
            if self._state == State.PREPARING:
                logging.error("[MCU] Aborting Prepare because of the MCU error")
                self._end_experiment()
        elif tag == "@":
            self._handle_message(body)
        elif line.startswith("t_ms"):
            logging.debug(f"[MCU][Serial] Header: {line}")
        elif self._state == State.RUNNING:
            self._handle_sample(line)
        else:
            self._bad_line(f"data before ready: {line}")

    def _handle_message(self, body: str) -> None:
        try:
            msg = json.loads(body)
            msg_type = msg.pop("type", None)
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            self._bad_line(f"bad '@' message ({e!r}): {body}")
            return

        try:
            match msg_type:
                case "cal":
                    self._cal = CalibrationMsg.model_validate(msg)
                    self._check_location(self._cal.location)
                    logging.info(f"[MCU][Serial] Calibration: {self._cal.model_dump_json()}")
                case "sync":
                    self._sync = SyncMsg.model_validate(msg)
                    sync_us = self._sync.server_reference_time_us
                    if self._config.sync_time_epoch == "ntp":
                        sync_us -= NTP_TO_UNIX_EPOCH_US
                    self._sync_unix_ms = sync_us / 1000.0
                    logging.info(f"[MCU][Serial] Time sync point: {self._sync_iso()}")
                case "ready":
                    self._ready()
                case _:
                    self._bad_line(f"unknown message type '{msg_type}': {body}")
        except ValidationError as e:
            logging.error(
                f"[MCU][Serial] Invalid '{msg_type}' message: {body} ({e.error_count()} error(s): "
                f"{e.errors()[0]['loc']} {e.errors()[0]['msg']})",
                extra={"error_code": ErrorCodes.BAD_MESSAGE},
            )
            if self._state == State.PREPARING:
                self._end_experiment()

    def _check_location(self, location: Location) -> None:
        expected = self._config.expected_location
        if expected is not None and expected != location:
            logging.warning(
                f"[MCU] Location mismatch on {self._config.port}: expected {expected.value}, "
                f"MCU reports {location.value}. Using {location.value}.",
                extra={"error_code": ErrorCodes.LOCATION_MISMATCH},
            )

    def _bad_line(self, reason: str) -> None:
        self._bad_lines += 1
        if self._bad_lines <= MCU.MAX_BAD_LINE_LOGS or self._bad_lines % 1000 == 0:
            logging.warning(
                f"[MCU][Serial] Ignored line #{self._bad_lines} - {reason}",
                extra={"error_code": ErrorCodes.BAD_MESSAGE},
            )

    # ------------------------------------------------------------------ #
    # Measurements
    # ------------------------------------------------------------------ #

    def _velocity(self, diff_mbar: float) -> float:
        """sign(d) * sqrt(2 * |d - err| * 100 / rho), with err() evaluated on |d|."""
        magnitude = abs(diff_mbar)
        corrected = magnitude - self._correction.evaluate(magnitude)
        return math.copysign(math.sqrt(self._velocity_scale * abs(corrected)), diff_mbar)

    def _handle_sample(self, line: str) -> None:
        parts = line.split(",")
        if len(parts) != 4:
            self._bad_line(f"expected 4 fields, got {len(parts)}: {line}")
            return
        try:
            t_ms = int(parts[0])
            p_1, p_2, diff = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            self._bad_line(f"not numeric: {line}")
            return

        self._batch.append(
            Sample(
                t_ms=t_ms,
                t_unix_ms=self._sync_unix_ms + t_ms,
                p_1_mbar=p_1,
                p_2_mbar=p_2,
                diff_mbar=diff,
                velocity=self._velocity(diff),
            )
        )

        if len(self._batch) >= self._config.batch_size:
            self._flush_batch(publish=True)

    def _flush_batch(self, publish: bool) -> None:
        batch, self._batch = self._batch, []
        self._write_rows(batch)

        if not publish or self._cal is None:
            return

        dec = self._config.display_decimals
        avg_diff = fmean(s.diff_mbar for s in batch)
        mcu_data = MCUData(
            Location=self._cal.location,
            Pressure=Pressure(
                DP=round(avg_diff, dec),
                CH1=round(fmean(s.p_1_mbar for s in batch), dec),
                CH2=round(fmean(s.p_2_mbar for s in batch), dec),
            ),
            Velocity=round(self._velocity(avg_diff), dec),  # from the averaged diff
        )
        self._publish(MCUMqttConfig.display_data_topic, mcu_data.model_dump_json())

    # ------------------------------------------------------------------ #
    # CSV file
    # ------------------------------------------------------------------ #

    def _sync_iso(self) -> str:
        return datetime.fromtimestamp(self._sync_unix_ms / 1000.0, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )

    def _open_file(self) -> bool:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = self._config.data_path / f"{self._file_name}_{self._cal.location.value}_{stamp}.csv"
        logging.debug(f"[MCU] Trying to create CSV file {file_path}")

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(file_path, "w", newline="")
            header = {
                "location": self._cal.location.value,
                "port": self._config.port,
                "client_name": self._config.client_name,
                "ref1_mbar": self._cal.ref1_mbar,
                "ref2_mbar": self._cal.ref2_mbar,
                "pair_offset_mbar": self._cal.pair_offset_mbar,
                "server_reference_time_us": self._sync.server_reference_time_us,
                "sync_time_epoch": self._config.sync_time_epoch,
                "sync_time_utc": self._sync_iso(),
                "file_created_local": datetime.now().isoformat(timespec="seconds"),
                "batch_size": self._config.batch_size,
                "fluid_density_kg_m3": self._config.fluid_density_kg_m3,
                "error_correction": self._correction.model_dump_json(),
            }
            for key, value in header.items():
                self._file.write(f"# {key}: {value}\n")

            self._csv_writer = csv.writer(self._file)
            self._csv_writer.writerow(MCU.CSV_COLUMNS)
            self._file.flush()
        except OSError as e:
            logging.error(
                f"[MCU] Could not create CSV file {file_path}: {e}",
                extra={"error_code": ErrorCodes.FILE_ERROR},
            )
            self._file = None
            self._csv_writer = None
            return False

        self._file_path = file_path
        logging.info(f"[MCU] Created CSV file: {file_path}")
        return True

    def _write_rows(self, batch: List[Sample]) -> None:
        if self._csv_writer is None:
            return
        dec = self._config.sample_decimals
        try:
            self._csv_writer.writerows(
                (s.t_ms, f"{s.t_unix_ms:.3f}", s.p_1_mbar, s.p_2_mbar, s.diff_mbar, round(s.velocity, dec))
                for s in batch
            )
            self._file.flush()
        except OSError as e:
            logging.error(
                f"[MCU] CSV writing exception: {e}", extra={"error_code": ErrorCodes.FILE_ERROR}
            )

    def _close_file(self) -> None:
        if self._file is None:
            return
        logging.debug(f"[MCU] Trying to close CSV file: {self._file_path}")
        try:
            self._file.close()
            logging.info(f"[MCU] CSV file closed: {self._file_path}")
        except OSError as e:
            logging.error(
                f"[MCU] Exception when closing CSV {self._file_path}: {e}",
                extra={"error_code": ErrorCodes.FILE_ERROR},
            )
        self._file = None
        self._csv_writer = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> State:
        return self._state

    def close(self):
        logging.debug("[MCU] Closing serial and CSV file...")
        self._end_experiment()

        logging.debug("[MCU] Disconnecting from MQTT...")
        self.mqtt_disconnect()
        self.mqtt_client.loop_stop()

    @classmethod
    def run(cls, config: MCUConfig, logger_config: LoggerConfig, stop_event: Event):
        initialize_logging(
            process_name=logger_config.log_name,
            broker=logger_config.mqtt_config.host_name,
            port=logger_config.mqtt_config.host_port,
        )

        logging.debug(f"[MCU] Starting the MCU Worker for {config.port}")
        mcu = MCU.get_instance(config=config, logger_config=logger_config)

        try:
            while not stop_event.is_set():
                mcu.step()
        finally:
            logging.debug("[MCU] Stopping the MCU Worker")
            mcu.close()
