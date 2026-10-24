import os
import yaml
from typing import Dict, Any, List

DEFAULT_CONFIG: Dict[str, Any] = {
    "adapter": {
        "address": "01:23:45:67:89:BA",
        "channel": 2,
        "pin": "1234",
        "timeout_s": 2.0,
        "wakeup_hold_s": 3.5,
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
    },
    "recording": {
        "output_dir": "sessions",
        "flush_interval_s": 1.0,
        "auto_connect_on_start": False,
        "auto_start_recording_on_connect": False,
        "auto_stop_minutes": 0,
        "keep_armed": False,
    },
    "polling": {
        "fast": ["0D", "0C", "49", "04"],
        "medium": ["05", "42", "10", "0B", "23"],
        "medium_interval_s": 2.5,
        "slow": ["0F", "46", "33"],
        "slow_interval_s": 30.0,
        "batch_fast_if_supported": True,
    },
    "vehicle": {
        "name": "Ford EcoSport 1.5 TDCi",
        "vin": "MAJAXXMRKAFE35942",
        "protocol": "ISO 15765-4 CAN 11-bit 500k",
    }
}


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load configuration from YAML file with default fallbacks."""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
                # Deep merge top-level sections
                for sec, vals in user_config.items():
                    if isinstance(vals, dict) and sec in config:
                        config[sec].update(vals)
                    else:
                        config[sec] = vals
        except Exception as e:
            print(f"[Config] Error reading {config_path}: {e}. Using defaults.")
    return config
