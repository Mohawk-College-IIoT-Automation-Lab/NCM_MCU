#! /usr/bin/env python3
"""Simulated MCU on a pseudo-terminal, for testing main.py without hardware.

    python3 tools/fake_mcu.py --link /tmp/fake_mcu --location LL
    python3 main.py --config <config with "port": "/tmp/fake_mcu">

Like the firmware, it waits for the port to be opened, prints the start-up
messages, streams data at 200 Hz, and "resets" when the port is closed.
"""

import argparse
import errno
import json
import math
import os
import pty
import select
import time

parser = argparse.ArgumentParser()
parser.add_argument("--link", default="/tmp/fake_mcu", help="symlink to create for the serial port")
parser.add_argument("--location", default="LL")
parser.add_argument("--rate", type=float, default=200.0, help="samples per second")
parser.add_argument("--startup-s", type=float, default=1.0, help="simulated calibration + NTP time")
parser.add_argument(
    "--mode",
    choices=["normal", "ntp_fail", "no_ready", "bad_lines"],
    default="normal",
)
args = parser.parse_args()

master, slave = pty.openpty()
slave_name = os.ttyname(slave)
os.close(slave)
if os.path.islink(args.link) or os.path.exists(args.link):
    os.remove(args.link)
os.symlink(slave_name, args.link)
print(f"fake MCU on {args.link} -> {slave_name} ({args.mode})", flush=True)


def port_open() -> bool:
    """A pty master reads EIO while nobody has the slave open."""
    readable, _, _ = select.select([master], [], [], 0)
    if not readable:
        return True
    try:
        os.read(master, 1024)  # discard anything the host sent
        return True
    except OSError as e:
        if e.errno == errno.EIO:
            return False
        raise


def send(line: str) -> None:
    os.write(master, (line + "\r\n").encode())


while True:
    # setup(): block until the host opens the port
    while not port_open():
        time.sleep(0.05)
    print("port opened -> setup()", flush=True)

    send("# Sensor connected on Wire")
    send("# Sensor connected on myWire")
    time.sleep(args.startup_s / 2)
    send(
        "@ "
        + json.dumps(
            {"type": "cal", "location": args.location, "ref1_mbar": 1013.251,
             "ref2_mbar": 1012.874, "pair_offset_mbar": 0.377}
        )
    )
    send("# Synchronizing with NTP server 192.168.1.10")
    time.sleep(args.startup_s / 2)

    if args.mode == "ntp_fail":
        send("! ERROR: NTP sync failed after 120 s. Check the server and network, then reset the board.")
        while port_open():  # halt()
            time.sleep(0.05)
        print("port closed -> reset", flush=True)
        continue

    send("# NTP sync OK after 1 attempt(s)")
    send("@ " + json.dumps({"type": "sync", "server_reference_time_us": int(time.time() * 1e6)}))

    if args.mode == "no_ready":
        while port_open():
            time.sleep(0.05)
        print("port closed -> reset", flush=True)
        continue

    send('@ {"type": "ready"}')
    send("t_ms,p_1_mbar,p_2_mbar,diff_mbar")

    # loop(): stream until the host closes the port
    t0 = time.monotonic()
    n = 0
    while port_open():
        t_ms = int((time.monotonic() - t0) * 1000)
        diff = 3.0 * math.sin(2 * math.pi * 0.2 * t_ms / 1000)  # +-3 mbar, both directions
        p1 = 1013.0 + diff / 2
        send(f"{t_ms},{p1:.3f},{p1 - diff:.3f},{diff:.3f}")
        n += 1
        if args.mode == "bad_lines" and n % 50 == 0:
            send("12,garbage,line")
        time.sleep(1.0 / args.rate)
    print(f"port closed after {n} samples -> reset", flush=True)
