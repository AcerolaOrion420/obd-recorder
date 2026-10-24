import time
import threading
import logging
from typing import Dict, Any, Optional, List, Callable
from .obd_protocol import (
    PID_DEFS,
    parse_single_pid_response,
    parse_multi_pid_response,
)
from .obd_connection import ConnectionState

logger = logging.getLogger("scheduler")


class TieredScheduler:
    """
    Tiered OBD-II polling engine.
    Executes fast-loop queries continuously, interleaves medium-loop and slow-loop
    PIDs via round-robin, benchmarks multi-PID batch support on connect,
    and feeds live telemetry to recorder and web clients.
    """

    def __init__(
        self,
        connection,
        recorder,
        config: dict,
        on_telemetry: Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[dict], None]] = None,
    ):
        self.conn = connection
        self.recorder = recorder
        self.config = config
        self.on_telemetry = on_telemetry
        self.on_status = on_status

        # Polling configuration
        poll_cfg = config.get("polling", {})
        self.fast_pids: List[str] = [p.upper().zfill(2) for p in poll_cfg.get("fast", ["0D", "0C", "49", "04"])]
        self.medium_pids: List[str] = [p.upper().zfill(2) for p in poll_cfg.get("medium", ["05", "42", "10", "0B", "23"])]
        self.slow_pids: List[str] = [p.upper().zfill(2) for p in poll_cfg.get("slow", ["0F", "46", "33"])]

        self.medium_interval_s = float(poll_cfg.get("medium_interval_s", 2.5))
        self.slow_interval_s = float(poll_cfg.get("slow_interval_s", 30.0))
        self.batch_fast_if_supported = bool(poll_cfg.get("batch_fast_if_supported", True))

        # Runtime telemetry cache {pid: {"val": ..., "mono": ...}}
        self.cache: Dict[str, Dict[str, Any]] = {}
        for p in self.fast_pids + self.medium_pids + self.slow_pids:
            self.cache[p] = {"val": None, "mono": 0.0}

        # Benchmark results
        self.benchmark_results: Dict[str, Any] = {
            "single_pid_latencies_ms": [],
            "single_pid_mean_ms": 0.0,
            "multi_pid_tested": False,
            "multi_pid_supported": False,
            "fast_loop_mode": "single",
            "achieved_fast_hz": 0.0,
        }

        # Scheduling state
        self.medium_idx = 0
        self.slow_idx = 0
        self.last_medium_time = 0.0
        self.last_slow_time = 0.0
        self.is_running = False
        self.worker_thread: Optional[threading.Thread] = None

        # Disconnect & gap tracking
        self.was_connected = False
        self.disconnect_time_mono = 0.0
        self.no_data_start_mono = 0.0

        # Auto settings
        rec_cfg = config.get("recording", {})
        self.auto_start_recording_on_connect = rec_cfg.get("auto_start_recording_on_connect", False)
        self.auto_stop_minutes = float(rec_cfg.get("auto_stop_minutes", 0))
        self.keep_armed = rec_cfg.get("keep_armed", False)

    def start(self):
        """Start the background polling worker thread."""
        if self.is_running:
            return
        self.is_running = True
        self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.worker_thread.start()
        logger.info("TieredScheduler started.")

    def stop(self):
        """Stop polling worker."""
        self.is_running = False
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)
        logger.info("TieredScheduler stopped.")

    def run_startup_benchmark(self) -> dict:
        """
        Run startup benchmark on connect:
        1. Measure round-trip times of single PIDs.
        2. Test multi-PID request e.g. 010D0C4904.
        """
        logger.info("Running startup OBD-II round-trip & multi-PID benchmark...")
        latencies = []

        # 1. Measure 4 single PID roundtrips
        for _ in range(4):
            t0 = time.monotonic()
            success, resp = self.conn.query("010C\r")
            dt = (time.monotonic() - t0) * 1000.0
            if success and not resp.startswith("?"):
                latencies.append(dt)

        mean_ms = sum(latencies) / len(latencies) if latencies else 200.0
        self.benchmark_results["single_pid_latencies_ms"] = [round(l, 1) for l in latencies]
        self.benchmark_results["single_pid_mean_ms"] = round(mean_ms, 1)

        # 2. Test multi-PID batch request if configured
        multi_supported = False
        if self.batch_fast_if_supported:
            batch_cmd = "01" + "".join(self.fast_pids) + "\r"
            logger.info(f"Testing multi-PID batch query: '{batch_cmd.strip()}'...")
            t0 = time.monotonic()
            success, resp = self.conn.query(batch_cmd)
            dt = (time.monotonic() - t0) * 1000.0

            if success:
                all_found, results, err = parse_multi_pid_response(self.fast_pids, resp)
                if all_found:
                    multi_supported = True
                    logger.info(f"Multi-PID batching SUPPORTED! Roundtrip: {dt:.1f}ms. Results: {list(results.keys())}")
                else:
                    logger.info(f"Multi-PID batching not supported or incomplete ({err}). Falling back to single PIDs.")
            else:
                logger.info("Multi-PID query returned error. Falling back to single PIDs.")

        self.benchmark_results["multi_pid_tested"] = True
        self.benchmark_results["multi_pid_supported"] = multi_supported
        self.benchmark_results["fast_loop_mode"] = "batch" if multi_supported else "single"
        
        # Estimate theoretical fast-loop Hz
        if multi_supported:
            self.benchmark_results["achieved_fast_hz"] = round(1000.0 / (mean_ms * 1.5), 1)
        else:
            self.benchmark_results["achieved_fast_hz"] = round(1000.0 / (mean_ms * len(self.fast_pids)), 1)

        logger.info(f"Benchmark finished: mode={self.benchmark_results['fast_loop_mode']}, target Hz ~ {self.benchmark_results['achieved_fast_hz']}")
        return self.benchmark_results

    def _run_loop(self):
        """Main polling thread loop."""
        benchmarked_this_connection = False

        while self.is_running:
            # Check connection state
            if self.conn.state == ConnectionState.CONNECTED:
                # If we just connected
                if not self.was_connected:
                    self.was_connected = True
                    logger.info("Connection established in scheduler.")
                    
                    # Log gap restoration if we were recording
                    if self.disconnect_time_mono > 0 and self.recorder.is_recording:
                        gap_dur = round(time.monotonic() - self.disconnect_time_mono, 1)
                        self.recorder.add_marker(
                            label="CONNECTION_RESTORED",
                            note=f"Link restored after {gap_dur}s drop",
                            speed=self.cache.get("0D", {}).get("val"),
                            rpm=self.cache.get("0C", {}).get("val"),
                        )
                    self.disconnect_time_mono = 0.0

                    # Run benchmark once per connection
                    if not benchmarked_this_connection:
                        try:
                            self.run_startup_benchmark()
                            benchmarked_this_connection = True
                        except Exception as e:
                            logger.error(f"Error during benchmark: {e}")

                    # Auto-start recording if configured
                    if self.auto_start_recording_on_connect and not self.recorder.is_recording:
                        session_dir = self.recorder.start_session(
                            name="auto_drive",
                            vehicle_info=self.config.get("vehicle"),
                            adapter_info={"address": self.conn.address, "channel": self.conn.channel},
                            polling_config=self.config.get("polling"),
                            benchmark_results=self.benchmark_results,
                        )
                        logger.info(f"Auto-started recording session in {session_dir}")

                # Execute one full cycle
                self._execute_cycle()

            elif self.conn.state in (ConnectionState.DISCONNECTED, ConnectionState.RECONNECTING, ConnectionState.WAKING, ConnectionState.CONNECTING):
                benchmarked_this_connection = False
                if self.was_connected:
                    self.was_connected = False
                    self.disconnect_time_mono = time.monotonic()
                    logger.warning("Connection lost in scheduler.")
                    if self.recorder.is_recording:
                        self.recorder.add_marker(
                            label="CONNECTION_LOST",
                            note="Bluetooth connection lost; attempting reconnect",
                            speed=self.cache.get("0D", {}).get("val"),
                            rpm=self.cache.get("0C", {}).get("val"),
                        )

                # Attempt auto-reconnect if we were previously connected or in reconnecting state
                if self.conn.state == ConnectionState.RECONNECTING:
                    backoff = getattr(self.conn, "backoff_s", 1.0)
                    time.sleep(backoff)
                    logger.info(f"Attempting auto-reconnect (backoff={backoff:.1f}s)...")
                    try:
                        ok = self.conn.connect()
                        if not ok:
                            self.conn.backoff_s = min(15.0, backoff * 1.5)
                        else:
                            self.conn.backoff_s = 1.0
                    except Exception as e:
                        logger.error(f"Error during auto-reconnect attempt: {e}")
                else:
                    time.sleep(0.5)

            elif self.conn.state == ConnectionState.ECU_NOT_RESPONDING:
                # Handle ignition off / ECU no data
                time.sleep(1.0)
                # Check auto-stop timer
                if self.no_data_start_mono == 0.0:
                    self.no_data_start_mono = time.monotonic()
                elif self.auto_stop_minutes > 0 and (time.monotonic() - self.no_data_start_mono) > (self.auto_stop_minutes * 60):
                    if self.recorder.is_recording:
                        logger.warning(f"Auto-stopping recording after {self.auto_stop_minutes}m of ECU NO DATA.")
                        self.recorder.stop_session()

            time.sleep(0.005)

    def _execute_cycle(self):
        """Execute one fast-loop cycle, plus round-robin medium and slow queries."""
        now_mono = time.monotonic()
        cycle_start_wall = time.time()

        # 1. Fast Loop Queries
        if self.benchmark_results.get("multi_pid_supported") and len(self.fast_pids) > 1:
            # Batch query
            batch_cmd = "01" + "".join(self.fast_pids) + "\r"
            success, raw_resp = self.conn.query(batch_cmd)
            if success:
                all_found, results, err = parse_multi_pid_response(self.fast_pids, raw_resp)
                for pid, (raw_hex, val) in results.items():
                    if val is not None:
                        self.cache[pid] = {"val": val, "mono": now_mono}
                        p_def = PID_DEFS.get(pid)
                        unit = p_def.unit if p_def else ""
                        self.recorder.record_raw(pid, raw_hex, val, unit)
        else:
            # Single-PID queries
            for pid in self.fast_pids:
                cmd = f"01{pid}\r"
                success, raw_resp = self.conn.query(cmd)
                if success:
                    ok, raw_hex, val, err = parse_single_pid_response(pid, raw_resp)
                    if ok and val is not None:
                        self.cache[pid] = {"val": val, "mono": now_mono}
                        p_def = PID_DEFS.get(pid)
                        unit = p_def.unit if p_def else ""
                        self.recorder.record_raw(pid, raw_hex or "", val, unit)

        # 2. Interleave one Medium PID if due
        if self.medium_pids:
            slot_s = self.medium_interval_s / max(1, len(self.medium_pids))
            if now_mono - self.last_medium_time >= slot_s:
                pid = self.medium_pids[self.medium_idx % len(self.medium_pids)]
                self.medium_idx += 1
                self.last_medium_time = now_mono
                success, raw_resp = self.conn.query(f"01{pid}\r")
                if success:
                    ok, raw_hex, val, err = parse_single_pid_response(pid, raw_resp)
                    if ok and val is not None:
                        self.cache[pid] = {"val": val, "mono": now_mono}
                        p_def = PID_DEFS.get(pid)
                        unit = p_def.unit if p_def else ""
                        self.recorder.record_raw(pid, raw_hex or "", val, unit)

        # 3. Interleave one Slow PID if due
        if self.slow_pids:
            slot_s = self.slow_interval_s / max(1, len(self.slow_pids))
            if now_mono - self.last_slow_time >= slot_s:
                pid = self.slow_pids[self.slow_idx % len(self.slow_pids)]
                self.slow_idx += 1
                self.last_slow_time = now_mono
                success, raw_resp = self.conn.query(f"01{pid}\r")
                if success:
                    ok, raw_hex, val, err = parse_single_pid_response(pid, raw_resp)
                    if ok and val is not None:
                        self.cache[pid] = {"val": val, "mono": now_mono}
                        p_def = PID_DEFS.get(pid)
                        unit = p_def.unit if p_def else ""
                        self.recorder.record_raw(pid, raw_hex or "", val, unit)

        # 4. Assemble synchronized fast cycle row
        now_mono2 = time.monotonic()
        from .recorder import now_iso
        elapsed_s = now_mono2 - self.recorder.start_mono if self.recorder.is_recording else 0.0

        def get_val_and_age(pid: str) -> tuple:
            entry = self.cache.get(pid, {})
            val = entry.get("val")
            m = entry.get("mono", 0.0)
            age = round(now_mono2 - m, 1) if m > 0 else 999.9
            return val, age

        coolant, c_age = get_val_and_age("05")
        voltage, v_age = get_val_and_age("42")
        maf, maf_age = get_val_and_age("10")
        map_p, map_age = get_val_and_age("0B")
        fuel_rail, fr_age = get_val_and_age("23")
        iat, iat_age = get_val_and_age("0F")
        ambient, amb_age = get_val_and_age("46")
        baro, baro_age = get_val_and_age("33")

        cycle_row = {
            "elapsed_s": round(elapsed_s, 3),
            "t_wall": now_iso(),
            "t_mono": round(now_mono2, 4),
            # Fast loop
            "speed_kmh": self.cache.get("0D", {}).get("val", 0) or 0,
            "rpm": self.cache.get("0C", {}).get("val", 0) or 0,
            "accel_pedal_pct": self.cache.get("49", {}).get("val", 0.0) or 0.0,
            "engine_load_pct": self.cache.get("04", {}).get("val", 0.0) or 0.0,
            # Medium loop
            "coolant_temp_c": coolant if coolant is not None else "",
            "coolant_age_s": c_age,
            "module_voltage_v": voltage if voltage is not None else "",
            "voltage_age_s": v_age,
            "maf_gps": maf if maf is not None else "",
            "maf_age_s": maf_age,
            "map_kpa": map_p if map_p is not None else "",
            "map_age_s": map_age,
            "fuel_rail_kpa": fuel_rail if fuel_rail is not None else "",
            "fuel_rail_age_s": fr_age,
            # Slow loop
            "iat_c": iat if iat is not None else "",
            "iat_age_s": iat_age,
            "ambient_c": ambient if ambient is not None else "",
            "ambient_age_s": amb_age,
            "baro_kpa": baro if baro is not None else "",
            "baro_age_s": baro_age,
        }

        # Write cycle to readings.csv
        self.recorder.record_fast_cycle(cycle_row)

        # Broadcast live telemetry update
        if self.on_telemetry:
            try:
                self.on_telemetry(cycle_row)
            except Exception as e:
                logger.error(f"Error in on_telemetry callback: {e}")
