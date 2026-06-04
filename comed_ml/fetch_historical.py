#!/usr/bin/env python3
"""
ComEd Historical Price + Weather Fetcher
=========================================
Fetches hourly ComEd prices and matching Open-Meteo weather data
for Aurora IL, merges them into a clean CSV for ML training.

Usage:
    python3 fetch_historical.py                  # pulls last 2 years
    python3 fetch_historical.py --days 365       # pulls last 1 year
    python3 fetch_historical.py --append         # adds only new rows to existing CSV

Output:
    /config/comed_ml/prices_weather.csv

Run this once on your laptop or Pi to build the training dataset.
After that, the monthly retrain cron job calls it with --append.
"""

import requests
import csv
import os
import time
import argparse
import json
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────

LAT         = 41.7421
LON         = -88.2456
OUTPUT_DIR  = "/config/comed_ml"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "prices_weather.csv")
DEFAULT_DAYS = 365  # 1 year

COMED_HISTORY_URL = "https://hourlypricing.comed.com/api"
WEATHER_HISTORY_URL = "https://archive-api.open-meteo.com/v1/archive"

CSV_HEADERS = [
    "timestamp",        # YYYY-MM-DD HH:00
    "price_cents",      # ComEd hourly billing average (cents/kWh)
    "temp_c",           # actual recorded temp at that hour
    "humidity",         # relative humidity %
    "dewpoint_c",       # dew point (heat index proxy)
    "hour",             # 0–23
    "day_of_week",      # 0=Monday, 6=Sunday
    "month",            # 1–12
    "is_weekend",       # 0 or 1
    "is_summer",        # 1 if June–September (capacity season)
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def date_range(start: datetime, end: datetime):
    """Yield each date from start up to but not including end."""
    current = start.date()
    stop = end.date()
    while current < stop:
        yield current
        current += timedelta(days=1)


def fetch_comed_day(date) -> dict:
    date_str = date.strftime("%Y%m%d")
    params = {
        "type": "5minutefeed",
        "datestart": date_str + "0000",
        "dateend":   date_str + "2359",
    }
    try:
        resp = requests.get(COMED_HISTORY_URL, params=params, timeout=15)
        resp.raise_for_status()
        text = resp.text.strip()
        if not text or text[0] != "[":
            return {}
        data = json.loads(text)
    except Exception as e:
        log(f"  ComEd fetch failed for {date}: {e}")
        return {}

    buckets = {}
    for entry in data:
        try:
            ts = datetime.fromtimestamp(int(entry["millisUTC"]) / 1000, tz=timezone.utc)
            local_hour = (ts - timedelta(hours=5)).hour
            price = float(entry["price"])
            if local_hour not in buckets:
                buckets[local_hour] = []
            buckets[local_hour].append(price)
        except (KeyError, ValueError):
            continue

    return {h: round(sum(p) / len(p), 4) for h, p in buckets.items()}


def fetch_weather_range(start: datetime, end: datetime) -> dict:
    """
    Fetch hourly weather from Open-Meteo historical archive for a date range.
    Returns dict of {"YYYY-MM-DD HH:00": {temp_c, humidity, dewpoint_c}}
    Fetches the whole range in one API call — much faster than per-day.
    """
    params = {
        "latitude": LAT,
        "longitude": LON,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m",
        "timezone": "America/Chicago",
    }
    try:
        resp = requests.get(WEATHER_HISTORY_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log(f"  Weather fetch failed: {e}")
        return {}

    weather = {}
    times      = data["hourly"]["time"]
    temps      = data["hourly"]["temperature_2m"]
    humidities = data["hourly"]["relative_humidity_2m"]
    dewpoints  = data["hourly"]["dew_point_2m"]

    for i, ts_str in enumerate(times):
        # Open-Meteo returns "2024-06-15T14:00" — normalize to our key format
        key = ts_str.replace("T", " ")  # "2024-06-15 14:00"
        weather[key] = {
            "temp_c":    round(temps[i], 2)      if temps[i]      is not None else None,
            "humidity":  round(humidities[i], 1) if humidities[i] is not None else None,
            "dewpoint_c": round(dewpoints[i], 2) if dewpoints[i]  is not None else None,
        }

    return weather


def load_existing_timestamps(filepath: str) -> set:
    """Return set of timestamp strings already in the CSV."""
    existing = set()
    if not os.path.exists(filepath):
        return existing
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            existing.add(row["timestamp"])
    return existing


def forward_fill(rows: list) -> list:
    """
    Fill any missing price or weather values by carrying forward
    the last known good value. Handles ComEd API gaps gracefully.
    """
    last = {}
    filled = []
    for row in rows:
        for key in ["price_cents", "temp_c", "humidity", "dewpoint_c"]:
            if row[key] is None or row[key] == "":
                row[key] = last.get(key, "")
            else:
                last[key] = row[key]
        filled.append(row)
    return filled


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch ComEd + weather history")
    parser.add_argument("--days",   type=int, default=DEFAULT_DAYS,
                        help="How many days back to fetch (default: 730)")
    parser.add_argument("--append", action="store_true",
                        help="Only fetch rows newer than what's already in the CSV")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    end_date   = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=args.days)

    existing_timestamps = set()
    if args.append and os.path.exists(OUTPUT_FILE):
        existing_timestamps = load_existing_timestamps(OUTPUT_FILE)
        if existing_timestamps:
            # Only fetch from the day after our last known row
            last_ts = max(existing_timestamps)
            last_dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M")
            start_date = last_dt.replace(tzinfo=timezone.utc) + timedelta(hours=1)
            log(f"Append mode: fetching from {start_date.date()} onward")
        else:
            log("Append mode: CSV exists but is empty, doing full fetch")
    else:
        log(f"Full fetch: {start_date.date()} → {end_date.date()} ({args.days} days)")

    if start_date >= end_date:
        log("Nothing to fetch — CSV is already up to date.")
        return

    # ── Fetch all weather in one API call ──────────────────────────────────
    log(f"Fetching weather data from Open-Meteo ({start_date.date()} → {end_date.date()})...")
    weather_data = fetch_weather_range(start_date, end_date)
    log(f"  Got {len(weather_data)} hourly weather readings")

    # ── Fetch ComEd prices day by day ──────────────────────────────────────
    all_rows = []
    dates = list(date_range(start_date, end_date))
    log(f"Fetching ComEd prices for {len(dates)} days...")

    for i, date in enumerate(dates):
        if i > 0 and i % 30 == 0:
            log(f"  Progress: {i}/{len(dates)} days ({i*24} hours)")
            time.sleep(0.5)  # be polite to ComEd API

        prices = fetch_comed_day(date)

        for hour in range(24):
            ts_str = f"{date.strftime('%Y-%m-%d')} {hour:02d}:00"

            if ts_str in existing_timestamps:
                continue  # skip rows we already have

            price = prices.get(hour)
            wx    = weather_data.get(ts_str, {})

            dt = datetime(date.year, date.month, date.day, hour)

            row = {
                "timestamp":   ts_str,
                "price_cents": price,
                "temp_c":      wx.get("temp_c"),
                "humidity":    wx.get("humidity"),
                "dewpoint_c":  wx.get("dewpoint_c"),
                "hour":        hour,
                "day_of_week": dt.weekday(),        # 0=Mon
                "month":       dt.month,
                "is_weekend":  1 if dt.weekday() >= 5 else 0,
                "is_summer":   1 if dt.month in (6, 7, 8, 9) else 0,
            }
            all_rows.append(row)

        time.sleep(0.1)  # gentle rate limiting

    log(f"Fetched {len(all_rows)} new rows, applying forward-fill for gaps...")
    all_rows = forward_fill(all_rows)

    # ── Write CSV ──────────────────────────────────────────────────────────
    write_mode = "a" if (args.append and existing_timestamps) else "w"
    write_header = write_mode == "w"

    with open(OUTPUT_FILE, write_mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerows(all_rows)

    # ── Summary ────────────────────────────────────────────────────────────
    total_rows = len(existing_timestamps) + len(all_rows)
    log(f"\nDone!")
    log(f"  New rows written : {len(all_rows)}")
    log(f"  Total rows in CSV: {total_rows}")
    log(f"  Output file      : {OUTPUT_FILE}")

    # Spot-check: show first and last few rows
    prices_found = [r["price_cents"] for r in all_rows if r["price_cents"] is not None]
    if prices_found:
        log(f"  Price range      : {min(prices_found):.2f}¢ – {max(prices_found):.2f}¢/kWh")
    temps_found = [r["temp_c"] for r in all_rows if r["temp_c"] is not None]
    if temps_found:
        log(f"  Temp range       : {min(temps_found):.1f}°C – {max(temps_found):.1f}°C")


if __name__ == "__main__":
    main()
