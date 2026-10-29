# NCM MCU Bridge

Connects one pressure/velocity MCU (USB serial) to the NCM experiment (MQTT).
Run one instance per MCU (LL, LQ, RQ, RR).

```
main.py                     entry point: python3 main.py --config configs/LL.json
start_mcus.sh               starts all four
Constants/base_models.py    MQTT payloads (MCUData) and serial messages
Constants/configs.py        topics, error codes, error correction types, MCUConfig
MCU/MCU.py                  the bridge (state machine, serial parsing, CSV, MQTT)
MCU/GenericMqtteLogger/     git submodule (see Install)
configs/*.json              one config per MCU
tools/fake_mcu.py           simulated MCU for testing without hardware
```

## Install

```bash
git submodule add https://github.com/Mohawk-College-IIoT-Automation-Lab/GenericMqtteLogger.git MCU/GenericMqtteLogger
python3 -m pip install -r requirements.txt   # Python >= 3.11
```

`paho-mqtt` is pinned below 2.0 because `GenericMqtteLogger` uses the 1.x `Client()` API.
The user running the script needs access to the serial ports (`dialout` group).

## Experiment flow

[Experiment Flow Chart](experiment_flow.png)

Each instance publishes Start when its own MCU is ready. The DAQ ignores repeated Starts,
so it starts with the first ready MCU and the others begin logging when they're ready.
This system is not suitable to use with 4 sensors and is developed for testing the
sensor performance in the Water Model. Once this sensor passes testing, some kind of
orchestrator/state management scirpt/system will need to be added to start
everything at the same time.

## Serial protocol (MCU → PC)

One message per line. The first character says what the line is:

| Tag      | Meaning                                 | Bridge action                                     |
| -------- | --------------------------------------- | ------------------------------------------------- |
| `#`      | human-readable message                  | logged (INFO)                                     |
| `!`      | error                                   | logged (ERROR 4003); aborts a Prepare in progress |
| `@`      | JSON message with `"type"`              | `cal`, `sync`, `ready` (see below)                |
| `t_ms,…` | CSV header                              | ignored                                           |
| none     | data `t_ms,p_1_mbar,p_2_mbar,diff_mbar` | used only while RUNNING                           |

If an `@` message has no `"type"`, it is recognised by its keys (`location` → cal,
`server_reference_time_us` → sync, `ready` → ready) (order of `cal` and `sync` doesn't matter; `ready` must be last)

### Message Examples

Each message, except CSV header and data, starts with a tag and ends with a newline character

#### Error

```
! NTP sync failed
```

```cpp
Serial.print(F("! NTP sync failed"));
```

#### Info

```
# USB Serial Connected
```

```cpp
Serial.print(F("# USB Serial Connected"));
```

#### JSON

```
@ {"type": "sync", "server_reference_time_us": 4002038345268905}
```

```cpp

Serial.print(F("@ {\"type\": \"sync\", \"server_reference_time_us\": "));
printUint64(syncRef.serverTime_us);
Serial.println('}');
```

### JSON Examples

```json
{
    "type": "cal",
    "location": LL,
    "ref1_mbar": 997.402,
    "ref2_mbar": 100.672,
    "pair_offset_mbar": 3.270
}
```

```json
{
    "type": "sync",
    "server_reference_time_us": 4002038345280346
}
```

```json
{
    "type": "ready"
}
```

## MQTT

| Topic                    | Direction | Payload                  |
| ------------------------ | --------- | ------------------------ |
| `NCM/Experiment/Prepare` | sub       | empty                    |
| `NCM/Experiment/Stop`    | sub       | empty                    |
| `NCM/Experiment/Rename`  | sub       | file name (plain string) |
| `NCM/Experiment/Start`   | pub       | empty                    |
| `NCM/MCU/DisplayData`    | pub       | `MCUData` JSON, 10 Hz    |

```json
{
    "Location": "LL",
    "Pressure": { "DP": 0.57, "CH1": 1013.28, "CH2": 1012.72 },
    "Velocity": 0.33
}
```

`DP`, `CH1`, `CH2` are averages of `diff_mbar`, `p_1_mbar`, `p_2_mbar` over the batch.
`Velocity` (m/s) is calculated from the **averaged** `diff_mbar`. All rounded to `display_decimals`.

Logs go to `logs/global/<client_name>` (via `GenericMqtteLogger`) and `<client_name>.log`.

## Velocity

```
err       = error_correction(|diff|)          # evaluated on |diff|: same correction both directions
corrected = |diff| - err
velocity  = sign(diff) * sqrt(2 * 100 * |corrected| / fluid_density)
```

## Config (`configs/<LOC>.json`)

| Key                     | Default           | Notes                                                                                                   |
| ----------------------- | ----------------- | ------------------------------------------------------------------------------------------------------- |
| `port`                  | `/dev/ttyACM0`    | use the udev symlink, e.g. `/dev/ncm-mcu-LL`                                                            |
| `baud_rate`             | 115200            | ignored by native USB, kept for completeness                                                            |
| `serial_read_timeout_s` | 0.1               | how often MQTT events are checked while waiting                                                         |
| `expected_location`     | null              | used to determine incorrect board enumeration. Mismatch with the MCU is logged (WARNING 4001); MCU wins |
| `batch_size`            | 20                | buffer size for averaging and publishing                                                                |
| `prepare_timeout_s`     | 60                | must be longer than the firmware's 30 s NTP timeout                                                     |
| `display_decimals`      | 2                 | rounding of published values                                                                            |
| `data_dir`              | `./data`          | relative to the project folder                                                                          |
| `default_file_name`     | `mcu`             | → `mcu_LL_20261028_142501.csv`                                                                          |
| `sample_decimals`       | 4                 | rounding of the per-sample velocity in the CSV                                                          |
| `sync_time_epoch`       | `unix`            | `ntp` if `server_reference_time_us` counts from 1900                                                    |
| `fluid_density_kg_m3`   | 1000              |                                                                                                         |
| `error_correction`      | quadratic         | see below                                                                                               |
| `mqtt_config`           | localhost:1883    |                                                                                                         |
| `client_name`           | `MCU-<port name>` | MQTT client ID and log name; must be unique per instance                                                |

### Error correction

The "type" field selects the correction function.
The config is validated at startup, so an unknown type or invalid parameters
(e.g. x not increasing, x/y of different lengths) stop the script before it connects.

```json
"error_correction": {"type": "polynomial", "coeffs": [0.0, 0.0666, 6e-6]}
```

`err(x) = c0 + c1·x + c2·x² + …` (ascending). Use for linear, quadratic and weighted
quadratic fits.

```json
"error_correction": {"type": "piecewise_linear", "x": [0, 50, 200], "y": [0, 3.5, 16.0]}
```

Linear interpolation between points; extended linearly past the ends. Use for a double
linear spline (3 points) or a calibration table. `x` is `|diff_mbar|`, so start it at 0.

## Output file

`<name>_<LOC>_<YYYYmmdd_HHMMSS>.csv` in `data_dir`. Lines starting with `#` are the header:

```
# location: LL
# port: /dev/ncm-mcu-LL
# ref1_mbar: 1013.251
# ref2_mbar: 1012.874
# pair_offset_mbar: 0.377
# server_reference_time_us: 1793210111932312
# sync_time_utc: 2026-10-28T17:55:11.932+00:00
# ... (batch size, density, error correction)
t_ms,t_unix_ms,p_1_mbar,p_2_mbar,diff_mbar,velocity_m_s
0,1793210111932.312,1013.0,1013.0,0.0,0.0
```

`t_unix_ms = server_reference_time_us / 1000 + t_ms`. Read with `pandas.read_csv(path, comment="#")`.

The timestamp is always added to the name so a run never overwrites an earlier file
(the DAQ reopens `<name>.tdms` with `"wb"` and overwrites it).

## Stable port names (udev)

To make sure boards

udev can't choose the `ttyACM` number, but it can add a fixed symlink per board serial number.
To make sure that a board is assigned a correct location in the software.
The board's USB Serial number must be assigned a simbolic name everytime the board is
connected to the NCM on startup.
Do the following to find the serial number of a board:

1. Disconnect all boards but the board you are trying to get the SN for
2. Run `udevadm info -a -n /dev/ttyACM0 | grep -m1 '{serial}'`
3. Copy the number

Then update the `udev` rules in `/etc/udev/rules.d/99-ncm-mcu.rules`.
Run:

```shell
sudoedit /etc/udev/rules.d/99-ncm-mcu.rules
```

Edit `ATTRS{serial}` attribute on the line corresponding to the location of the board.
Location is embedded into the symlink.

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="239a", ATTRS{serial}=="<SERIAL_LL>", SYMLINK+="ncm-mcu-LL"
SUBSYSTEM=="tty", ATTRS{idVendor}=="239a", ATTRS{serial}=="<SERIAL_LQ>", SYMLINK+="ncm-mcu-LQ"
SUBSYSTEM=="tty", ATTRS{idVendor}=="239a", ATTRS{serial}=="<SERIAL_RQ>", SYMLINK+="ncm-mcu-RQ"
SUBSYSTEM=="tty", ATTRS{idVendor}=="239a", ATTRS{serial}=="<SERIAL_RR>", SYMLINK+="ncm-mcu-RR"
```

Run `sudo udevadm control --reload && sudo udevadm trigger` to reload the daemon and
apply the updated rules.

The configs in `configs/` already point at these names.

## Testing without hardware

```bash
python3 tools/fake_mcu.py --link /tmp/fake_mcu --location LL          # also: --mode ntp_fail|no_ready|bad_lines
# in a config: "port": "/tmp/fake_mcu"
python3 main.py --config /tmp/test.json
```

## Error codes (log `error_code`)

| Code | Meaning                                   |
| ---- | ----------------------------------------- |
| 4001 | expected location ≠ MCU location          |
| 4002 | MCU not ready within `prepare_timeout_s`  |
| 4003 | `!` error line from the MCU               |
| 4004 | serial open/read failure (e.g. unplugged) |
| 4005 | malformed line / message                  |
| 4006 | CSV file error / bad rename               |
