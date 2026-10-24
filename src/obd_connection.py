import socket
import subprocess
import time
import logging
from typing import Optional, Tuple, Callable

logger = logging.getLogger("obd_connection")


class ConnectionState:
    DISCONNECTED = "Disconnected"
    WAKING = "Waking adapter"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    ECU_NOT_RESPONDING = "ECU not responding"
    RECONNECTING = "Reconnecting"


class OBDConnection:
    """
    Manages low-level Bluetooth RFCOMM socket connection to the ELM327 adapter.
    Handles device wakeup via bluetoothctl, AT initialization, read buffering,
    exponential reconnect backoff, and distinct ECU state detection.
    """

    INIT_COMMANDS = [
        b"ATZ\r",
        b"ATE0\r",
        b"ATL0\r",
        b"ATS0\r",
        b"ATH0\r",
        b"ATSP0\r",
        b"ATAT2\r",
    ]

    def __init__(
        self,
        address: str = "01:23:45:67:89:BA",
        channel: int = 2,
        timeout_s: float = 2.0,
        wakeup_hold_s: float = 3.5,
        on_state_change: Optional[Callable[[str, str], None]] = None,
    ):
        self.address = address
        self.channel = channel
        self.timeout_s = timeout_s
        self.wakeup_hold_s = wakeup_hold_s
        self.on_state_change = on_state_change

        self.state = ConnectionState.DISCONNECTED
        self.sock: Optional[socket.socket] = None
        self.reconnect_count = 0
        self.error_count = 0
        self.consecutive_no_data = 0
        self.last_successful_time = time.time()
        self.should_run = False
        self.backoff_s = 1.0

    def set_state(self, new_state: str, details: str = ""):
        old_state = self.state
        if old_state != new_state:
            self.state = new_state
            logger.info(f"State transition: {old_state} -> {new_state} ({details})")
            if self.on_state_change:
                try:
                    self.on_state_change(old_state, new_state)
                except Exception as e:
                    logger.error(f"Error in on_state_change callback: {e}")

    def wake_adapter(self) -> bool:
        """
        Run bluetoothctl connect <address> to wake adapter from low-power dormant state.
        Without this, direct socket connection raises OSError 112 'Host is down'.
        """
        self.set_state(ConnectionState.WAKING, f"Pinging {self.address} via bluetoothctl")
        logger.info(f"Waking adapter via bluetoothctl on {self.address} (holding {self.wakeup_hold_s}s)...")
        try:
            proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            proc.stdin.write(f"connect {self.address}\n")
            proc.stdin.flush()
            time.sleep(self.wakeup_hold_s)
            proc.stdin.write("quit\n")
            proc.stdin.flush()
            try:
                proc.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            time.sleep(0.5)
            return True
        except Exception as e:
            logger.warning(f"Wakeup via bluetoothctl failed or timed out: {e}")
            return False

    def connect(self) -> bool:
        """Execute full connect sequence: wake adapter -> open RFCOMM -> run AT init."""
        self.close_socket()
        self.wake_adapter()

        self.set_state(ConnectionState.CONNECTING, f"Opening RFCOMM channel {self.channel}")
        try:
            logger.info(f"Opening RFCOMM socket to {self.address} channel {self.channel}...")
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            sock.settimeout(self.timeout_s + 4.0)
            sock.connect((self.address, self.channel))
            self.sock = sock
            logger.info("RFCOMM socket connected. Running AT initialization sequence...")

            # Run AT sequence
            for cmd in self.INIT_COMMANDS:
                resp = self._raw_send_receive(cmd, delay_after=0.1)
                logger.debug(f"Init {cmd.strip().decode(errors='replace')}: {resp}")

            self.set_state(ConnectionState.CONNECTED, "Initialization complete")
            self.consecutive_no_data = 0
            self.backoff_s = 1.0
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.error_count += 1
            self.close_socket()
            return False

    def close_socket(self):
        """Cleanly close RFCOMM socket."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def disconnect(self):
        """Explicitly disconnect adapter and reset state."""
        self.should_run = False
        self.close_socket()
        self.set_state(ConnectionState.DISCONNECTED, "User disconnect")

    def _raw_send_receive(self, cmd: bytes, delay_after: float = 0.05) -> str:
        """
        Send command and buffer received bytes until the '>' prompt is seen.
        """
        if not self.sock:
            raise ConnectionError("Socket not open")

        self.sock.send(cmd)
        if delay_after > 0:
            time.sleep(delay_after)

        buffer = bytearray()
        self.sock.settimeout(self.timeout_s)
        start_time = time.time()

        while True:
            try:
                chunk = self.sock.recv(1024)
                if not chunk:
                    raise ConnectionError("Socket EOF (adapter disconnected)")
                buffer.extend(chunk)
                if b">" in chunk:
                    break
            except socket.timeout:
                # If we read something before timeout, return what we have
                if buffer:
                    break
                raise TimeoutError("Socket timeout waiting for response")

            if time.time() - start_time > (self.timeout_s + 3.0):
                break

        return buffer.decode("ascii", errors="replace").strip()

    def query(self, cmd_str: str) -> Tuple[bool, str]:
        """
        Send OBD command string (e.g. '010C\\r') and return (success, response_string).
        Handles communication errors, tracks reconnects, and updates state.
        """
        if not self.sock or self.state not in (ConnectionState.CONNECTED, ConnectionState.ECU_NOT_RESPONDING):
            return False, "Not connected"

        cmd_bytes = cmd_str.encode("ascii") if isinstance(cmd_str, str) else cmd_str
        if not cmd_bytes.endswith(b"\r"):
            cmd_bytes += b"\r"

        try:
            resp = self._raw_send_receive(cmd_bytes)
            # Check for ECU responsiveness
            from .obd_protocol import is_elm_error
            is_err, err_msg = is_elm_error(resp)

            if is_err and err_msg in ("NO DATA", "UNABLE TO CONNECT", "BUS INIT"):
                self.consecutive_no_data += 1
                if self.consecutive_no_data >= 3:
                    self.set_state(ConnectionState.ECU_NOT_RESPONDING, f"Received repeated {err_msg}")
            else:
                self.consecutive_no_data = 0
                if self.state == ConnectionState.ECU_NOT_RESPONDING:
                    self.set_state(ConnectionState.CONNECTED, "ECU resumed responding")

            self.last_successful_time = time.time()
            return True, resp
        except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning(f"Bluetooth connection error during query: {e}")
            self.error_count += 1
            self.close_socket()
            self.set_state(ConnectionState.RECONNECTING, str(e))
            return False, str(e)
        except Exception as e:
            logger.warning(f"Unexpected query error: {e}")
            self.error_count += 1
            return False, str(e)
