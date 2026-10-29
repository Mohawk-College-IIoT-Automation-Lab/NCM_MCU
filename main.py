#! /usr/bin/env python3
"""Bridge one MCU (serial) to the NCM experiment (MQTT).

Run one instance per MCU, each with its own config:
    python3 main.py --config configs/ttyACM0.json
"""

import argparse
import logging
import signal
from pathlib import Path
from threading import Event

from Constants import LoggerConfig, MCUConfig
from MCU import MCU
from MCU.GenericMqtteLogger.davids_logger import initialize_logging

stop_event = Event()


def handle_signal(*args):
    logging.debug("[Main] Exiting")
    stop_event.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        required=True,
        help="MCU config file",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)

    config = MCUConfig.load(args.config)

    initialize_logging(
        process_name=config.client_name,
        broker=config.mqtt_config.host_name,
        port=config.mqtt_config.host_port,
    )
    logging.debug(f"[Main] Main initialized with config {args.config}")

    mcu_logger = LoggerConfig(log_name=config.client_name, mqtt_config=config.mqtt_config)
    MCU.run(config, mcu_logger, stop_event)
