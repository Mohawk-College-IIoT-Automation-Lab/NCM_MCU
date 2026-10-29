#!/bin/bash
# Start one bridge per MCU config in configs/ (LL, LQ, RQ, RR).
# Ctrl+C stops all of them (each one closes its file and serial port).

cd "$(dirname "$0")" || exit 1

PIDS=()
for loc in LL LQ RQ RR; do
    python3 main.py --config "configs/${loc}.json" &
    PIDS+=($!)
done

trap 'kill -INT "${PIDS[@]}" 2>/dev/null' INT TERM
wait
