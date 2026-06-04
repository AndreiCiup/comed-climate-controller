#!/usr/bin/env python3
"""
ComEd Price Predictor — Model Training
========================================
Trains a Random Forest model on prices_weather.csv to predict
hourly ComEd prices given weather + time features.

Designed to run on Raspberry Pi 3 at 3 AM monthly, or on your
Mac for faster initial training.

Usage:
    python3 train_model.py              # train if enough data
    python3 train_model.py --force      # train regardless of row count
    python3 train_model.py --evaluate   # show accuracy stats only

Output:
    /config/comed_ml/model.pkl          — trained model
    /config/comed_ml/model_meta.json    — accuracy stats + training date
"""

import os
import sys
import csv
import json
import pickle
import argparse
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

DATA_DIR    = "/config/comed_ml"
DATA_FILE   = os.path.join(DATA_DIR, "prices_weather.csv")
MODEL_FILE  = os.path.join(DATA_DIR, "model.pkl")
META_FILE   = os.path.join(DATA_DIR, "model_meta.json")

MIN_ROWS    = 500   # minimum rows before training makes sense
N_ESTIMATORS = 100  # random forest trees — good balance speed vs accuracy

# Features used for training — must match collect_data.py CSV columns
FEATURES = [
    "hour",
    "day_of_week",
    "month",
    "is_weekend",
    "is_summer",
    "temp_c",
    "humidity",
    "dewpoint_c",
]
TARGET = "price_cents"

# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(filepath: str) -> tuple:
    """
    Load CSV and return (X, y) as plain Python lists.
    Skips rows with missing values.
    Returns (X, y, skipped_count)
    """
    X, y = [], []
    skipped = 0

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        seen_timestamps = set()

        for row in reader:
            # Deduplicate by timestamp (handles manual test rows)
            ts = row["timestamp"]
            if ts in seen_timestamps:
                skipped += 1
                continue
            seen_timestamps.add(ts)

            # Skip rows with missing values
            try:
                features = [float(row[f]) for f in FEATURES]
                target   = float(row[TARGET])
            except (ValueError, KeyError):
                skipped += 1
                continue

            # Skip obvious outliers (negative or absurdly high prices)
            if target < 0 or target > 200:
                skipped += 1
                continue

            X.append(features)
            y.append(target)

    return X, y, skipped


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
    Cross-validated accuracy metrics.
    Uses last 20% of data as test set (time-ordered split).
    """
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    # Retrain on train split only
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

    # MAE — mean absolute error in cents
    mae  = sum(abs(p - a) for p, a in zip(preds, y_test)) / len(y_test)
    # RMSE — root mean squared error
    rmse = (sum((p - a) ** 2 for p, a in zip(preds, y_test)) / len(y_test)) ** 0.5
    # Within 2 cents accuracy
    within_2c = sum(1 for p, a in zip(preds, y_test) if abs(p - a) <= 2.0) / len(y_test)

    # Feature importance
    feature_importance = dict(zip(FEATURES, eval_model.feature_importances_))
    top_features = sorted(feature_importance.items(), key=lambda x: x[1], reverse=True)

    return {
        "mae_cents":       round(mae, 3),
        "rmse_cents":      round(rmse, 3),
        "within_2c_pct":   round(within_2c * 100, 1),
        "test_rows":       len(y_test),
        "top_features":    [(k, round(v, 3)) for k, v in top_features[:4]],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force",    action="store_true",
                        help="Train even if below minimum row count")
    parser.add_argument("--evaluate", action="store_true",
                        help="Show accuracy stats without saving model")
    args = parser.parse_args()

    if not os.path.exists(DATA_FILE):
        print(f"ERROR: Data file not found at {DATA_FILE}")
        print("Run parse_log.py first to bootstrap the dataset.")
        sys.exit(1)

    # Load data
    print("Loading data...")
    X, y, skipped = load_data(DATA_FILE)
    print(f"  Loaded  : {len(X)} rows")
    print(f"  Skipped : {skipped} (duplicates or missing values)")

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
    }
    with open(META_FILE, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\nModel saved to  : {MODEL_FILE}")
    print(f"Metadata saved  : {META_FILE}")
    print(f"\nSummary:")
    print(f"  Trained on    : {len(X)} hours of Aurora IL price data")
    print(f"  Accuracy      : ±{metrics['mae_cents']}¢ mean error")
    print(f"  Best predictor: {metrics['top_features'][0][0]}")


if __name__ == "__main__":
    main()