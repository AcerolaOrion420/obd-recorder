#!/usr/bin/env python3
"""
Session Analysis & Visualization Tool
Parses a recorded session directory, prints statistical metrics, and generates
a dual-axis speed & RPM plot with event markers drawn onto a PNG image.
"""

import os
import sys
import json
import csv
import argparse
from typing import List, Dict, Any

try:
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive headless backend
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


def analyze_session(session_dir: str):
    if not os.path.isdir(session_dir):
        print(f"\033[91mError: Session folder '{session_dir}' does not exist.\033[0m")
        sys.exit(1)

    meta_file = os.path.join(session_dir, "meta.json")
    readings_file = os.path.join(session_dir, "readings.csv")
    markers_file = os.path.join(session_dir, "markers.csv")

    meta: Dict[str, Any] = {}
    if os.path.exists(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            print(f"Warning: could not parse meta.json: {e}")

    # Load readings.csv
    elapsed_list: List[float] = []
    speed_list: List[float] = []
    rpm_list: List[float] = []
    load_list: List[float] = []

    if not os.path.exists(readings_file):
        print(f"\033[91mError: readings.csv not found in '{session_dir}'.\033[0m")
        sys.exit(1)

    with open(readings_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                e = float(row.get("elapsed_s", 0.0))
                s = float(row.get("speed_kmh", 0.0))
                r = float(row.get("rpm", 0.0))
                l = float(row.get("engine_load_pct", 0.0)) if row.get("engine_load_pct") else 0.0
                elapsed_list.append(e)
                speed_list.append(s)
                rpm_list.append(r)
                load_list.append(l)
            except (ValueError, TypeError):
                continue

    # Load markers.csv
    markers: List[Dict[str, Any]] = []
    if os.path.exists(markers_file):
        with open(markers_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    markers.append({
                        "id": row.get("id"),
                        "elapsed_s": float(row.get("elapsed_s", 0.0)),
                        "label": row.get("label", "OTHER"),
                        "note": row.get("note", ""),
                        "speed": row.get("speed_kmh"),
                        "rpm": row.get("rpm"),
                    })
                except Exception:
                    continue

    if not elapsed_list:
        print(f"\033[93mWarning: readings.csv is empty or has no numeric data.\033[0m")
        return

    # Calculate statistics
    total_samples = len(elapsed_list)
    duration_s = elapsed_list[-1] - elapsed_list[0] if total_samples > 1 else 0.0
    mean_hz = total_samples / duration_s if duration_s > 0 else 0.0
    max_speed = max(speed_list) if speed_list else 0.0
    min_speed = min(speed_list) if speed_list else 0.0
    avg_speed = sum(speed_list) / len(speed_list) if speed_list else 0.0
    max_rpm = max(rpm_list) if rpm_list else 0.0
    avg_rpm = sum(rpm_list) / len(rpm_list) if rpm_list else 0.0

    # Count gaps > 2s
    gaps_over_2s = []
    for i in range(1, len(elapsed_list)):
        dt = elapsed_list[i] - elapsed_list[i - 1]
        if dt > 2.0:
            gaps_over_2s.append((elapsed_list[i - 1], elapsed_list[i], dt))

    # Marker breakdown
    marker_counts: Dict[str, int] = {}
    for m in markers:
        lbl = m["label"]
        marker_counts[lbl] = marker_counts.get(lbl, 0) + 1

    # Print Summary Table
    print("\n" + "=" * 62)
    print("  FLEETPULSE SESSION ANALYSIS REPORT")
    print("=" * 62)
    print(f"  Session Directory : {session_dir}")
    print(f"  Start Time (Wall) : {meta.get('start_time', 'N/A')}")
    print(f"  Duration          : {duration_s:.1f} s ({duration_s / 60.0:.2f} min)")
    print(f"  Total Fast Cycles : {total_samples}")
    print(f"  Average Sampling  : \033[1;32m{mean_hz:.1f} Hz\033[0m")
    print("-" * 62)
    print("  POWERTRAIN DYNAMICS:")
    print(f"    Speed (km/h)    : Min = {min_speed:.1f} | Avg = {avg_speed:.1f} | Max = \033[1;36m{max_speed:.1f}\033[0m")
    print(f"    Engine RPM      : Avg = {avg_rpm:.0f} | Max = \033[1;33m{max_rpm:.0f} RPM\033[0m")
    print(f"    Gaps > 2.0s     : {len(gaps_over_2s)} detected")
    for g in gaps_over_2s[:5]:
        print(f"      * Gap @ {g[0]:.1f}s -> {g[1]:.1f}s (duration: {g[2]:.2f}s)")
    if len(gaps_over_2s) > 5:
        print(f"      ... and {len(gaps_over_2s) - 5} more")
    print("-" * 62)
    print(f"  DRIVER EVENT MARKERS ({len(markers)} total):")
    if not markers:
        print("    (No markers recorded during this drive)")
    else:
        for lbl, cnt in sorted(marker_counts.items()):
            print(f"    {lbl:<20} : {cnt} events")
    print("=" * 62 + "\n")

    # Generate Visual Plot
    if MATPLOTLIB_AVAILABLE:
        plot_path = os.path.join(session_dir, "session_plot.png")
        print(f"Generating session plot at '{plot_path}'...")

        fig, ax1 = plt.subplots(figsize=(12, 6), dpi=140)

        # Style dark theme
        fig.patch.set_facecolor("#0b111e")
        ax1.set_facecolor("#111827")
        ax1.tick_params(colors="#94a3b8")
        ax1.xaxis.label.set_color("#cbd5e1")
        ax1.yaxis.label.set_color("#06b6d4")
        for spine in ax1.spines.values():
            spine.set_color("#1e293b")

        # Plot Speed on Left Axis
        ax1.set_xlabel("Elapsed Time (seconds)", fontsize=11, fontweight="bold")
        ax1.set_ylabel("Vehicle Speed (km/h)", color="#06b6d4", fontsize=11, fontweight="bold")
        line1, = ax1.plot(elapsed_list, speed_list, color="#06b6d4", linewidth=2.0, label="Speed (km/h)")
        ax1.set_ylim(0, max(100.0, max_speed * 1.15))
        ax1.grid(True, linestyle="--", alpha=0.25, color="#334155")

        # Plot RPM on Right Axis
        ax2 = ax1.twinx()
        ax2.set_ylabel("Engine Speed (RPM)", color="#f59e0b", fontsize=11, fontweight="bold")
        ax2.tick_params(colors="#94a3b8")
        ax2.yaxis.label.set_color("#f59e0b")
        for spine in ax2.spines.values():
            spine.set_color("#1e293b")
        line2, = ax2.plot(elapsed_list, rpm_list, color="#f59e0b", linewidth=1.5, alpha=0.85, label="RPM")
        ax2.set_ylim(0, max(4500.0, max_rpm * 1.15))

        # Color palette for event markers
        marker_colors = {
            "HARD BRAKE": "#ef4444",
            "HARD ACCEL": "#ea580c",
            "SMOOTH": "#10b981",
            "IDLE START": "#d97706",
            "IDLE END": "#3b82f6",
            "CONNECTION_LOST": "#dc2626",
            "CONNECTION_RESTORED": "#059669",
        }

        # Draw vertical lines for event markers
        for m in markers:
            x_val = m["elapsed_s"]
            lbl = m["label"]
            col = marker_colors.get(lbl, "#a855f7")
            ax1.axvline(x=x_val, color=col, linestyle="--", linewidth=1.4, alpha=0.9)
            # Add text callout near top
            ax1.text(
                x_val,
                ax1.get_ylim()[1] * 0.92,
                f" {lbl}",
                rotation=90,
                color=col,
                fontsize=8,
                fontweight="bold",
                verticalalignment="top",
            )

        title_str = f"FleetPulse Telemetry: {meta.get('session_name', 'Drive')} ({meta.get('vehicle', {}).get('name', 'Ford EcoSport')})"
        plt.title(title_str, color="#ffffff", fontsize=13, fontweight="bold", pad=14)

        # Combined legend
        lines = [line1, line2]
        labels = [l.get_label() for l in lines]
        ax1.legend(lines, labels, loc="upper left", facecolor="#1e293b", edgecolor="#334155", labelcolor="#f8fafc")

        plt.tight_layout()
        plt.savefig(plot_path, facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close()
        print(f"\033[1;32m✓ Plot saved successfully: {plot_path}\033[0m\n")
    else:
        print("Note: matplotlib not found; skipping plot generation.")


def main():
    parser = argparse.ArgumentParser(description="Analyze and visualize an OBD-II recording session.")
    parser.add_argument("session_dir", type=str, help="Path to session directory (e.g. sessions/20261020_170000_run)")
    args = parser.parse_args()

    analyze_session(args.session_dir)


if __name__ == "__main__":
    main()
