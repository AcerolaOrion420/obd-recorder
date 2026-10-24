# FleetPulse — Standalone OBD-II Data Recorder

A crash-safe, mobile-first OBD-II telematics data recorder with an offline web control interface. Specifically engineered to capture high-rate real-world driving data from a **Ford EcoSport 1.5 TDCi** (India OBD II / IOBD-II, ISO 15765-4 CAN 11-bit 500k) via a generic **ELM327 v2.1 Bluetooth adapter**.

The captured telemetry is synchronized and indexed with high-precision driver event markers (harsh braking, rapid acceleration, idling, over-revving) for training automated driver-behavior scoring algorithms.

---

## 1. Features

* **Zero Cloud / Offline First**: Served directly by the local FastAPI backend with vanilla HTML5/CSS3/JavaScript. No npm, no build steps, and zero external CDN dependencies.
* **Mobile-First Touch UI (360px+)**: Designed for glanceless use on a mounted smartphone over a hotspot, with extra-large touch targets (74px+ event buttons), high-contrast dark theme, and Screen Wake Lock API.
* **Tiered Polling Engine**:
  * **Fast loop**: Speed (`0D`), RPM (`0C`), Accel Pedal (`49`), Engine Load (`04`) continuously queried as fast as the adapter allows.
  * **Medium loop**: Coolant (`05`), Voltage (`42`), MAF (`10`), MAP (`0B`), Fuel Rail (`23`) interleaved round-robin every ~2.5s.
  * **Slow loop**: Intake Air Temp (`0F`), Ambient (`46`), Barometric (`33`) interleaved round-robin every ~30s.
  * **Startup Benchmark**: Measures round-trip latency and tests multi-PID CAN batching on connect.
* **Crash-Safe Storage**: Every session writes to an independent directory with open file descriptors flushed and `os.fsync()`-ed every 1.0 second. A laptop lid close or abrupt power drop loses at most ~1s of data.
* **Link Resilience**: Automatic exponential reconnect backoff (1s to 15s) with ELM327 wake-up handshake. Disconnects log automatic `CONNECTION_LOST` and `CONNECTION_RESTORED` markers without stopping the active recording session.
* **Event Marker Pipeline**: Instant one-tap tagging with audible WebAudio synthesis and keyboard shortcuts (1–6) for `HARD BRAKE`, `HARD ACCEL`, `SMOOTH`, `IDLE START`, `IDLE END`, and `OTHER...` (with custom text notes). Full one-tap UNDO support.
* **Simulation & Replay**: Native `--mock` mode with realistic EcoSport powertrain physics and `--replay <session_dir>` mode to test without a car.

---

## 2. Hardware Quirks & Linux Bluetooth Setup

### Adapter Details
* **Address**: `01:23:45:67:89:BA` (Configurable in `config.yaml`)
* **PIN**: `1234`
* **RFCOMM Channel**: **Channel 2** *(Standard tools that query Channel 1 will fail with `ConnectionRefusedError`)*.

### Arch Linux Permissions & Pairing
Ensure your user belongs to the `rfkill` and `uucp` groups:
```bash
sudo usermod -aG rfkill,uucp $USER
```

Verify Bluetooth is unblocked:
```bash
rfkill unblock bluetooth
sudo systemctl enable --now bluetooth
```

Pair with the ELM327 adapter (one-time setup):
```bash
bluetoothctl
[bluetooth]# power on
[bluetooth]# default-agent
[bluetooth]# scan on
# Wait until 01:23:45:67:89:BA appears
[bluetooth]# pair 01:23:45:67:89:BA
# When prompted for PIN, enter: 1234
[bluetooth]# trust 01:23:45:67:89:BA
[bluetooth]# quit
```

> **Why the Wakeup Handshake Matters**: The ELM327 sleeps when idle. FleetPulse automatically executes an asynchronous `bluetoothctl connect <addr>` ping for 3.5 seconds before binding the raw RFCOMM Channel 2 socket. Without this, Linux returns `OSError: [Errno 112] Host is down`.

---

## 3. Installation & Quickstart

```bash
cd /home/omy-archy/obd-recorder

# Python 3 virtual environment
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Running with Real Car
```bash
./venv/bin/python main.py
```

### Testing with Realistic Mock Simulator
```bash
./venv/bin/python main.py --mock
```

### Replaying a Past Recorded Drive
```bash
./venv/bin/python main.py --replay sessions/20261020_170000_drive
```

When started, FleetPulse detects your network interfaces and prints clear URLs:
```text
====================================================================
  FLEETPULSE — STANDALONE OBD-II TELEMETRY RECORDER
====================================================================
  Operating Mode : LIVE BLUETOOTH ADAPTER
  Target Vehicle : Ford EcoSport 1.5 TDCi
  Hardware Target: 01:23:45:67:89:BA (RFCOMM Ch 2)
--------------------------------------------------------------------
  Local Web UI   : http://localhost:8000
  Phone / Hotspot: http://192.168.0.224:8000
====================================================================
```

Open the printed IP URL on your mobile phone browser while connected to the same Wi-Fi or laptop hotspot.

---

## 4. Recording Output Structure

Each session creates a self-contained timestamped folder: `sessions/<YYYYMMDD_HHMMSS>_<name>/`

```text
sessions/20261020_173000_highway_test/
├── raw.jsonl       # Every single PID reading as it arrives with millisecond timestamps
├── readings.csv    # Synchronized fast-loop cycle rows with carried-forward values & ages
├── markers.csv     # Event markers with exact timestamp, speed, RPM, and notes
├── meta.json       # Session metrics, vehicle info, sampling rate, benchmark, and gap counts
└── session_plot.png # (Generated by analyze_session.py) Dual-axis plot with marker lines
```

### `raw.jsonl` Schema
```json
{"t_wall": "2026-10-20T17:30:02.124+05:30", "t_mono": 142.341, "pid": "0x0C", "raw_hex": "1AF8", "value": 1726, "unit": "rpm"}
{"t_wall": "2026-10-20T17:30:02.148+05:30", "t_mono": 142.365, "pid": "0x0D", "raw_hex": "32", "value": 50, "unit": "km/h"}
```

### `readings.csv` Headers
* `elapsed_s`, `t_wall`, `t_mono`
* `speed_kmh`, `rpm`, `accel_pedal_pct`, `engine_load_pct` (Fast loop)
* `coolant_temp_c`, `coolant_age_s`
* `module_voltage_v`, `voltage_age_s`
* `maf_gps`, `maf_age_s`
* `map_kpa`, `map_age_s`
* `fuel_rail_kpa`, `fuel_rail_age_s`
* `iat_c`, `iat_age_s`, `ambient_c`, `ambient_age_s`, `baro_kpa`, `baro_age_s`

### `markers.csv` Headers
`id, t_wall, t_mono, elapsed_s, label, note, speed_kmh, rpm`

---

## 5. Session Analysis & Visualization

Run `analyze_session.py` on any completed session folder:
```bash
./venv/bin/python analyze_session.py sessions/20261020_173000_highway_test
```

This outputs:
1. **Summary Table**: Duration, total fast cycles, average sampling frequency (Hz), min/avg/max speed and RPM, and a list of all gaps exceeding 2.0s.
2. **Event Marker Tally**: Breakdown of driver events tagged during the drive.
3. **High-Resolution PNG Plot**: Dual-axis graph of Speed (Cyan) and RPM (Amber) over time, with color-coded vertical dashed lines and callout labels for every tagged event.
