"""
Trains a Random Forest model on prices_weather.csv to predict
hourly ComEd prices given weather + time features, and generates
tomorrow's hourly price predictions for the dashboard's prediction
chart (predictions.json).

Two ways to run:

  Full train (also regenerates tomorrow's predictions afterward):
      python3 train_model.py              # train if enough data (>= MIN_ROWS)
      python3 train_model.py --force      # train regardless of row count
      python3 train_model.py --evaluate   # show accuracy stats only, no save

  Predict only — no retraining, requires an existing model.pkl:
      python3 train_model.py --predict-only

  Run from Mac with local data:
      python3 train_model.py --data-dir ~/comed-climate-controller/comed_ml --evaluate

Recommended cron setup on the Pi (neither is currently scheduled —
add these once a model exists):
  Nightly, refresh tomorrow's predictions:
      0 23 * * * /usr/bin/python3 /config/comed_ml/train_model.py --predict-only
  Monthly, full retrain on accumulated data:
      0 3 1 * * /usr/bin/python3 /config/comed_ml/train_model.py

Output:
    /config/comed_ml/model.pkl          — trained model
    /config/comed_ml/model_meta.json    — accuracy stats + training date
    /config/www/predictions.json        — tomorrow's hourly price predictions
                                           (read by dashboard.html's
                                           "Tomorrow's Predicted Prices" chart)
"""

import os
import sys
import csv
import json
import math
import pickle
import argparse
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CHICAGO_TZ = ZoneInfo("America/Chicago")

# ── Config ────────────────────────────────────────────────────────────────────

DATA_DIR    = "/config/comed_ml"
DATA_FILE   = os.path.join(DATA_DIR, "prices_weather.csv")
MODEL_FILE  = os.path.join(DATA_DIR, "model.pkl")
META_FILE   = os.path.join(DATA_DIR, "model_meta.json")

MIN_ROWS     = 1000  # minimum rows before training makes sense
N_ESTIMATORS = 100   # random forest trees — good balance speed vs accuracy

# Location — matches collect_data.py / controller.py (Aurora IL)
LAT = 41.7421
LON = -88.2456
WEATHER_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Where the dashboard reads tomorrow's predictions from (served as
# /local/predictions.json by Home Assistant)
PREDICTIONS_OUTPUT_DIR = "/config/www"
PREDICTIONS_FILE = os.path.join(PREDICTIONS_OUTPUT_DIR, "predictions.json")

# Mirrors controller.py's price tier thresholds (PRICE_LOW / PRICE_HIGH).
# Duplicated here since this script runs standalone — keep in sync if
# those ever change in controller.py.
PRICE_LOW_THRESHOLD  = 5.0
PRICE_HIGH_THRESHOLD = 12.0

# Features used for training — order matters for the model
FEATURES = [
    "hour",
    "hour_sin",
    "hour_cos",
    "day_of_week",
    "month",
    "is_weekend",
    "temp_c",
    "humidity",
    "dewpoint_c",
    "lag_168",       # same hour, one week ago
    "roll_24_mean",  # rolling mean of last 24h of prices
    "roll_24_max",   # rolling max of last 24h of prices
    "roll_168_mean", # rolling mean of last 7 days of prices
]
TARGET = "price_cents"

# Price bands for the controller — used for band-accuracy evaluation.
# Maps a price to the band the controller would act on.
PRICE_BANDS = [
    (3.0,  "super_cheap"),   # < 3¢
    (5.0,  "cheap"),         # 3–5¢
    (8.0,  "moderate"),      # 5–8¢
    (12.0, "expensive"),     # 8–12¢
    (15.0, "high"),          # 12–15¢
    (999,  "spike"),         # 15¢+
]


def price_to_band(price: float) -> str:
    for threshold, label in PRICE_BANDS:
        if price < threshold:
            return label
    return "spike"


# ── Data loading ──────────────────────────────────────────────────────────────

def parse_csv(filepath: str) -> list:
    """
    Parse CSV into a sorted list of dicts with parsed timestamps.
    Deduplicates by timestamp (keeps first occurrence).
    """
    rows = []
    seen = set()

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_str = row.get("timestamp", "").strip()
            if not ts_str or ts_str in seen:
                continue
            seen.add(ts_str)

            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:00")
            except ValueError:
                continue

            try:
                price = float(row[TARGET])
                temp  = float(row["temp_c"])
                hum   = float(row["humidity"])
                dew   = float(row["dewpoint_c"])
            except (ValueError, KeyError):
                continue

            rows.append({
                "timestamp": ts,
                "price_cents": price,
                "temp_c": temp,
                "humidity": hum,
                "dewpoint_c": dew,
            })

    rows.sort(key=lambda r: r["timestamp"])
    return rows


def build_price_lookup(rows: list) -> dict:
    """Build {timestamp: price} dict for lag/rolling feature computation."""
    return {r["timestamp"]: r["price_cents"] for r in rows}


def compute_rolling(price_lookup: dict, ts: datetime, window_hours: int) -> dict:
    """
    Compute rolling mean and max over the window_hours before ts.
    Uses timestamp lookup — handles gaps correctly (skips missing hours).
    Returns {"mean": float|None, "max": float|None}
    """
    prices = []
    for h in range(1, window_hours + 1):
        past_ts = ts - timedelta(hours=h)
        if past_ts in price_lookup:
            prices.append(price_lookup[past_ts])

    if len(prices) < window_hours * 0.5:  # need at least half the window
        return {"mean": None, "max": None}

    return {
        "mean": sum(prices) / len(prices),
        "max": max(prices),
    }


def load_data(filepath: str) -> tuple:
    """
    Load CSV and return (X, y, skipped_count).
    Computes lag and rolling features from price history.
    """
    rows = parse_csv(filepath)
    price_lookup = build_price_lookup(rows)

    X, y = [], []
    skipped = 0

    for row in rows:
        price = row["price_cents"]

        # Sanity bounds — allow real negatives, reject absurd values
        if price < -50 or price > 200:
            skipped += 1
            continue

        ts   = row["timestamp"]
        hour = ts.hour
        dow  = ts.weekday()

        # Lag: same hour, one week ago
        lag_168_ts = ts - timedelta(hours=168)
        lag_168 = price_lookup.get(lag_168_ts)
        if lag_168 is None:
            skipped += 1
            continue

        # Rolling stats
        roll_24 = compute_rolling(price_lookup, ts, 24)
        roll_168 = compute_rolling(price_lookup, ts, 168)

        if roll_24["mean"] is None or roll_168["mean"] is None:
            skipped += 1
            continue

        features = [
            hour,
            math.sin(2 * math.pi * hour / 24),   # hour_sin
            math.cos(2 * math.pi * hour / 24),   # hour_cos
            dow,
            ts.month,
            1 if dow >= 5 else 0,                 # is_weekend
            row["temp_c"],
            row["humidity"],
            row["dewpoint_c"],
            lag_168,
            roll_24["mean"],
            roll_24["max"],
            roll_168["mean"],
        ]

        X.append(features)
        y.append(price)

    return X, y, len(rows) - len(X)


# ── Model training ────────────────────────────────────────────────────────────

def train(X: list, y: list) -> object:
    """Train a Random Forest regressor."""
    try:
        from sklearn.ensemble import RandomForestRegressor
    except ImportError:
        print("ERROR: scikit-learn not installed.")
        print("Run: pip3 install scikit-learn --break-system-packages")
        sys.exit(1)

    model = RandomForestRegressor(
        n_estimators=N_ESTIMATORS,
        max_depth=12,           # limit depth — keeps Pi memory low
        min_samples_leaf=3,     # prevents overfitting on small dataset
        n_jobs=1,               # single core — Pi 3 friendly
        random_state=42,
    )
    model.fit(X, y)
    return model


# ── Evaluation ────────────────────────────────────────────────────────────────

def evaluate(model, X: list, y: list) -> dict:
    """
    Accuracy metrics on a time-ordered 80/20 split.
    Includes spike-aware metrics that matter for controller decisions.
    """
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    from sklearn.ensemble import RandomForestRegressor
    eval_model = RandomForestRegressor(
        n_estimators=N_ESTIMATORS,
        max_depth=12,
        min_samples_leaf=3,
        n_jobs=1,
        random_state=42,
    )
    eval_model.fit(X_train, y_train)
    preds = eval_model.predict(X_test)

    # ── Overall metrics ──
    mae  = sum(abs(p - a) for p, a in zip(preds, y_test)) / len(y_test)
    rmse = (sum((p - a) ** 2 for p, a in zip(preds, y_test)) / len(y_test)) ** 0.5
    within_2c = sum(1 for p, a in zip(preds, y_test) if abs(p - a) <= 2.0) / len(y_test)

    # ── Conditional MAE: actual >= 10¢ ──
    expensive = [(p, a) for p, a in zip(preds, y_test) if a >= 10.0]
    mae_expensive = None
    if expensive:
        mae_expensive = sum(abs(p - a) for p, a in expensive) / len(expensive)

    # ── Spike recall: actual >= 15¢, did we predict >= 15¢? ──
    actual_spikes   = [(p, a) for p, a in zip(preds, y_test) if a >= 15.0]
    spike_recall    = None
    spike_count     = len(actual_spikes)
    if actual_spikes:
        caught = sum(1 for p, a in actual_spikes if p >= 15.0)
        spike_recall = caught / len(actual_spikes)

    # ── Band accuracy: does the prediction land in the same controller band? ──
    band_correct = sum(
        1 for p, a in zip(preds, y_test)
        if price_to_band(p) == price_to_band(a)
    )
    band_accuracy = band_correct / len(y_test)

    # ── Band confusion summary ──
    band_confusion = {}
    for p, a in zip(preds, y_test):
        actual_band = price_to_band(a)
        pred_band   = price_to_band(p)
        key = (actual_band, pred_band)
        band_confusion[key] = band_confusion.get(key, 0) + 1

    # Feature importance
    feature_importance = dict(zip(FEATURES, eval_model.feature_importances_))
    top_features = sorted(feature_importance.items(), key=lambda x: x[1], reverse=True)

    return {
        "mae_cents":       round(mae, 3),
        "rmse_cents":      round(rmse, 3),
        "within_2c_pct":   round(within_2c * 100, 1),
        "test_rows":       len(y_test),
        "top_features":    [(k, round(v, 3)) for k, v in top_features[:6]],
        "mae_expensive":   round(mae_expensive, 3) if mae_expensive else None,
        "expensive_count": len(expensive),
        "spike_recall":    round(spike_recall, 3) if spike_recall is not None else None,
        "spike_count":     spike_count,
        "band_accuracy":   round(band_accuracy * 100, 1),
        "band_confusion":  band_confusion,
    }


# ── Tomorrow's price prediction ─────────────────────────────────────────────

def fetch_tomorrow_weather() -> tuple:
    """
    Fetch tomorrow's hourly weather forecast from Open-Meteo.
    Returns ({hour: {"temp_c", "humidity", "dewpoint_c"}}, "YYYY-MM-DD")
    """
    tomorrow_str = (datetime.now(CHICAGO_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        "latitude":  LAT,
        "longitude": LON,
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m",
        "timezone": "America/Chicago",
        "forecast_days": 2,
    }
    try:
        resp = requests.get(WEATHER_FORECAST_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Weather forecast fetch failed: {e}")
        return {}, tomorrow_str

    weather_by_hour = {}
    times = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    hums  = data["hourly"]["relative_humidity_2m"]
    dews  = data["hourly"]["dew_point_2m"]

    for i, ts in enumerate(times):
        if not ts.startswith(tomorrow_str):
            continue
        hour = int(ts[11:13])
        weather_by_hour[hour] = {
            "temp_c":     round(temps[i], 2) if temps[i] is not None else None,
            "humidity":   round(hums[i], 1)  if hums[i]  is not None else None,
            "dewpoint_c": round(dews[i], 2)  if dews[i]  is not None else None,
        }

    return weather_by_hour, tomorrow_str


def format_hour(h: int) -> str:
    """24h int -> '2 AM' / '11 PM' style label."""
    h = h % 24
    period  = "AM" if h < 12 else "PM"
    display = h % 12 or 12
    return f"{display} {period}"


def find_best_charging_window(predictions: list, threshold: float = PRICE_LOW_THRESHOLD) -> str:
    """
    Find the longest contiguous run of hours at/under threshold.
    Falls back to the single cheapest hour if nothing qualifies.
    """
    cheap_hours = sorted(p["hour"] for p in predictions if p["predicted_price"] <= threshold)

    if not cheap_hours:
        best = min(predictions, key=lambda p: p["predicted_price"])
        return f"{format_hour(best['hour'])} ({best['predicted_price']:.1f}¢)"

    runs, run = [], [cheap_hours[0]]
    for h in cheap_hours[1:]:
        if h == run[-1] + 1:
            run.append(h)
        else:
            runs.append(run)
            run = [h]
    runs.append(run)

    best_run = max(runs, key=len)
    prices_in_run = [p["predicted_price"] for p in predictions if p["hour"] in best_run]
    avg_price = sum(prices_in_run) / len(prices_in_run)
    start, end = best_run[0], (best_run[-1] + 1) % 24
    return f"{format_hour(start)}–{format_hour(end)} (avg {avg_price:.1f}¢)"


def predict_tomorrow(data_file: str) -> bool:
    """
    Load the existing trained model and generate hourly price predictions
    for tomorrow, writing predictions.json for the dashboard chart.
    Does NOT retrain — requires model.pkl to already exist.
    Returns True on success, False if predictions could not be generated.
    """
    if not os.path.exists(MODEL_FILE):
        print(f"No trained model yet at {MODEL_FILE} — nothing to predict.")
        print(f"Run train_model.py (without --predict-only) once you have {MIN_ROWS}+ rows.")
        return False

    with open(MODEL_FILE, "rb") as f:
        model = pickle.load(f)

    mae = None
    training_rows = None
    if os.path.exists(META_FILE):
        with open(META_FILE) as f:
            meta = json.load(f)
            mae           = meta.get("mae_cents")
            training_rows = meta.get("training_rows")

    # Load price history for lag/rolling features
    if not os.path.exists(data_file):
        print(f"Cannot compute lag features: {data_file} not found.")
        return False

    rows = parse_csv(data_file)
    price_lookup = build_price_lookup(rows)

    weather_by_hour, tomorrow_str = fetch_tomorrow_weather()
    if not weather_by_hour:
        print("Could not fetch tomorrow's weather forecast — skipping prediction this run.")
        return False

    tomorrow_dt = datetime.strptime(tomorrow_str, "%Y-%m-%d")
    dow     = tomorrow_dt.weekday()
    month   = tomorrow_dt.month
    is_wknd = 1 if dow >= 5 else 0

    # Rolling stats: computed once from the most recent prices
    # Use the latest timestamp in the data as the anchor
    latest_ts = rows[-1]["timestamp"] if rows else None
    if latest_ts is None:
        print("No price history available for rolling features.")
        return False

    roll_24  = compute_rolling(price_lookup, latest_ts, 24)
    roll_168 = compute_rolling(price_lookup, latest_ts, 168)

    if roll_24["mean"] is None or roll_168["mean"] is None:
        print("Insufficient price history for rolling features.")
        return False

    predictions = []
    skipped_hours = []
    for hour in range(24):
        wx = weather_by_hour.get(hour, {})
        temp, hum, dew = wx.get("temp_c"), wx.get("humidity"), wx.get("dewpoint_c")
        if temp is None or hum is None or dew is None:
            skipped_hours.append(hour)
            continue

        # lag_168: same hour, 7 days ago
        forecast_ts = tomorrow_dt.replace(hour=hour)
        lag_168_ts  = forecast_ts - timedelta(hours=168)
        lag_168     = price_lookup.get(lag_168_ts)

        if lag_168 is None:
            # Fallback: use roll_168_mean if exact lag missing
            lag_168 = roll_168["mean"]

        features = [[
            hour,
            math.sin(2 * math.pi * hour / 24),
            math.cos(2 * math.pi * hour / 24),
            dow,
            month,
            is_wknd,
            temp,
            hum,
            dew,
            lag_168,
            roll_24["mean"],
            roll_24["max"],
            roll_168["mean"],
        ]]
        price = float(model.predict(features)[0])
        predictions.append({"hour": hour, "predicted_price": round(price, 2)})

    if not predictions:
        print("No usable forecast hours for tomorrow — skipping prediction this run.")
        return False

    peak_hours  = [p["hour"] for p in predictions if p["predicted_price"] >= PRICE_HIGH_THRESHOLD]
    best_window = find_best_charging_window(predictions)

    if peak_hours:
        recommendation = (
            f"Avoid heavy appliance use {format_hour(peak_hours[0])}-"
            f"{format_hour((peak_hours[-1] + 1) % 24)}. Best window: {best_window}."
        )
    else:
        recommendation = f"No price spikes expected tomorrow. Best window: {best_window}."

    output = {
        "generated_at":         datetime.now(CHICAGO_TZ).strftime("%Y-%m-%d %H:%M"),
        "for_date":             tomorrow_str,
        "model_mae":            mae,
        "training_rows":        training_rows,
        "predictions":          predictions,
        "best_charging_window": best_window,
        "peak_risk_hours":      peak_hours,
        "recommendation":       recommendation,
    }

    try:
        os.makedirs(PREDICTIONS_OUTPUT_DIR, exist_ok=True)
        with open(PREDICTIONS_FILE, "w") as f:
            json.dump(output, f, indent=2)
    except Exception as e:
        print(f"Failed to write {PREDICTIONS_FILE}: {e}")
        return False

    print(f"\nPredictions written to {PREDICTIONS_FILE}")
    print(f"  For date       : {tomorrow_str}")
    print(f"  Hours predicted: {len(predictions)}/24")
    if skipped_hours:
        print(f"  Skipped hours  : {skipped_hours} (missing weather forecast)")
    print(f"  Best window    : {best_window}")
    print(f"  Peak risk hrs  : {peak_hours if peak_hours else 'none'}")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global DATA_DIR, DATA_FILE, MODEL_FILE, META_FILE, PREDICTIONS_OUTPUT_DIR, PREDICTIONS_FILE

    parser = argparse.ArgumentParser()
    parser.add_argument("--force",    action="store_true",
                        help="Train even if below minimum row count")
    parser.add_argument("--evaluate", action="store_true",
                        help="Show accuracy stats without saving model")
    parser.add_argument("--predict-only", action="store_true",
                        help="Skip training entirely; load the existing model "
                             "and write tomorrow's predictions.json")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="Override data directory (for running on Mac)")
    args = parser.parse_args()

    if args.data_dir:
        DATA_DIR   = os.path.expanduser(args.data_dir)
        DATA_FILE  = os.path.join(DATA_DIR, "prices_weather.csv")
        MODEL_FILE = os.path.join(DATA_DIR, "model.pkl")
        META_FILE  = os.path.join(DATA_DIR, "model_meta.json")
        # Predictions also redirect to the local data dir when running on
        # the Mac -- /config/www only exists on the Pi.
        PREDICTIONS_OUTPUT_DIR = DATA_DIR
        PREDICTIONS_FILE       = os.path.join(DATA_DIR, "predictions.json")

    if args.predict_only:
        ok = predict_tomorrow(DATA_FILE)
        sys.exit(0 if ok else 1)

    if not os.path.exists(DATA_FILE):
        print(f"ERROR: Data file not found at {DATA_FILE}")
        print("Run parse_log.py first to bootstrap the dataset.")
        sys.exit(1)

    # Load data
    print("Loading data...")
    X, y, skipped = load_data(DATA_FILE)
    print(f"  Loaded  : {len(X)} rows (with lag/rolling features)")
    print(f"  Skipped : {skipped} (dupes, missing values, or insufficient history)")

    if len(X) < MIN_ROWS and not args.force:
        print(f"\nNot enough data yet ({len(X)} rows, need {MIN_ROWS}).")
        print(f"Keep collecting — you need ~{MIN_ROWS - len(X)} more rows.")
        print(f"At 24 rows/day that's ~{(MIN_ROWS - len(X)) // 24} more days.")
        print("Use --force to train anyway (accuracy will be low).")
        sys.exit(0)

    if len(X) < MIN_ROWS:
        print(f"WARNING: Only {len(X)} rows (below {MIN_ROWS} minimum) — training anyway (--force)")

    # Evaluate first
    print("\nEvaluating model accuracy (80/20 time split)...")
    metrics = evaluate(None, X, y)
    print(f"  MAE           : {metrics['mae_cents']}¢  (mean prediction error)")
    print(f"  RMSE          : {metrics['rmse_cents']}¢")
    print(f"  Within 2¢     : {metrics['within_2c_pct']}% of predictions")
    print(f"  Test rows     : {metrics['test_rows']}")
    print(f"  Top features  : {metrics['top_features']}")

    # Spike-aware metrics
    print(f"\n  --- Spike-aware metrics ---")
    if metrics["mae_expensive"] is not None:
        print(f"  MAE (≥10¢)    : {metrics['mae_expensive']}¢  ({metrics['expensive_count']} hours)")
    else:
        print(f"  MAE (≥10¢)    : no hours ≥10¢ in test set")
    if metrics["spike_recall"] is not None:
        pct = round(metrics["spike_recall"] * 100, 1)
        print(f"  Spike recall  : {pct}%  (of {metrics['spike_count']} actual spikes ≥15¢, predicted ≥15¢)")
    else:
        print(f"  Spike recall  : no spikes ≥15¢ in test set")
    print(f"  Band accuracy : {metrics['band_accuracy']}%  (prediction lands in correct controller band)")

    # Band confusion summary
    if metrics["band_confusion"]:
        print(f"\n  --- Band confusion (actual → predicted) ---")
        sorted_conf = sorted(metrics["band_confusion"].items(),
                             key=lambda x: (-x[1],))
        for (actual, pred), count in sorted_conf[:10]:
            marker = " ✓" if actual == pred else ""
            print(f"    {actual:>12} → {pred:<12} : {count}{marker}")

    if args.evaluate:
        print("\nEvaluate-only mode — no model saved.")
        return

    # Train final model on ALL data
    print("\nTraining final model on full dataset...")
    model = train(X, y)
    print("  Done.")

    # Save model
    with open(MODEL_FILE, "wb") as f:
        pickle.dump(model, f)

    # Save metadata
    meta = {
        "trained_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
        "training_rows":   len(X),
        "features":        FEATURES,
        "mae_cents":       metrics["mae_cents"],
        "rmse_cents":      metrics["rmse_cents"],
        "within_2c_pct":   metrics["within_2c_pct"],
        "top_features":    metrics["top_features"],
        "n_estimators":    N_ESTIMATORS,
        "spike_recall":    metrics["spike_recall"],
        "band_accuracy":   metrics["band_accuracy"],
    }
    with open(META_FILE, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\nModel saved to  : {MODEL_FILE}")
    print(f"Metadata saved  : {META_FILE}")
    print(f"\nSummary:")
    print(f"  Trained on    : {len(X)} hours of Aurora IL price data")
    print(f"  Accuracy      : ±{metrics['mae_cents']}¢ mean error")
    print(f"  Spike recall  : {metrics['spike_recall']}")
    print(f"  Band accuracy : {metrics['band_accuracy']}%")
    print(f"  Best predictor: {metrics['top_features'][0][0]}")

    # Refresh tomorrow's predictions immediately using the freshly trained model.
    # Non-fatal if this fails (e.g. weather API hiccup) — training itself
    # already succeeded and was saved above.
    print("\nGenerating tomorrow's price predictions for the dashboard...")
    predict_tomorrow(DATA_FILE)


if __name__ == "__main__":
    main()