#!/usr/bin/env python3
"""
ComEd Hourly Data Collector
============================
Called by controller.py once per hour to append a new row to
prices_weather.csv with the current hour's price and weather.

Designed to be lightweight — runs in under 2 seconds, uses no
extra memory when not running.

Usage (called from controller.py):
    from collect_data import record_hour
    record_hour(hour_avg_price)

Or standalone:
    python3 collect_data.py --price 4.5
"""

import os
import csv
import requests
import argparse
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

LAT         = 41.7421
LON         = -88.2456
OUTPUT_DIR  = "/config/comed_ml"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "prices_weather.csv")
TRACKER_FILE = os.path.join(OUTPUT_DIR, "last_recorded_hour.txt")

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

CSV_HEADERS = [
    "timestamp",
    "price_cents",
    "temp_c",
    "humidity",
    "dewpoint_c",
    "hour",
    "day_of_week",
    "month",
    "is_weekend",
    "is_summer",
]

# ── Weather fetch (current conditions) ───────────────────────────────────────

def fetch_current_weather() -> dict:
    """Fetch current weather for Aurora IL from Open-Meteo forecast API."""
    params = {
        "latitude":  LAT,
        "longitude": LON,
        "current": "temperature_2m,relative_humidity_2m,dew_point_2m",
        "timezone": "America/Chicago",
    }
    try:
        resp = requests.get(WEATHER_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        current = data["current"]
        return {
            "temp_c":     round(current["temperature_2m"], 2),
            "humidity":   round(current["relative_humidity_2m"], 1),
            "dewpoint_c": round(current["dew_point_2m"], 2),
        }
    except Exception as e:
        print(f"[collect_data] Weather fetch failed: {e}")
        return {}


# ── Main record function ──────────────────────────────────────────────────────

def record_hour(price_cents: float) -> bool:
    """
    Record one hourly data point. Called once per hour.
    Returns True if a new row was written, False if already recorded this hour.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now = datetime.now()
    ts_str = now.strftime("%Y-%m-%d %H:00")

    # Check if we already recorded this hour
    if os.path.exists(TRACKER_FILE):
        with open(TRACKER_FILE, "r") as f:
            last = f.read().strip()
        if last == ts_str:
            return False  # already recorded this hour

    # Fetch weather
    wx = fetch_current_weather()

    # Build row
    row = {
        "timestamp":   ts_str,
        "price_cents": round(price_cents, 4),
        "temp_c":      wx.get("temp_c", ""),
        "humidity":    wx.get("humidity", ""),
        "dewpoint_c":  wx.get("dewpoint_c", ""),
        "hour":        now.hour,
        "day_of_week": now.weekday(),
        "month":       now.month,
        "is_weekend":  1 if now.weekday() >= 5 else 0,
        "is_summer":   1 if now.month in (6, 7, 8, 9) else 0,
    }

    # Write header if file doesn't exist
    write_header = not os.path.exists(OUTPUT_FILE)

    with open(OUTPUT_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    # Update tracker
    with open(TRACKER_FILE, "w") as f:
        f.write(ts_str)

    print(f"[collect_data] Recorded: {ts_str} | {price_cents}¢ | {wx.get('temp_c', '?')}°C")
    return True


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--price", type=float, required=True,
                        help="Current hour average price in cents/kWh")
    args = parser.parse_args()
    record_hour(args.price)