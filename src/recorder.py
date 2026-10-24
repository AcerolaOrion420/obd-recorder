import os
import json
import csv
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger("recorder")


def now_iso() -> str:
    """Current timestamp in ISO 8601 with millisecond precision."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class SessionRecorder:
    """
    Manages crash-safe logging of OBD-II data to disk.
    Periodically fsyncs open file descriptors every 1.0 second.
    """

    READINGS_COLUMNS = [
        "elapsed_s",
        "t_wall",
        "t_mono",
        # Fast loop
        "speed_kmh",
        "rpm",
        "accel_pedal_pct",
        "engine_load_pct",
        # Medium loop & ages
        "coolant_temp_c",
        "coolant_age_s",
        "module_voltage_v",
        "voltage_age_s",
        "maf_gps",
        "maf_age_s",
        "map_kpa",
        "map_age_s",
        "fuel_rail_kpa",
        "fuel_rail_age_s",
        # Slow loop & ages
        "iat_c",
        "iat_age_s",
        "ambient_c",
        "ambient_age_s",
        "baro_kpa",
        "baro_age_s",
    ]

    MARKER_COLUMNS = [
        "id",
        "t_wall",
        "t_mono",
        "elapsed_s",
        "label",
        "note",
        "speed_kmh",
        "rpm",
    ]

    def __init__(self, output_dir: str = "sessions", flush_interval_s: float = 1.0):
        self.output_dir = output_dir
        self.flush_interval_s = flush_interval_s
        self.is_recording = False
        self.session_dir: Optional[str] = None
        self.session_name = ""
        self.start_wall_iso = ""
        self.start_mono = 0.0

        # File handles
        self.raw_file = None
        self.readings_file = None
        self.readings_csv_writer = None
        self.markers_file = None
        self.markers_csv_writer = None

        # Tracking metrics
        self.total_raw_readings = 0
        self.total_cycles = 0
        self.cycle_intervals: List[float] = []
        self.last_cycle_mono = 0.0
        self.marker_id_counter = 0
        self.markers_list: List[dict] = []
        self.gaps_over_2s: List[dict] = []

        # Lock & sync thread (RLock to allow re-entrant locking if start_session stops existing session)
        self.lock = threading.RLock()
        self.sync_thread: Optional[threading.Thread] = None
        self.stop_sync_event = threading.Event()

        # Session metadata template
        self.meta_data: Dict[str, Any] = {}

    def start_session(
        self,
        name: str = "drive",
        vehicle_info: Optional[dict] = None,
        adapter_info: Optional[dict] = None,
        polling_config: Optional[dict] = None,
        benchmark_results: Optional[dict] = None,
    ) -> str:
        """Create a new session folder and open all crash-safe file descriptors."""
        with self.lock:
            if self.is_recording:
                self.stop_session()

            now_dt = datetime.now()
            clean_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name.strip()) or "drive"
            folder_name = f"{now_dt.strftime('%Y%m%d_%H%M%S')}_{clean_name}"
            self.session_dir = os.path.join(self.output_dir, folder_name)
            os.makedirs(self.session_dir, exist_ok=True)

            self.session_name = clean_name
            self.start_wall_iso = now_iso()
            self.start_mono = time.monotonic()
            self.last_cycle_mono = self.start_mono
            self.total_raw_readings = 0
            self.total_cycles = 0
            self.cycle_intervals.clear()
            self.marker_id_counter = 0
            self.markers_list.clear()
            self.gaps_over_2s.clear()

            # 1. raw.jsonl
            raw_path = os.path.join(self.session_dir, "raw.jsonl")
            self.raw_file = open(raw_path, "a", encoding="utf-8")

            # 2. readings.csv
            readings_path = os.path.join(self.session_dir, "readings.csv")
            self.readings_file = open(readings_path, "w", newline="", encoding="utf-8")
            self.readings_csv_writer = csv.DictWriter(self.readings_file, fieldnames=self.READINGS_COLUMNS)
            self.readings_csv_writer.writeheader()
            self.readings_file.flush()
            os.fsync(self.readings_file.fileno())

            # 3. markers.csv
            markers_path = os.path.join(self.session_dir, "markers.csv")
            self.markers_file = open(markers_path, "w", newline="", encoding="utf-8")
            self.markers_csv_writer = csv.DictWriter(self.markers_file, fieldnames=self.MARKER_COLUMNS)
            self.markers_csv_writer.writeheader()
            self.markers_file.flush()
            os.fsync(self.markers_file.fileno())

            # 4. meta.json
            self.meta_data = {
                "session_id": folder_name,
                "session_name": clean_name,
                "start_time": self.start_wall_iso,
                "end_time": None,
                "duration_seconds": 0.0,
                "vehicle": vehicle_info or {},
                "adapter": adapter_info or {},
                "polling_config": polling_config or {},
                "benchmark": benchmark_results or {},
                "metrics": {
                    "total_raw_readings": 0,
                    "total_fast_cycles": 0,
                    "mean_fast_loop_hz": 0.0,
                    "min_fast_loop_hz": 0.0,
                    "max_fast_loop_hz": 0.0,
                    "gaps_over_2s_count": 0,
                    "gaps_over_2s_list": [],
                    "marker_count": 0,
                },
            }
            self._save_meta_json()

            self.is_recording = True

            # Start background fsync worker
            self.stop_sync_event.clear()
            self.sync_thread = threading.Thread(target=self._fsync_worker, daemon=True)
            self.sync_thread.start()

            logger.info(f"Started recording session in {self.session_dir}")
            return self.session_dir

    def record_raw(self, pid: str, raw_hex: str, value: Any, unit: str):
        """Append one raw PID reading to raw.jsonl."""
        if not self.is_recording or not self.raw_file:
            return

        now_mono = time.monotonic()
        entry = {
            "t_wall": now_iso(),
            "t_mono": round(now_mono, 4),
            "pid": f"0x{pid.upper()}",
            "raw_hex": raw_hex,
            "value": value,
            "unit": unit,
        }
        with self.lock:
            try:
                self.raw_file.write(json.dumps(entry) + "\n")
                self.total_raw_readings += 1
            except Exception as e:
                logger.error(f"Error writing to raw.jsonl: {e}")

    def record_fast_cycle(self, row: dict):
        """Write one synchronized cycle row to readings.csv."""
        if not self.is_recording or not self.readings_csv_writer:
            return

        now_mono = time.monotonic()
        with self.lock:
            dt = now_mono - self.last_cycle_mono
            if self.total_cycles > 0:
                self.cycle_intervals.append(dt)
                if dt > 2.0:
                    gap_info = {
                        "t_wall": now_iso(),
                        "t_mono": round(now_mono, 3),
                        "duration_s": round(dt, 3),
                    }
                    self.gaps_over_2s.append(gap_info)
                    logger.warning(f"Telemetry gap >2s detected: {dt:.2f}s")

            self.last_cycle_mono = now_mono
            self.total_cycles += 1

            try:
                # Ensure all defined columns are present
                clean_row = {col: row.get(col, "") for col in self.READINGS_COLUMNS}
                self.readings_csv_writer.writerow(clean_row)
            except Exception as e:
                logger.error(f"Error writing to readings.csv: {e}")

    def add_marker(self, label: str, note: str = "", speed: Optional[float] = None, rpm: Optional[float] = None) -> dict:
        """Add an event marker to markers.csv and return marker entry."""
        now_mono = time.monotonic()
        elapsed = now_mono - self.start_mono if self.is_recording else 0.0

        self.marker_id_counter += 1
        entry = {
            "id": self.marker_id_counter,
            "t_wall": now_iso(),
            "t_mono": round(now_mono, 4),
            "elapsed_s": round(elapsed, 2),
            "label": label.upper(),
            "note": note.strip(),
            "speed_kmh": round(speed, 1) if speed is not None else "",
            "rpm": int(rpm) if rpm is not None else "",
        }

        with self.lock:
            self.markers_list.append(entry)
            if self.is_recording and self.markers_csv_writer:
                try:
                    self.markers_csv_writer.writerow(entry)
                    self.markers_file.flush()
                    os.fsync(self.markers_file.fileno())
                except Exception as e:
                    logger.error(f"Error writing marker: {e}")

        logger.info(f"Marker recorded #{entry['id']}: {entry['label']} @ {entry['elapsed_s']}s (Speed={speed}, RPM={rpm})")
        return entry

    def undo_last_marker(self) -> Optional[dict]:
        """Undo the most recent marker and rewrite markers.csv safely."""
        with self.lock:
            if not self.markers_list:
                return None
            removed = self.markers_list.pop()
            # Rewrite markers.csv cleanly
            if self.is_recording and self.session_dir:
                try:
                    markers_path = os.path.join(self.session_dir, "markers.csv")
                    self.markers_file.close()
                    with open(markers_path, "w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=self.MARKER_COLUMNS)
                        writer.writeheader()
                        for m in self.markers_list:
                            writer.writerow(m)
                        f.flush()
                        os.fsync(f.fileno())
                    # Reopen in append mode
                    self.markers_file = open(markers_path, "a", newline="", encoding="utf-8")
                    self.markers_csv_writer = csv.DictWriter(self.markers_file, fieldnames=self.MARKER_COLUMNS)
                except Exception as e:
                    logger.error(f"Error rewriting markers.csv on undo: {e}")

            logger.info(f"Undid marker #{removed['id']}: {removed['label']}")
            return removed

    def get_stats(self) -> dict:
        """Current session runtime stats for Web UI."""
        if not self.is_recording:
            metrics = self.meta_data.get("metrics", {})
            return {
                "is_recording": False,
                "session_name": self.session_name,
                "session_dir": self.session_dir,
                "elapsed_s": self.meta_data.get("duration_seconds", 0.0),
                "total_cycles": metrics.get("total_fast_cycles", self.total_cycles),
                "total_readings": metrics.get("total_raw_readings", self.total_raw_readings),
                "file_size_kb": 0.0,
                "hz": metrics.get("mean_fast_loop_hz", 0.0),
                "marker_count": len(self.markers_list),
                "last_marker": self.markers_list[-1] if self.markers_list else None,
            }

        elapsed = time.monotonic() - self.start_mono
        size_bytes = 0
        if self.session_dir and os.path.exists(self.session_dir):
            for fname in ("raw.jsonl", "readings.csv", "markers.csv"):
                fpath = os.path.join(self.session_dir, fname)
                if os.path.exists(fpath):
                    size_bytes += os.path.getsize(fpath)

        # Calculate current Hz over last 20 intervals
        recent_intervals = self.cycle_intervals[-20:] if self.cycle_intervals else []
        hz = 1.0 / (sum(recent_intervals) / len(recent_intervals)) if recent_intervals and sum(recent_intervals) > 0 else 0.0

        return {
            "is_recording": True,
            "session_name": self.session_name,
            "session_dir": self.session_dir,
            "elapsed_s": round(elapsed, 1),
            "total_cycles": self.total_cycles,
            "total_readings": self.total_raw_readings,
            "file_size_kb": round(size_bytes / 1024, 1),
            "hz": round(hz, 2),
            "marker_count": len(self.markers_list),
            "last_marker": self.markers_list[-1] if self.markers_list else None,
        }

    def _fsync_worker(self):
        """Dedicated background loop that flushes and fsyncs files every second."""
        while not self.stop_sync_event.is_set():
            time.sleep(self.flush_interval_s)
            with self.lock:
                if not self.is_recording:
                    break
                try:
                    if self.raw_file and not self.raw_file.closed:
                        self.raw_file.flush()
                        os.fsync(self.raw_file.fileno())
                    if self.readings_file and not self.readings_file.closed:
                        self.readings_file.flush()
                        os.fsync(self.readings_file.fileno())
                    if self.markers_file and not self.markers_file.closed:
                        self.markers_file.flush()
                        os.fsync(self.markers_file.fileno())
                except Exception as e:
                    logger.debug(f"Fsync tick exception: {e}")

    def _save_meta_json(self):
        """Write meta.json file atomically."""
        if not self.session_dir:
            return
        meta_path = os.path.join(self.session_dir, "meta.json")
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(self.meta_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            logger.error(f"Error saving meta.json: {e}")

    def stop_session(self):
        """Finalize all files, calculate summary metrics, write meta.json, and close."""
        with self.lock:
            if not self.is_recording:
                return

            self.is_recording = False
            self.stop_sync_event.set()

            duration = time.monotonic() - self.start_mono
            end_wall_iso = now_iso()

            # Calculate frequency metrics
            rates = [1.0 / dt for dt in self.cycle_intervals if dt > 0]
            mean_hz = sum(rates) / len(rates) if rates else 0.0
            min_hz = min(rates) if rates else 0.0
            max_hz = max(rates) if rates else 0.0

            # Update meta.json
            self.meta_data["end_time"] = end_wall_iso
            self.meta_data["duration_seconds"] = round(duration, 2)
            self.meta_data["metrics"]["total_raw_readings"] = self.total_raw_readings
            self.meta_data["metrics"]["total_fast_cycles"] = self.total_cycles
            self.meta_data["metrics"]["mean_fast_loop_hz"] = round(mean_hz, 2)
            self.meta_data["metrics"]["min_fast_loop_hz"] = round(min_hz, 2)
            self.meta_data["metrics"]["max_fast_loop_hz"] = round(max_hz, 2)
            self.meta_data["metrics"]["gaps_over_2s_count"] = len(self.gaps_over_2s)
            self.meta_data["metrics"]["gaps_over_2s_list"] = self.gaps_over_2s
            self.meta_data["metrics"]["marker_count"] = len(self.markers_list)

            self._save_meta_json()

            # Close open files
            for f in (self.raw_file, self.readings_file, self.markers_file):
                if f and not f.closed:
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                        f.close()
                    except Exception:
                        pass

            self.raw_file = None
            self.readings_file = None
            self.markers_file = None

            logger.info(f"Finalized session {self.session_name}: {self.total_cycles} cycles in {duration:.1f}s (mean {mean_hz:.1f} Hz)")
