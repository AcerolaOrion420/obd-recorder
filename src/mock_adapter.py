import time
import math
import logging
from typing import Optional, Tuple, Callable
from .obd_connection import ConnectionState

logger = logging.getLogger("mock_adapter")


class MockOBDAdapter:
    """
    Simulates a Ford EcoSport 1.5 TDCi connected via ELM327.
    Emulates realistic powertrain physics and responds to both single-PID
    and multi-PID queries at realistic microsecond speeds.
    """

    def __init__(self, on_state_change: Optional[Callable[[str, str], None]] = None):
        self.on_state_change = on_state_change
        self.state = ConnectionState.DISCONNECTED
        self.start_mono = time.monotonic()
        self.reconnect_count = 0
        self.error_count = 0
        self.simulated_disconnect_duration = 0.0
        self.simulated_disconnect_until = 0.0

    def set_state(self, new_state: str, details: str = ""):
        old_state = self.state
        if old_state != new_state:
            self.state = new_state
            logger.info(f"[Mock] State: {old_state} -> {new_state} ({details})")
            if self.on_state_change:
                try:
                    self.on_state_change(old_state, new_state)
                except Exception as e:
                    logger.error(f"[Mock] Error in state change callback: {e}")

    def connect(self) -> bool:
        now = time.monotonic()
        if now < self.simulated_disconnect_until:
            return False
        self.set_state(ConnectionState.WAKING, "Mock adapter wakeup")
        time.sleep(0.2)
        self.set_state(ConnectionState.CONNECTING, "Mock RFCOMM handshake")
        time.sleep(0.1)
        self.reconnect_count += 1
        self.set_state(ConnectionState.CONNECTED, "Mock ELM327 v2.1 Ready")
        return True

    def disconnect(self):
        self.set_state(ConnectionState.DISCONNECTED, "User disconnect")

    def trigger_temporary_disconnect(self, duration_s: float = 3.5):
        """Force a temporary connection drop to test reconnect and gap markers."""
        logger.warning(f"[Mock] Simulating connection drop for {duration_s} seconds...")
        self.simulated_disconnect_until = time.monotonic() + duration_s
        self.set_state(ConnectionState.RECONNECTING, "Simulated Bluetooth Link Drop")

    def get_simulated_values(self) -> dict:
        """Calculate continuous realistic powertrain dynamics based on elapsed time."""
        t = time.monotonic() - self.start_mono
        
        # 180-second driving loop
        cycle_t = t % 180.0
        
        # Default baseline
        speed = 0.0
        rpm = 780.0
        pedal = 7.8
        load = 15.0
        coolant = min(88.0, 27.0 + (t / 180.0) * 61.0)
        voltage = 13.9 + math.sin(t * 0.1) * 0.15
        map_press = 89.0
        maf_rate = 8.2
        fuel_rail = 32000.0  # kPa
        iat = 27.0
        ambient = 25.0
        baro = 90.0

        if cycle_t < 10.0:
            # Idle at start
            rpm = 780.0 + math.sin(cycle_t * 2) * 12
            speed = 0.0
            pedal = 7.8
            load = 14.0 + math.sin(cycle_t) * 1.5
            maf_rate = 8.1
            fuel_rail = 33000.0
        elif cycle_t < 45.0:
            # Acceleration up to 80 km/h with 5 simulated gear shifts
            prog = (cycle_t - 10.0) / 35.0
            speed = prog * 80.0
            pedal = 25.0 + math.sin(cycle_t * 3) * 5.0
            load = 55.0 + math.sin(cycle_t * 2) * 10.0
            gear = min(5, max(1, int(1 + speed / 16.0)))
            rpm = 1200.0 + ((speed % 16.0) / 16.0) * 1800.0
            map_press = 110.0 + (prog * 35.0)
            maf_rate = 14.0 + (prog * 25.0)
            fuel_rail = 50000.0 + (prog * 35000.0)
        elif cycle_t < 80.0:
            # Cruising at ~80 km/h
            speed = 80.0 + math.sin(cycle_t * 0.3) * 3.0
            rpm = 2100.0 + math.sin(cycle_t * 0.3) * 80.0
            pedal = 20.0 + math.sin(cycle_t * 0.5) * 2.0
            load = 34.0 + math.sin(cycle_t * 0.5) * 4.0
            map_press = 115.0
            maf_rate = 22.0
            fuel_rail = 60000.0
        elif cycle_t < 95.0:
            # Harsh braking down to 0
            prog = (cycle_t - 80.0) / 15.0
            speed = max(0.0, 80.0 * (1.0 - prog))
            pedal = 0.0
            load = 8.0
            rpm = 850.0 + speed * 12.0
            map_press = 88.0
            maf_rate = 7.5
            fuel_rail = 31000.0
        elif cycle_t < 130.0:
            # Idling at red light
            speed = 0.0
            rpm = 778.0 + math.sin(cycle_t * 2) * 10.0
            pedal = 7.8
            load = 14.0
            fuel_rail = 32500.0
        else:
            # Smooth city driving at 40 km/h
            prog = (cycle_t - 130.0) / 50.0
            speed = 40.0 + math.sin(cycle_t * 0.5) * 5.0
            rpm = 1600.0 + math.sin(cycle_t * 0.5) * 50.0
            pedal = 15.0
            load = 28.0
            fuel_rail = 45000.0

        return {
            "0D": int(max(0, speed)),
            "0C": int(max(0, rpm)),
            "49": round(max(0.0, min(100.0, pedal)), 1),
            "04": round(max(0.0, min(100.0, load)), 1),
            "05": int(coolant),
            "42": round(voltage, 2),
            "10": round(maf_rate, 2),
            "0B": int(map_press),
            "23": int(fuel_rail),
            "0F": int(iat),
            "46": int(ambient),
            "33": int(baro),
        }

    def query(self, cmd_str: str) -> Tuple[bool, str]:
        """Respond to AT commands or Mode 01 queries with valid hex payloads."""
        now = time.monotonic()
        if now < self.simulated_disconnect_until:
            return False, "Simulated Link Drop"
        elif self.state == ConnectionState.RECONNECTING:
            # Auto-restored
            self.reconnect_count += 1
            self.set_state(ConnectionState.CONNECTED, "Simulated Link Restored")

        if self.state != ConnectionState.CONNECTED:
            return False, "Not connected"

        # Simulate small Bluetooth transmission latency (15ms)
        time.sleep(0.015)

        cmd = cmd_str.strip().upper()

        if cmd.startswith("AT"):
            if cmd in ("ATZ", "ATI"):
                return True, "ELM327 v2.1\r\n>"
            elif cmd == "ATDP":
                return True, "ISO 15765-4 (CAN 11/500)\r\n>"
            elif cmd == "ATRV":
                return True, "13.9V\r\n>"
            return True, "OK\r\n>"

        if not cmd.startswith("01"):
            return False, "?\r\n>"

        sim = self.get_simulated_values()
        pid_requests = []
        body = cmd[2:]
        for i in range(0, len(body), 2):
            p = body[i:i + 2]
            if len(p) == 2:
                pid_requests.append(p)

        # Single PID request
        if len(pid_requests) == 1:
            p = pid_requests[0]
            hex_data = self._encode_pid(p, sim)
            if hex_data:
                return True, f"41 {p} {hex_data}\r\n>"
            return False, "NO DATA\r\n>"

        # Multi-PID request (batch)
        parts = ["41"]
        for p in pid_requests:
            hex_data = self._encode_pid(p, sim)
            if hex_data:
                parts.append(p)
                parts.append(hex_data)
        
        return True, " ".join(parts) + "\r\n>"

    def _encode_pid(self, p: str, sim: dict) -> Optional[str]:
        if p not in sim:
            return None
        val = sim[p]
        if p == "0D":   # Speed (1 byte)
            return f"{int(val):02X}"
        elif p == "0C": # RPM: val = ((A*256)+B)/4 -> raw = val * 4
            raw = int(val * 4)
            A = (raw >> 8) & 0xFF
            B = raw & 0xFF
            return f"{A:02X} {B:02X}"
        elif p == "49": # Pedal: val = A * 100 / 255 -> A = int(val * 255 / 100)
            A = int(val * 255 / 100)
            return f"{A:02X}"
        elif p == "04": # Load: val = A * 100 / 255
            A = int(val * 255 / 100)
            return f"{A:02X}"
        elif p == "05": # Coolant: val = A - 40 -> A = val + 40
            A = int(val + 40)
            return f"{A:02X}"
        elif p == "42": # Voltage: val = raw / 1000 -> raw = val * 1000
            raw = int(val * 1000)
            A = (raw >> 8) & 0xFF
            B = raw & 0xFF
            return f"{A:02X} {B:02X}"
        elif p == "10": # MAF: val = raw / 100 -> raw = val * 100
            raw = int(val * 100)
            A = (raw >> 8) & 0xFF
            B = raw & 0xFF
            return f"{A:02X} {B:02X}"
        elif p == "0B": # MAP: val = A
            return f"{int(val):02X}"
        elif p == "23": # Fuel rail: val = raw * 10 -> raw = val / 10
            raw = int(val / 10)
            A = (raw >> 8) & 0xFF
            B = raw & 0xFF
            return f"{A:02X} {B:02X}"
        elif p in ("0F", "46"): # Temp = A - 40
            A = int(val + 40)
            return f"{A:02X}"
        elif p == "33": # Baro = A
            return f"{int(val):02X}"
        return None
