#!/usr/bin/env python3
"""
ComEd Log Parser — extract historical prices from controller.log
=================================================================
Parses controller.log to extract hourly average prices, pairs each
hour with matching weather from Open-Meteo, and writes prices_weather.csv.

Run once to bootstrap the training dataset from existing log data.
After this, collect_data.py takes over for ongoing collection.

Usage:
    python3 parse_log.py
"""

import re
import os
import csv
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────

LAT         = 41.7421
LON         = -88.2456
LOG_FILE    = "/config/comed_ecobee/controller.log"
OUTPUT_DIR  = "/config/comed_ml"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "prices_weather.csv")

WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"

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

# Matches lines like:
# 2026-05-26 22:04:33,707 INFO 5-min price: 5.50¢ | Hour avg: 8.30¢
PRICE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):\d{2}:\d{2},\d+ INFO 5-min price: [\d.]+[¢c] \| Hour avg: ([\d.]+)[¢c]"
)

# ── Step 1: Parse log → collect hourly readings ───────────────────────────────

def parse_log(log_file: str) -> dict:
    """
    Parse controller.log and collect all hour-avg prices.
    For each calendar hour, keep the LAST reading (most accurate —
    hour avg converges as the hour progresses).
    Returns dict of {"YYYY-MM-DD HH:00": price_cents}
    """
    hourly = {}  # "YYYY-MM-DD HH:00" -> list of prices seen that hour

    if not os.path.exists(log_file):
        print(f"ERROR: Log file not found at {log_file}")
        return {}

    with open(log_file, "r", errors="replace") as f:
        for line in f:
            m = PRICE_RE.match(line.strip())
            if not m:
                continue
            date_str = m.group(1)   # "2026-05-26"
            hour_str = m.group(2)   # "22"
            price    = float(m.group(3))
            key = f"{date_str} {hour_str}:00"
            if key not in hourly:
                hourly[key] = []
            hourly[key].append(price)

    # Use the last reading per hour (most accurate hour avg)
    result = {}
    for key, prices in hourly.items():
        result[key] = round(prices[-1], 4)

    print(f"Parsed {len(result)} unique hours from log")
    return result


# ── Step 2: Fetch matching weather ────────────────────────────────────────────

def fetch_weather(start_date: str, end_date: str) -> dict:
    """
    Fetch hourly weather from Open-Meteo archive for the given date range.
    Returns dict of {"YYYY-MM-DD HH:00": {temp_c, humidity, dewpoint_c}}
    """
    params = {
        "latitude":  LAT,
        "longitude": LON,
        "start_date": start_date,
        "end_date":   end_date,
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m",
        "timezone": "America/Chicago",
    }
    try:
        resp = requests.get(WEATHER_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Weather fetch failed: {e}")
        return {}

    weather = {}
    times      = data["hourly"]["time"]
    temps      = data["hourly"]["temperature_2m"]
    humidities = data["hourly"]["relative_humidity_2m"]
    dewpoints  = data["hourly"]["dew_point_2m"]

    for i, ts in enumerate(times):
        key = ts.replace("T", " ")  # "2026-05-26 22:00"
        weather[key] = {
            "temp_c":     round(temps[i], 2)      if temps[i]      is not None else None,
            "humidity":   round(humidities[i], 1) if humidities[i] is not None else None,
            "dewpoint_c": round(dewpoints[i], 2)  if dewpoints[i]  is not None else None,
        }

    print(f"Fetched {len(weather)} hourly weather readings")
    return weather


# ── Step 3: Merge and write CSV ───────────────────────────────────────────────

def build_row(ts_str: str, price: float, wx: dict) -> dict:
    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
    return {
        "timestamp":   ts_str,
        "price_cents": price,
        "temp_c":      wx.get("temp_c", ""),
        "humidity":    wx.get("humidity", ""),
        "dewpoint_c":  wx.get("dewpoint_c", ""),
        "hour":        dt.hour,
        "day_of_week": dt.weekday(),
        "month":       dt.month,
        "is_weekend":  1 if dt.weekday() >= 5 else 0,
        "is_summer":   1 if dt.month in (6, 7, 8, 9) else 0,
    }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Parse log
    print("Step 1: Parsing controller.log...")
    prices = parse_log(LOG_FILE)

    if not prices:
        print("No price data found in log. Exiting.")
        return

    # Find date range
    timestamps = sorted(prices.keys())
    start_date = timestamps[0][:10]   # "2026-05-26"
    end_date   = timestamps[-1][:10]  # "2026-06-04"
    print(f"Date range: {start_date} → {end_date}")

    # Fetch weather for that exact range
    print("Step 2: Fetching matching weather data...")
    weather = fetch_weather(start_date, end_date)

    # Merge
    print("Step 3: Merging and writing CSV...")
    rows = []
    missing_weather = 0
    for ts in timestamps:
        wx = weather.get(ts, {})
        if not wx:
            missing_weather += 1
        rows.append(build_row(ts, prices[ts], wx))

    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    print(f"\nDone!")
    print(f"  Hours extracted   : {len(rows)}")
    print(f"  Missing weather   : {missing_weather}")
    print(f"  Output            : {OUTPUT_FILE}")

    prices_list = list(prices.values())
    print(f"  Price range       : {min(prices_list):.2f}¢ – {max(prices_list):.2f}¢/kWh")

    # Show sample
    print(f"\nFirst 3 rows:")
    for row in rows[:3]:
        print(f"  {row['timestamp']} | {row['price_cents']}¢ | {row['temp_c']}°C | humidity: {row['humidity']}%")


if __name__ == "__main__":
    main()