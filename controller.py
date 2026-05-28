#!/usr/bin/env python3
"""
ComEd Hourly Price → Ecobee + Tesla Climate & Charging Controller
Runs every 5 minutes via run.sh
"""

import requests
import json
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── CONFIG ────────────────────────────────────────────────────────────────────

HA_URL          = "http://homeassistant.local:8123"
HA_TOKEN        = "***REMOVED***"
CLIMATE_ENTITY  = "climate.my_ecobee"

# Tesla entities
TESLA_CHARGE_SWITCH   = "switch.lady_t_charge"
TESLA_CHARGING_SENSOR = "sensor.lady_t_charging"
TESLA_BATTERY_SENSOR  = "sensor.lady_t_battery_level"
TESLA_MAX_BATTERY     = 85.0
TESLA_CHARGE_PRICE    = 3.0
TESLA_STOP_PRICE      = 8.0
TESLA_RESUME_PRICE    = 5.0
TESLA_PROTECT_START   = 12
TESLA_PROTECT_END     = 19
WALL_CONNECTOR_VEHICLE = "sensor.wall_connector_vehicle"
WALL_CONNECTOR_POWER   = "sensor.wall_connector_power"
# Gmail settings
GMAIL_USER    = "climatecontrol.pi@gmail.com"
GMAIL_PASS    = "***REMOVED***"
NOTIFY_EMAILS = [
    "aciuperca.sdet@gmail.com",
    "g.crisu@yahoo.com"
]

# Baseline
FLAT_RATE = 11.5

# Cat safety
CAT_MAX_C = 25.0

# Price thresholds in cents/kWh
PRICE_FREE   = 1.0
PRICE_LOW    = 5.0
PRICE_NORMAL = 9.0
PRICE_HIGH   = 12.0

# Capacity charge tracking
CAPACITY_MONTHS   = [6, 7, 8, 9]
CAPACITY_HOURS    = range(12, 19)
CAPACITY_TEMP_C   = 30.0
CAPACITY_PRICE    = 10.0

# Setpoints in Celsius
SETPOINTS = {
    "free":   {"heat": 21.0, "cool": 22.0},
    "low":    {"heat": 21.0, "cool": 23.5},
    "normal": {"heat": 20.5, "cool": 23.5},
    "high":   {"heat": 20.0, "cool": 23.8},
    "peak":   {"heat": 19.5, "cool": 24.0},
}

SLEEP_SETPOINTS = {
    "free":   {"heat": 20.0, "cool": 20.0},
    "low":    {"heat": 20.5, "cool": 21.5},
    "normal": {"heat": 20.0, "cool": 21.5},
    "high":   {"heat": 19.5, "cool": 22.0},
    "peak":   {"heat": 19.0, "cool": 23.0},
}

# Logging
logging.basicConfig(
    filename="/config/comed_ecobee/controller.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open("/config/comed_ecobee/state.json", "r") as f:
            return json.load(f)
    except:
        return {
            "last_tier":          "normal",
            "last_change_hour":   -1,
            "low_alert_sent":     False,
            "daily_hours":        {},
            "capacity_peaks":     {},
            "tesla_paused":       False,
            "tesla_events":       [],
            "overnight_prices":   [],
            "overnight_start_battery": None,
        }

def save_state(state):
    with open("/config/comed_ecobee/state.json", "w") as f:
        json.dump(state, f)

# ── HELPERS ───────────────────────────────────────────────────────────────────

def is_sleep_time():
    hour = datetime.now().hour
    return hour >= 22 or hour < 6

def is_overnight_charging_window():
    hour = datetime.now().hour
    return hour >= 0 and hour < 6

# ── PRICE ─────────────────────────────────────────────────────────────────────

def get_comed_price():
    url = "https://hourlypricing.comed.com/api?type=5minutefeed"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    current = float(r.json()[0]["price"])

    url2 = "https://hourlypricing.comed.com/api?type=currenthouraverage"
    r2 = requests.get(url2, timeout=10)
    r2.raise_for_status()
    avg = float(r2.json()[0]["price"])

    logging.info(f"5-min price: {current:.2f}¢ | Hour avg: {avg:.2f}¢")
    return current, avg

def price_tier(price):
    if price < PRICE_FREE:
        return "free"
    elif price < PRICE_LOW:
        return "low"
    elif price < PRICE_NORMAL:
        return "normal"
    elif price < PRICE_HIGH:
        return "high"
    else:
        return "peak"

# ── WEATHER ───────────────────────────────────────────────────────────────────

def get_weather():
    url = ("https://api.open-meteo.com/v1/forecast"
           "?latitude=41.85&longitude=-87.65"
           "&hourly=temperature_2m,precipitation_probability"
           "&temperature_unit=celsius"
           "&timezone=America%2FChicago"
           "&forecast_days=2")
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()

def analyze_tomorrow_weather():
    data = get_weather()
    times = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_temps = [temps[i] for i, t in enumerate(times) if t.startswith(tomorrow)]
    if not tomorrow_temps:
        return None
    max_temp      = max(tomorrow_temps)
    afternoon     = tomorrow_temps[13:19]
    max_afternoon = max(afternoon) if afternoon else max_temp
    return {
        "max_temp":       max_temp,
        "max_afternoon":  max_afternoon,
        "expensive":      max_afternoon > 28.0,
        "very_expensive": max_afternoon > 32.0,
    }

def get_tonight_price_prediction():
    """Estimate tonight's midnight-6AM average based on current trends."""
    try:
        forecast = analyze_tomorrow_weather()
        if not forecast:
            return None, "Unknown"
        if forecast["very_expensive"]:
            return 8.0, "Potentially expensive tonight due to tomorrow's heat"
        elif forecast["expensive"]:
            return 5.0, "Moderate prices expected tonight"
        else:
            return 3.0, "Cheap prices expected tonight ✅"
    except:
        return None, "Forecast unavailable"

def should_precool():
    forecast = analyze_tomorrow_weather()
    if not forecast:
        return False, None
    return forecast["expensive"] or forecast["very_expensive"], forecast

# ── ECOBEE ────────────────────────────────────────────────────────────────────

def get_ha_state():
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    r = requests.get(
        f"{HA_URL}/api/states/{CLIMATE_ENTITY}",
        headers=headers, timeout=10
    )
    r.raise_for_status()
    return r.json()

def set_temperature(heat_c, cool_c):
    cool_c = min(cool_c, CAT_MAX_C)
    heat_c = min(heat_c, CAT_MAX_C - 1)
    cool_f = round((cool_c * 9/5) + 32, 1)
    heat_f = round((heat_c * 9/5) + 32, 1)

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type":  "application/json"
    }
    state     = get_ha_state()
    hvac_mode = state["state"]

    if hvac_mode == "cool":
        payload = {"entity_id": CLIMATE_ENTITY, "temperature": cool_f}
    elif hvac_mode == "heat":
        payload = {"entity_id": CLIMATE_ENTITY, "temperature": heat_f}
    else:
        payload = {
            "entity_id":       CLIMATE_ENTITY,
            "target_temp_high": cool_f,
            "target_temp_low":  heat_f
        }

    r = requests.post(
        f"{HA_URL}/api/services/climate/set_temperature",
        headers=headers, json=payload, timeout=10
    )
    r.raise_for_status()
    logging.info(f"Set temps → heat: {heat_c}°C ({heat_f}°F), cool: {cool_c}°C ({cool_f}°F)")

# ── CAPACITY PEAK ─────────────────────────────────────────────────────────────

def is_potential_peak_hour(price, hour_avg):
    now = datetime.now()
    if now.month not in CAPACITY_MONTHS:
        return False
    if now.hour not in CAPACITY_HOURS:
        return False
    if now.weekday() >= 5:
        return False
    try:
        weather  = get_weather()
        cur_temp = weather["hourly"]["temperature_2m"][0]
        if cur_temp < CAPACITY_TEMP_C:
            return False
    except:
        pass
    return hour_avg >= CAPACITY_PRICE

def handle_capacity_peak(state, hour_avg, current_temp):
    peak_key = f"peak_{datetime.now().strftime('%Y%m%d%H')}"
    if peak_key in state.get("capacity_peaks", {}):
        return state
    if "capacity_peaks" not in state:
        state["capacity_peaks"] = {}
    state["capacity_peaks"][peak_key] = {
        "price": hour_avg,
        "temp":  current_temp,
        "time":  datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    this_month       = datetime.now().strftime("%Y%m")
    peaks_this_month = sum(1 for k in state["capacity_peaks"] if k.startswith(f"peak_{this_month}"))

    # Log Tesla event
    state.setdefault("tesla_events", []).append({
        "time":   datetime.now().strftime("%I:%M %p"),
        "action": "stopped",
        "reason": f"Capacity peak hour — price {hour_avg:.2f}¢, temp {current_temp:.1f}°C",
        "price":  hour_avg
    })

    send_email(
        f"⚠️ Capacity Peak Alert — Reduce Usage Now!",
        f"Potential capacity peak hour detected!\n\n"
        f"Current price: {hour_avg:.2f}¢/kWh\n"
        f"Outdoor temp:  {current_temp:.1f}°C\n"
        f"Time: {datetime.now().strftime('%I:%M %p')}\n\n"
        f"ACTION NEEDED:\n"
        f"- Avoid dishwasher, laundry, oven\n"
        f"- Tesla charging stopped automatically\n"
        f"- Thermostat set to max comfort ceiling\n\n"
        f"Potential peak hours this month: {peaks_this_month}"
    )
    logging.info(f"Capacity peak: {hour_avg:.2f}¢, {current_temp:.1f}°C")
    return state

# ── TESLA ─────────────────────────────────────────────────────────────────────

def get_tesla_state():
    """Get Tesla state — checks wall connector first to minimize API calls."""
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}

    # Check wall connector first — cheapest call
    r_vehicle = requests.get(
        f"{HA_URL}/api/states/{WALL_CONNECTOR_VEHICLE}",
        headers=headers, timeout=10
    )
    r_vehicle.raise_for_status()
    vehicle_state = r_vehicle.json()["state"]

    r_power = requests.get(
        f"{HA_URL}/api/states/{WALL_CONNECTOR_POWER}",
        headers=headers, timeout=10
    )
    r_power.raise_for_status()
    try:
        wall_power = float(r_power.json()["state"])
    except:
        wall_power = 0.0

    # Car not home or not plugged in
    plugged_in = vehicle_state not in ["-", "0", "unavailable", "unknown"]

    if not plugged_in:
        return {
            "plugged_in":  False,
            "charging":    "Disconnected",
            "battery":     None,
            "switch":      "off",
            "wall_power":  0.0
        }

    # Car is plugged in — get full state
    r1 = requests.get(
        f"{HA_URL}/api/states/{TESLA_CHARGING_SENSOR}",
        headers=headers, timeout=10
    )
    r1.raise_for_status()

    r2 = requests.get(
        f"{HA_URL}/api/states/{TESLA_BATTERY_SENSOR}",
        headers=headers, timeout=10
    )
    r2.raise_for_status()

    r3 = requests.get(
        f"{HA_URL}/api/states/{TESLA_CHARGE_SWITCH}",
        headers=headers, timeout=10
    )
    r3.raise_for_status()

    return {
        "plugged_in":  True,
        "charging":    r1.json()["state"],
        "battery":     float(r2.json()["state"]),
        "switch":      r3.json()["state"],
        "wall_power":  wall_power
    }

def set_tesla_charging(enable):
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type":  "application/json"
    }
    service = "turn_on" if enable else "turn_off"
    r = requests.post(
        f"{HA_URL}/api/services/switch/{service}",
        headers=headers,
        json={"entity_id": TESLA_CHARGE_SWITCH},
        timeout=10
    )
    r.raise_for_status()
    logging.info(f"{'Started' if enable else 'Stopped'} Tesla charging")

def should_check_tesla(hour_avg, state):
    """Decide whether to make Tesla API calls this cycle."""
    now      = datetime.now()
    hour     = now.hour
    minute   = now.minute

    last_check_hour = state.get("tesla_last_check_hour", -1)
    last_check_min  = state.get("tesla_last_check_min", -1)
    last_avg        = state.get("last_hour_avg", 0.0)
    was_plugged_in  = state.get("tesla_plugged_in", False)

    # Price crossed a threshold — check immediately
    price_crossed = (
        (last_avg <= TESLA_STOP_PRICE < hour_avg) or
        (last_avg >= TESLA_STOP_PRICE > hour_avg) or
        (last_avg <= TESLA_RESUME_PRICE < hour_avg) or
        (last_avg >= TESLA_RESUME_PRICE > hour_avg) or
        (last_avg <= TESLA_CHARGE_PRICE < hour_avg) or
        (last_avg >= TESLA_CHARGE_PRICE > hour_avg)
    )
    if price_crossed:
        return True

    # Overnight charging window — check every 30 minutes
    if 0 <= hour < 6:
        if minute % 30 == 0:
            return True

    # Car was plugged in and actively charging — check every 30 minutes
    if was_plugged_in and minute % 30 == 0:
        return True

    # Once per hour for plug status check
    if last_check_hour != hour:
        return True

    return False

def handle_tesla_charging(hour_avg, state, is_capacity_peak):
    try:
# Check if we should poll Tesla this cycle
        if not should_check_tesla(hour_avg, state):
            return state

        # Update last check time
        state["tesla_last_check_hour"] = datetime.now().hour
        state["tesla_last_check_min"]  = datetime.now().minute
        state["last_hour_avg"]         = hour_avg
        tesla      = get_tesla_state()
# Update plugged in state for next cycle
        state["tesla_plugged_in"] = tesla["plugged_in"]

        # Car not home — nothing to do
        if not tesla["plugged_in"]:
            logging.info("Tesla not plugged in — skipping charging logic")
            return state
        battery    = tesla["battery"]
        is_charging = tesla["switch"] == "on"
        now_hour   = datetime.now().hour

        # Track start of overnight window
        if is_overnight_charging_window() and state.get("overnight_start_battery") is None:
            state["overnight_start_battery"] = battery
            state["overnight_prices"] = []

        # Track overnight prices
        if is_overnight_charging_window():
            state.setdefault("overnight_prices", []).append(hour_avg)

        # Never exceed max battery
        if battery >= TESLA_MAX_BATTERY:
            if is_charging:
                set_tesla_charging(False)
                state.setdefault("tesla_events", []).append({
                    "time":   datetime.now().strftime("%I:%M %p"),
                    "action": "stopped",
                    "reason": f"Battery reached {battery:.0f}%",
                    "price":  hour_avg
                })
            return state

        in_protect_hours = TESLA_PROTECT_START <= now_hour < TESLA_PROTECT_END

        # Capacity peak — stop charging no exceptions
        if is_capacity_peak:
            if is_charging:
                set_tesla_charging(False)
                state.setdefault("tesla_events", []).append({
                    "time":   datetime.now().strftime("%I:%M %p"),
                    "action": "stopped",
                    "reason": f"Capacity peak hour — price {hour_avg:.2f}¢",
                    "price":  hour_avg
                })
                state["tesla_paused"] = True
            return state

        # Protected hours noon-7 PM
        if in_protect_hours:
            if hour_avg <= TESLA_CHARGE_PRICE:
                if not is_charging:
                    set_tesla_charging(True)
                    state.setdefault("tesla_events", []).append({
                        "time":   datetime.now().strftime("%I:%M %p"),
                        "action": "started",
                        "reason": f"Price {hour_avg:.2f}¢ — too cheap to ignore",
                        "price":  hour_avg
                    })
                    logging.info(f"Charging during peak hours — price {hour_avg:.2f}¢")
            else:
                if is_charging:
                    set_tesla_charging(False)
                    state.setdefault("tesla_events", []).append({
                        "time":   datetime.now().strftime("%I:%M %p"),
                        "action": "stopped",
                        "reason": f"Protected hours (noon-7PM), price {hour_avg:.2f}¢",
                        "price":  hour_avg
                    })
                    state["tesla_paused"] = True
            return state

        # Outside protected hours
        if hour_avg > TESLA_STOP_PRICE:
            if is_charging:
                set_tesla_charging(False)
                state.setdefault("tesla_events", []).append({
                    "time":   datetime.now().strftime("%I:%M %p"),
                    "action": "stopped",
                    "reason": f"Price spike {hour_avg:.2f}¢ > {TESLA_STOP_PRICE}¢",
                    "price":  hour_avg
                })
                state["tesla_paused"] = True
                logging.info(f"Price spike {hour_avg:.2f}¢ — stopped Tesla charging")

        elif hour_avg <= TESLA_RESUME_PRICE:
            if not is_charging and battery < TESLA_MAX_BATTERY:
                set_tesla_charging(True)
                state.setdefault("tesla_events", []).append({
                    "time":   datetime.now().strftime("%I:%M %p"),
                    "action": "started",
                    "reason": f"Price {hour_avg:.2f}¢ ≤ resume threshold {TESLA_RESUME_PRICE}¢",
                    "price":  hour_avg
                })
                state["tesla_paused"] = False
                logging.info(f"Price {hour_avg:.2f}¢ — resumed Tesla charging")

        elif hour_avg <= TESLA_CHARGE_PRICE:
            if not is_charging and battery < TESLA_MAX_BATTERY:
                set_tesla_charging(True)
                state.setdefault("tesla_events", []).append({
                    "time":   datetime.now().strftime("%I:%M %p"),
                    "action": "started",
                    "reason": f"Price {hour_avg:.2f}¢ ≤ cheap threshold {TESLA_CHARGE_PRICE}¢",
                    "price":  hour_avg
                })
                logging.info(f"Cheap price {hour_avg:.2f}¢ — charging Tesla")

    except Exception as e:
        logging.error(f"Tesla charging error: {e}")

    return state

# ── EMAIL ─────────────────────────────────────────────────────────────────────

def send_email(subject, body):
    try:
        for recipient in NOTIFY_EMAILS:
            msg = MIMEMultipart()
            msg["From"]    = GMAIL_USER
            msg["To"]      = recipient
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(GMAIL_USER, GMAIL_PASS)
                server.send_message(msg)
        logging.info(f"Email sent: {subject}")
    except Exception as e:
        logging.error(f"Email failed: {e}")

def send_morning_report(state):
    """8 AM charging report covering overnight + any daytime events."""
    try:
        tesla       = get_tesla_state()
        battery_now = tesla["battery"]
        start_batt  = state.get("overnight_start_battery", 0)
        o_prices    = state.get("overnight_prices", [])
        avg_price   = sum(o_prices) / len(o_prices) if o_prices else 0

        # Estimate kWh charged overnight (Tesla ~0.25kWh/% for avg battery)
        pct_charged = battery_now - start_batt
        kwh_charged = max(0, pct_charged * 0.25)
        savings     = (FLAT_RATE - avg_price) / 100 * kwh_charged

        # Tonight's prediction
        pred_price, pred_note = get_tonight_price_prediction()

        # Build events section
        events = state.get("tesla_events", [])
        overnight_events = [e for e in events if _is_overnight_time(e["time"])]
        daytime_events   = [e for e in events if not _is_overnight_time(e["time"])]

        overnight_section = ""
        if overnight_events:
            overnight_section = "\nOVERNIGHT EVENTS:\n"
            for e in overnight_events:
                overnight_section += f"  {e['time']} — Charging {e['action']}: {e['reason']}\n"

        daytime_section = ""
        if daytime_events:
            daytime_section = "\nDAYTIME INTERACTIONS:\n"
            for e in daytime_events:
                daytime_section += f"  {e['time']} — Charging {e['action']}: {e['reason']}\n"
        else:
            daytime_section = "\nDAYTIME INTERACTIONS:\nNone\n"

        send_email(
            f"🚗 Daily Charging Report — {datetime.now().strftime('%B %d')}",
            f"OVERNIGHT CHARGING (12:00 AM – 6:00 AM):\n"
            f"Average price paid: {avg_price:.2f}¢/kWh\n"
            f"vs flat rate:       {FLAT_RATE}¢/kWh\n"
            f"Potential savings:  ${savings:.2f}\n"
            f"{overnight_section}"
            f"\nBATTERY:\n"
            f"Started night at: {start_batt:.0f}%\n"
            f"Current level:    {battery_now:.0f}%\n"
            f"Charged:          {pct_charged:.0f}% ({kwh_charged:.1f} kWh est.)\n"
            f"{daytime_section}"
            f"\nTONIGHT'S PREDICTION:\n"
            f"{pred_note}\n"
            f"Estimated price: {f'{pred_price:.1f}¢' if pred_price else 'Unknown'}"
        )

        # Reset overnight tracking
        state["overnight_start_battery"] = None
        state["overnight_prices"]        = []
        state["tesla_events"]            = []

    except Exception as e:
        logging.error(f"Morning report error: {e}")

    return state

def _is_overnight_time(time_str):
    """Check if a time string is between midnight and 6 AM."""
    try:
        hour = int(time_str.split(":")[0])
        ampm = time_str.split(" ")[1] if " " in time_str else "AM"
        if ampm == "AM" and hour != 12:
            return hour < 6
        return False
    except:
        return False

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    now          = datetime.now()
    current_hour = now.hour
    state        = load_state()

    # Get prices
    price_5min, hour_avg = get_comed_price()
    tier = price_tier(hour_avg)

    # Track hourly prices
    daily = state.setdefault("daily_hours", {})
    daily[str(current_hour)] = hour_avg
    state["daily_hours"] = daily

    # Get current outdoor temp
    try:
        weather_now    = get_weather()
        current_temp_c = weather_now["hourly"]["temperature_2m"][0]
    except:
        current_temp_c = 25.0

    # Capacity peak detection
    is_capacity_peak = is_potential_peak_hour(price_5min, hour_avg)
    if is_capacity_peak:
        state = handle_capacity_peak(state, hour_avg, current_temp_c)
        tier  = "peak"

    # Tesla charging management
    state = handle_tesla_charging(hour_avg, state, is_capacity_peak)

    # Low price alert (once per window)
    if hour_avg <= PRICE_LOW:
        if not state.get("low_alert_sent"):
            send_email(
                f"⚡ Low Price Alert: {hour_avg:.2f}¢/kWh",
                f"Current ComEd price is {hour_avg:.2f}¢/kWh\n"
                f"Great time to run dishwasher, laundry!\n\n"
                f"5-minute price: {price_5min:.2f}¢"
            )
            state["low_alert_sent"] = True
    else:
        state["low_alert_sent"] = False

    # 8 AM morning charging report
    if current_hour == 8 and now.minute < 6:
        if not state.get("morning_report_sent"):
            state = send_morning_report(state)
            state["morning_report_sent"] = True
    elif current_hour == 9:
        state["morning_report_sent"] = False

    # 5 PM forecast email
    if current_hour == 17 and now.minute < 6:
        if not state.get("forecast_sent"):
            precool, forecast = should_precool()
            pred_price, pred_note = get_tonight_price_prediction()
            if forecast:
                action = ""
                if forecast["very_expensive"]:
                    action = "🥶 Pre-cooling to 22°C tonight!"
                elif forecast["expensive"]:
                    action = "❄️ Pre-cooling slightly tonight."
                else:
                    action = "✅ Normal prices expected."

                send_email(
                    f"🌤️ Evening Forecast — {(datetime.now() + timedelta(days=1)).strftime('%B %d')}",
                    f"TOMORROW'S WEATHER:\n"
                    f"Forecast high:    {forecast['max_temp']:.1f}°C\n"
                    f"Afternoon peak:   {forecast['max_afternoon']:.1f}°C\n"
                    f"Price prediction: {'⚠️ Expensive afternoon' if forecast['expensive'] else '✅ Normal prices'}\n"
                    f"{action}\n\n"
                    f"TONIGHT'S CHARGING:\n"
                    f"{pred_note}\n"
                    f"Estimated price: {f'{pred_price:.1f}¢' if pred_price else 'Unknown'}\n\n"
                    f"Tip: Peak hours to avoid heavy usage tomorrow: 2 PM – 6 PM"
                )
            state["forecast_sent"] = True
    elif current_hour == 18:
        state["forecast_sent"] = False

    # 10:30 PM daily savings report
    if current_hour == 22 and now.minute >= 30:
        if not state.get("daily_report_sent"):
            hours_data = [v for v in daily.values() if isinstance(v, float)]
            if hours_data:
                avg_today  = sum(hours_data) / len(hours_data)
                hours_beat = sum(1 for h in hours_data if h < FLAT_RATE)
                kwh_used   = len(hours_data) * 1.5
                savings    = (FLAT_RATE - avg_today) / 100 * kwh_used

                # Tesla summary
                events     = state.get("tesla_events", [])
                n_paused   = sum(1 for e in events if e["action"] == "stopped")
                n_resumed  = sum(1 for e in events if e["action"] == "started")
                peaks_today = sum(
                    1 for k in state.get("capacity_peaks", {})
                    if k.startswith(f"peak_{now.strftime('%Y%m%d')}")
                )

                try:
                    tesla       = get_tesla_state()
                    battery_now = tesla["battery"]
                    tesla_line  = f"Current battery: {battery_now:.0f}%\n"
                except:
                    tesla_line  = ""

                send_email(
                    f"📊 Daily Energy Report — {now.strftime('%B %d')}",
                    f"HOUSE:\n"
                    f"Average supply price: {avg_today:.2f}¢/kWh\n"
                    f"Old flat rate:        {FLAT_RATE}¢/kWh\n"
                    f"Beat flat rate:       {hours_beat}/{len(hours_data)} hours\n"
                    f"Estimated savings:    ${savings:.2f}\n\n"
                    f"🚗 TESLA:\n"
                    f"{tesla_line}"
                    f"Charging paused:  {n_paused} time(s)\n"
                    f"Charging resumed: {n_resumed} time(s)\n"
                    f"Capacity peaks:   {peaks_today} detected today\n\n"
                    f"TOMORROW:\n"
                    f"Check your 5 PM forecast email for details."
                )
            state["daily_report_sent"] = True

    elif current_hour == 23:
        state["daily_report_sent"] = False
        state["daily_hours"]       = {}

    # Pre-cool overnight if tomorrow is expensive
    precool, forecast = should_precool()
    if precool and is_sleep_time():
        target_cool = 20.0 if forecast["very_expensive"] else 21.0
        set_temperature(19.0, target_cool)
        logging.info(f"Pre-cooling for tomorrow: {target_cool}°C")
        state["last_tier"]        = "precool"
        state["last_change_hour"] = current_hour
        save_state(state)
        return

    # Thermostat adjustment (max once per hour)
    if state.get("last_change_hour") != current_hour or state.get("last_tier") != tier:
        ha_state = get_ha_state()
        preset   = ha_state["attributes"].get("preset_mode", "")

        if "away" in preset.lower():
            set_temperature(18.0, min(26.0, CAT_MAX_C))
            logging.info("Away mode active")
        else:
            sp = SLEEP_SETPOINTS[tier] if is_sleep_time() else SETPOINTS[tier]
            set_temperature(sp["heat"], sp["cool"])

        state["last_tier"]        = tier
        state["last_change_hour"] = current_hour

    save_state(state)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        raise
