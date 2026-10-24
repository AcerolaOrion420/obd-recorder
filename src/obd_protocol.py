import re
from typing import Dict, Any, Optional, Tuple, List, Callable

# Known ELM327 error strings
ELM_ERROR_STRINGS = [
    "NO DATA",
    "UNABLE TO CONNECT",
    "BUS INIT",
    "BUS ERROR",
    "STOPPED",
    "CAN ERROR",
    "BUFFER FULL",
    "?",
    "SEARCHING...",
    "FB ERROR",
    "DATA ERROR",
    "ERR",
]


class PIDDefinition:
    def __init__(self, pid_hex: str, name: str, unit: str, byte_count: int, decoder: Callable[[List[int]], Any]):
        self.pid_hex = pid_hex.upper().zfill(2)
        self.pid_int = int(self.pid_hex, 16)
        self.name = name
        self.unit = unit
        self.byte_count = byte_count
        self.decoder = decoder

    def decode(self, data_bytes: List[int]) -> Any:
        try:
            return self.decoder(data_bytes)
        except Exception:
            return None


# Canonical dictionary of PIDs supported on the Ford EcoSport
PID_DEFS: Dict[str, PIDDefinition] = {
    # Fast loop PIDs
    "0D": PIDDefinition("0D", "Vehicle Speed", "km/h", 1, lambda d: d[0]),
    "0C": PIDDefinition("0C", "Engine Speed", "rpm", 2, lambda d: int(((d[0] * 256) + d[1]) / 4)),
    "49": PIDDefinition("49", "Accelerator Pedal D", "%", 1, lambda d: round(d[0] * 100 / 255, 1)),
    "04": PIDDefinition("04", "Engine Load", "%", 1, lambda d: round(d[0] * 100 / 255, 1)),

    # Medium loop PIDs
    "05": PIDDefinition("05", "Coolant Temperature", "°C", 1, lambda d: d[0] - 40),
    "42": PIDDefinition("42", "Control Module Voltage", "V", 2, lambda d: round(((d[0] * 256) + d[1]) / 1000, 2)),
    "10": PIDDefinition("10", "Mass Air Flow Rate", "g/s", 2, lambda d: round(((d[0] * 256) + d[1]) / 100, 2)),
    "0B": PIDDefinition("0B", "Intake Manifold Pressure", "kPa", 1, lambda d: d[0]),
    "23": PIDDefinition("23", "Fuel Rail Gauge Pressure", "kPa", 2, lambda d: ((d[0] * 256) + d[1]) * 10),

    # Slow loop PIDs
    "0F": PIDDefinition("0F", "Intake Air Temperature", "°C", 1, lambda d: d[0] - 40),
    "46": PIDDefinition("46", "Ambient Air Temperature", "°C", 1, lambda d: d[0] - 40),
    "33": PIDDefinition("33", "Barometric Pressure", "kPa", 1, lambda d: d[0]),

    # Auxiliary / diagnostic
    "11": PIDDefinition("11", "Throttle Position", "%", 1, lambda d: round(d[0] * 100 / 255, 1)),
    "5C": PIDDefinition("5C", "Engine Oil Temperature", "°C", 1, lambda d: d[0] - 40),
}


def clean_elm_response(raw_resp: str) -> str:
    """Strip prompts, echoes, trailing carriage returns and newlines."""
    if not raw_resp:
        return ""
    # Strip prompt character
    cleaned = raw_resp.replace(">", "").strip()
    # Normalize carriage returns
    cleaned = cleaned.replace("\r", "\n")
    lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
    return "\n".join(lines)


def is_elm_error(resp_str: str) -> Tuple[bool, Optional[str]]:
    """Check if the ELM327 response indicates an error or no data."""
    cleaned = clean_elm_response(resp_str).upper()
    for err in ELM_ERROR_STRINGS:
        if err in cleaned:
            return True, err
    return False, None


def extract_can_hex_stream(cleaned_resp: str) -> str:
    """
    Extract continuous hex stream from single-line or multi-line ISO 15765-4 responses.
    Handles lines like '0: 41 0D 00 0C 0C 48' and '1: 49 14 04 00 00 00'.
    """
    hex_chars = []
    for line in cleaned_resp.split("\n"):
        line = line.strip()
        if not line:
            continue
        # If line has CAN frame sequence counter prefix (e.g. '0:', '1:')
        if ":" in line:
            parts = line.split(":", 1)
            line = parts[1]
        # Keep only hex characters
        clean_hex = re.sub(r"[^0-9A-Fa-f]", "", line)
        hex_chars.append(clean_hex)
    return "".join(hex_chars)


def parse_single_pid_response(pid_hex: str, raw_resp: str) -> Tuple[bool, Optional[str], Optional[Any], Optional[str]]:
    """
    Parse the response for a single PID request (e.g. '010C').
    Returns (success, raw_hex, decoded_value, error_message).
    """
    pid_hex = pid_hex.upper().zfill(2)
    cleaned = clean_elm_response(raw_resp)
    
    is_err, err_msg = is_elm_error(cleaned)
    if is_err:
        return False, None, None, err_msg

    hex_stream = extract_can_hex_stream(cleaned).upper()
    expected_header = f"41{pid_hex}"
    
    idx = hex_stream.find(expected_header)
    if idx == -1:
        return False, None, None, f"Missing header {expected_header} in {hex_stream}"

    pid_def = PID_DEFS.get(pid_hex)
    byte_count = pid_def.byte_count if pid_def else 1
    char_count = byte_count * 2

    data_start = idx + 4
    data_hex = hex_stream[data_start:data_start + char_count]
    
    if len(data_hex) < char_count:
        return False, None, None, f"Incomplete data hex: expected {char_count} chars, got '{data_hex}'"

    try:
        data_bytes = [int(data_hex[i:i + 2], 16) for i in range(0, char_count, 2)]
        val = pid_def.decode(data_bytes) if pid_def else None
        return True, data_hex, val, None
    except Exception as e:
        return False, data_hex, None, f"Decoding error: {e}"


def parse_multi_pid_response(pids: List[str], raw_resp: str) -> Tuple[bool, Dict[str, Tuple[str, Any]], Optional[str]]:
    """
    Parse combined multi-PID response (e.g. from '01 0D 0C 49 04').
    Returns (all_parsed, {pid: (raw_hex, value)}, error_message).
    """
    cleaned = clean_elm_response(raw_resp)
    is_err, err_msg = is_elm_error(cleaned)
    if is_err:
        return False, {}, err_msg

    hex_stream = extract_can_hex_stream(cleaned).upper()
    
    # Locate '41' response indicator
    idx_41 = hex_stream.find("41")
    if idx_41 == -1:
        return False, {}, f"No Mode 01 response header ('41') found in {hex_stream}"

    pos = idx_41 + 2
    results: Dict[str, Tuple[str, Any]] = {}
    
    # In standard multi-PID responses, each PID is echoed followed by its data bytes:
    # 41 <PID1> <DATA1> <PID2> <DATA2> ...
    # We walk the stream matching expected PIDs
    remaining_pids = [p.upper().zfill(2) for p in pids]
    
    while pos < len(hex_stream) and remaining_pids:
        # Check next 2 characters for expected next PID or any remaining PID
        found_pid = None
        for p in remaining_pids:
            if hex_stream[pos:pos + 2] == p:
                found_pid = p
                break
        
        if not found_pid:
            # Shift by 1 character in case of framing alignment
            pos += 1
            continue

        pos += 2  # Skip PID hex
        pid_def = PID_DEFS.get(found_pid)
        byte_count = pid_def.byte_count if pid_def else 1
        char_count = byte_count * 2

        if pos + char_count > len(hex_stream):
            break

        data_hex = hex_stream[pos:pos + char_count]
        pos += char_count

        try:
            data_bytes = [int(data_hex[i:i + 2], 16) for i in range(0, char_count, 2)]
            val = pid_def.decode(data_bytes) if pid_def else None
            results[found_pid] = (data_hex, val)
            remaining_pids.remove(found_pid)
        except Exception:
            pass

    all_found = len(remaining_pids) == 0
    return all_found, results, None if all_found else f"Missing PIDs: {remaining_pids}"
