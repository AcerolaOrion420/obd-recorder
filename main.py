#!/usr/bin/env python3
"""
FleetPulse Standalone OBD-II Data Recorder
Entrypoint for the recorder web server and data capture daemon.
"""

import os
import sys
import argparse
import socket
import logging
import uvicorn

# Ensure package directory is on path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.config import load_config
from src.obd_connection import OBDConnection
from src.mock_adapter import MockOBDAdapter
from src.replay_adapter import ReplayOBDAdapter
from src.recorder import SessionRecorder
from src.scheduler import TieredScheduler
from src.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def get_local_ip_addresses() -> list:
    """Find all valid LAN IP addresses on the machine to display in the banner."""
    ips = []
    try:
        # Probe primary route without sending actual packets
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        primary_ip = s.getsockname()[0]
        s.close()
        if primary_ip and primary_ip not in ips and not primary_ip.startswith("127."):
            ips.append(primary_ip)
    except Exception:
        pass

    # Read from network interfaces if possible
    try:
        import subprocess
        out = subprocess.check_output(["ip", "-4", "addr", "show"], text=True)
        import re
        for match in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/\d+", out):
            ip = match.group(1)
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    return ips


def main():
    parser = argparse.ArgumentParser(description="FleetPulse Standalone OBD-II Data Recorder")
    parser.add_argument("--mock", action="store_true", help="Run with simulated vehicle telemetry")
    parser.add_argument("--replay", type=str, default="", help="Replay previously recorded session directory")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--host", type=str, default="", help="Server bind host (overrides config)")
    parser.add_argument("--port", type=int, default=0, help="Server bind port (overrides config)")
    args = parser.parse_args()

    config = load_config(args.config)

    host = args.host or config.get("server", {}).get("host", "0.0.0.0")
    port = args.port or config.get("server", {}).get("port", 8000)
    output_dir = config.get("recording", {}).get("output_dir", "sessions")
    adapter_cfg = config.get("adapter", {})

    mode_label = "LIVE BLUETOOTH ADAPTER"
    conn = None

    if args.mock:
        mode_label = "MOCK VEHICLE SIMULATOR"
        conn = MockOBDAdapter()
    elif args.replay:
        mode_label = f"REPLAY MODE ({args.replay})"
        conn = ReplayOBDAdapter(session_dir=args.replay)
    else:
        conn = OBDConnection(
            address=adapter_cfg.get("address", "01:23:45:67:89:BA"),
            channel=int(adapter_cfg.get("channel", 2)),
            timeout_s=float(adapter_cfg.get("timeout_s", 2.0)),
            wakeup_hold_s=float(adapter_cfg.get("wakeup_hold_s", 3.5)),
        )

    # Initialize recorder and scheduler
    recorder = SessionRecorder(
        output_dir=output_dir,
        flush_interval_s=float(config.get("recording", {}).get("flush_interval_s", 1.0)),
    )

    scheduler = TieredScheduler(
        connection=conn,
        recorder=recorder,
        config=config,
    )

    # Build FastAPI web app
    app = create_app(conn, recorder, scheduler, config, is_mock=args.mock)

    # Start background polling scheduler
    scheduler.start()

    # Auto-connect if configured or in mock/replay mode
    if args.mock or args.replay or config.get("recording", {}).get("auto_connect_on_start", False):
        import threading
        threading.Thread(target=conn.connect, daemon=True).start()

    # Print startup banner
    lan_ips = get_local_ip_addresses()
    print("\n" + "=" * 68)
    print("  FLEETPULSE — STANDALONE OBD-II TELEMETRY RECORDER")
    print("=" * 68)
    print(f"  Operating Mode : \033[1;36m{mode_label}\033[0m")
    print(f"  Target Vehicle : {config.get('vehicle', {}).get('name', 'Ford EcoSport')}")
    print(f"  Hardware Target: {adapter_cfg.get('address')} (RFCOMM Ch {adapter_cfg.get('channel')})")
    print("-" * 68)
    print(f"  Local Web UI   : \033[1;32mhttp://localhost:{port}\033[0m")
    for ip in lan_ips:
        print(f"  Phone / Hotspot: \033[1;33mhttp://{ip}:{port}\033[0m")
    print("=" * 68 + "\n")

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        logger.info("Shutting down recorder...")
        scheduler.stop()
        if recorder.is_recording:
            recorder.stop_session()
        conn.disconnect()


if __name__ == "__main__":
    main()
