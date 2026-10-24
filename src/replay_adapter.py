import json
import time
import os
import logging
from typing import Optional, Tuple, Callable, List
from .obd_connection import ConnectionState

logger = logging.getLogger("replay_adapter")


class ReplayOBDAdapter:
    """
    Replays previously recorded session data from raw.jsonl at original playback speed.
    """

    def __init__(self, session_dir: str, on_state_change: Optional[Callable[[str, str], None]] = None):
        self.session_dir = session_dir
        self.raw_path = os.path.join(session_dir, "raw.jsonl")
        self.on_state_change = on_state_change
        self.state = ConnectionState.DISCONNECTED
        self.entries: List[dict] = []
        self.entry_idx = 0
        self.start_wall = 0.0
        self.first_entry_time = 0.0
        self.reconnect_count = 0
        self.error_count = 0

        self._load_data()

    def _load_data(self):
        if not os.path.exists(self.raw_path):
            raise FileNotFoundError(f"Missing raw.jsonl in replay directory: {self.session_dir}")

        with open(self.raw_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        self.entries.append(json.loads(line))
                    except Exception:
                        pass
        logger.info(f"Loaded {len(self.entries)} entries for replay from {self.raw_path}")

    def set_state(self, new_state: str, details: str = ""):
        old_state = self.state
        if old_state != new_state:
            self.state = new_state
            logger.info(f"[Replay] State: {old_state} -> {new_state} ({details})")
            if self.on_state_change:
                try:
                    self.on_state_change(old_state, new_state)
                except Exception as e:
                    logger.error(f"[Replay] State change callback error: {e}")

    def connect(self) -> bool:
        self.set_state(ConnectionState.CONNECTING, "Loading replay file")
        time.sleep(0.2)
        self.set_state(ConnectionState.CONNECTED, f"Replaying {len(self.entries)} records")
        self.entry_idx = 0
        self.start_wall = time.time()
        if self.entries:
            self.first_entry_time = self.entries[0].get("t_mono", 0.0)
        return True

    def disconnect(self):
        self.set_state(ConnectionState.DISCONNECTED, "Replay stopped")

    def query(self, cmd_str: str) -> Tuple[bool, str]:
        if self.state != ConnectionState.CONNECTED or not self.entries:
            return False, "Not connected"

        cmd = cmd_str.strip().upper()
        if cmd.startswith("AT"):
            return True, "OK\r\n>"

        if self.entry_idx >= len(self.entries):
            # Loop replay
            self.entry_idx = 0
            self.start_wall = time.time()

        entry = self.entries[self.entry_idx]
        self.entry_idx += 1

        pid = entry.get("pid", "").replace("0x", "")
        raw_hex = entry.get("raw_hex", "")
        
        # Emulate reply
        return True, f"41 {pid} {raw_hex}\r\n>"
