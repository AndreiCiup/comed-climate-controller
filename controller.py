#!/usr/bin/env python3
"""
ComEd Hourly Price -> Ecobee + Tesla Climate & Charging Controller
Runs every 5 minutes via run.sh
"""

import requests
import json
import logging
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# -- CONFIG -------------------------------------------------------------------
try:
    from secrets import HA_TOKEN, GMAIL_APP_PASSWORD, GMAIL_USER, NOTIFY_EMAILS
except ImportError:
    HA_TOKEN          = ""
    GMAIL_APP_PASSWORD = ""
    GMAIL_USER        = ""
    NOTIFY_EMAILS     = []

HA_URL          = "http://homeassistant.local:8123"

CLIMATE_ENTITY  = "climate.my_ecobee"

# Tesla entities
TESLA_CHARGE_SWITCH    = "switch.lady_t_charge"
TESLA_CHARGING_SENSOR  = "sensor.lady_t_charging"
TESLA_BATTERY_SENSOR   = "sensor.lady_t_battery_level"
TESLA_LOCATION         = "device_tracker.lady_t_location"
TESLA_WAKE_BUTTON      = "button.lady_t_wake"
WALL_CONNECTOR_POWER   = "sensor.wall_connector_power"
TESLA_MAX_BATTERY      = 85.0
TESLA_CHARGE_PRICE     = 2.0
TESLA_STOP_PRICE_DAY   = 3.5
TESLA_STOP_PRICE_NIGHT = 6.0
TESLA_PROTECT_START    = 12
TESLA_PROTECT_END      = 19
TESLA_KWH_PER_PERCENT  = 0.83

# Gmail
try:
    from secrets import HA_TOKEN, GMAIL_APP_PASSWORD, GMAIL_USER, NOTIFY_EMAILS
except ImportError:
    HA_TOKEN = ""
    GMAIL_APP_PASSWORD = ""
    GMAIL_USER = ""
    NOTIFY_EMAILS = []

# Baseline
FLAT_RATE = 11.5

# Cat safety
CAT_MAX_C = 25.0

# Price thresholds
PRICE_FREE   = 1.0
PRICE_LOW    = 5.0
PRICE_NORMAL = 9.0
PRICE_HIGH   = 12.0

# Capacity
CAPACITY_MONTHS   = [6, 7, 8, 9]
CAPACITY_HOURS    = range(12, 19)
CAPACITY_TEMP_C   = 30.0
CAPACITY_PRICE    = 10.0
CAPACITY_DAY_TEMP = 30.0

# Location - Aurora IL 60504
LATITUDE  = 41.7421
LONGITUDE = -88.2456

# Dynamic thermostat scale daytime centered on 23.5C
DYNAMIC_COOL = [
    (-99,  1.0, 22.0),
    (1.0,  3.0, 22.5),
    (3.0,  5.0, 23.0),
    (5.0,  8.0, 23.5),
    (8.0,  10.0, 24.0),
    (10.0, 12.0, 24.5),
    (12.0, 99,   25.0),
]

# Dynamic thermostat scale sleep centered on 21.5C
DYNAMIC_COOL_SLEEP = [
    (-99,  3.0, 20.5),
    (3.0,  5.0, 21.0),
    (5.0,  8.0, 21.5),
    (8.0,  12.0, 22.0),
    (12.0, 99,   23.0),
]

DYNAMIC_HEAT           = 19.5
THERMOSTAT_UPDATE_MINS = 20

# Counters
COUNTERS_FILE = "/config/comed_ecobee/counters.json"

logging.basicConfig(
    filename="/config/comed_ecobee/controller.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# -- HVAC SAVINGS -------------------------------------------------------------

def get_hvac_kwh_per_hour(outdoor_temp_c):
    if outdoor_temp_c > 32:    return 2.39
    elif outdoor_temp_c > 28:  return 1.88
    elif outdoor_temp_c > 22:  return 1.20
    else:                      return 0.34

# -- STATE --------------------------------------------------------------------

def load_state():
    try:
        with open("/config/comed_ecobee/state.json", "r") as f:
            return json.load(f)
    except:
        return {
            "last_cool_setpoint":      23.5,
            "last_thermostat_update":  0,
            "low_alert_sent":          False,
            "daily_hours":             {},
            "capacity_peaks":          {},
            "capacity_day":            False,
            "capacity_day_checked":    False,
            "tesla_paused":            False,
            "tesla_events":            [],
            "overnight_prices":        [],
            "overnight_start_battery": None,
            "morning_report_sent":     False,
            "forecast_sent":           False,
            "daily_report_sent":       False,
            "tesla_last_check_hour":   -1,
            "tesla_last_check_min":    -1,
            "last_hour_avg":           0.0,
            "tesla_plugged_in":        False,
            "tesla_wake_hour":         -1,
        }

def save_state(state):
    with open("/config/comed_ecobee/state.json", "w") as f:
        json.dump(state, f)

# -- COUNTERS -----------------------------------------------------------------

def load_counters():
    try:
        with open(COUNTERS_FILE, "r") as f:
            return json.load(f)
    except:
        return {
            "start_date":              datetime.now().strftime("%Y-%m-%d"),
            "thermostat_cooled":       0,
            "thermostat_raised":       0,
            "tesla_started":           0,
            "tesla_stopped_spike":     0,
            "tesla_stopped_protected": 0,
            "tesla_stopped_capacity":  0,
            "capacity_peaks":          0,
            "precool_triggered":       0,
            "total_savings_house":     0.0,
            "total_savings_tesla":     0.0
        }

def save_counters(counters):
    with open(COUNTERS_FILE, "w") as f:
        json.dump(counters, f)
    # Also copy to www for dashboard access
    try:
        with open("/config/www/counters.json", "w") as f:
            json.dump(counters, f)
    except:
        pass

# -- HELPERS ------------------------------------------------------------------

def is_sleep_time():
    return datetime.now().hour >= 22 or datetime.now().hour < 6

def is_overnight_charging_window():
    return 0 <= datetime.now().hour < 6

def get_dynamic_cool(price, sleep=False):
    scale = DYNAMIC_COOL_SLEEP if sleep else DYNAMIC_COOL
    for low, high, setpoint in scale:
        if low <= price < high:
            return setpoint
    return 23.5

def smooth_setpoint(current, target, max_step=1.0):
    if target > current:
        return min(current + max_step, target)
    elif target < current:
        return max(current - max_step, target)
    return current

def minutes_since(timestamp):
    return (time.time() - timestamp) / 60

# -- PRICE --------------------------------------------------------------------

def get_comed_price():
    for attempt in range(2):
        try:
            r = requests.get(
                "https://hourlypricing.comed.com/api?type=5minutefeed",
                timeout=30
            )
            r.raise_for_status()
            current = float(r.json()[0]["price"])
            r2 = requests.get(
                "https://hourlypricing.comed.com/api?type=currenthouraverage",
                timeout=30
            )
            r2.raise_for_status()
            avg = float(r2.json()[0]["price"])
            logging.info(f"5-min price: {current:.2f}c | Hour avg: {avg:.2f}c")
            return current, avg
        except Exception as e:
            if attempt == 0:
                logging.warning(f"Price fetch failed, retrying: {e}")
                time.sleep(30)
            else:
                logging.error(f"Price fetch failed twice: {e}")
                raise

def price_tier(price):
    if price < PRICE_FREE:     return "free"
    elif price < PRICE_LOW:    return "low"
    elif price < PRICE_NORMAL: return "normal"
    elif price < PRICE_HIGH:   return "high"
    else:                      return "peak"

# -- WEATHER ------------------------------------------------------------------

def get_weather():
    for attempt in range(2):
        try:
            r = requests.get(
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={LATITUDE}&longitude={LONGITUDE}"
                f"&hourly=temperature_2m"
                f"&temperature_unit=celsius"
                f"&timezone=America%2FChicago"
                f"&forecast_days=2",
                timeout=30
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 0:
                logging.warning(f"Weather fetch failed, retrying: {e}")
                time.sleep(30)
            else:
                logging.warning(f"Weather fetch failed twice: {e}")
                return None

def analyze_tomorrow_weather():
    data = get_weather()
    if not data:
        return None
    times    = data["hourly"]["time"]
    temps    = data["hourly"]["temperature_2m"]
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    t_temps  = [temps[i] for i, t in enumerate(times) if t.startswith(tomorrow)]
    if not t_temps:
        return None
    max_temp      = max(t_temps)
    afternoon     = t_temps[13:19]
    max_afternoon = max(afternoon) if afternoon else max_temp
    return {
        "max_temp":       max_temp,
        "max_afternoon":  max_afternoon,
        "expensive":      max_afternoon > 28.0,
        "very_expensive": max_afternoon > 32.0,
        "capacity_day":   max_afternoon > CAPACITY_DAY_TEMP,
    }

def get_tonight_price_prediction():
    try:
        f = analyze_tomorrow_weather()
        if not f:
            return None, "Forecast unavailable"
        if f["very_expensive"]: return 8.0, "Potentially expensive tonight"
        elif f["expensive"]:    return 5.0, "Moderate prices expected tonight"
        else:                   return 3.0, "Cheap prices expected tonight"
    except:
        return None, "Forecast unavailable"

def should_precool():
    f = analyze_tomorrow_weather()
    if not f:
        return False, None
    return f["expensive"] or f["very_expensive"], f

def should_precool_aggressive(hour_avg):
    """
    Aggressive pre-cooling — triggered when price is cheap AND tomorrow is hot.
    Returns (should_precool, target_temp)
    """
    if hour_avg > 3.0:
        return False, None
    f = analyze_tomorrow_weather()
    if not f:
        return False, None
    max_tomorrow = f.get("max_afternoon", 0)
    if max_tomorrow > 30.0:
        return True, 19.5   # very hot tomorrow + cheap now → 19.5°C
    elif max_tomorrow > 25.0:
        return True, 20.0   # hot tomorrow + cheap now → 20°C
    return False, None

# -- ECOBEE -------------------------------------------------------------------

def get_ha_state():
    for attempt in range(2):
        try:
            headers = {"Authorization": f"Bearer {HA_TOKEN}"}
            r = requests.get(
                f"{HA_URL}/api/states/{CLIMATE_ENTITY}",
                headers=headers, timeout=30
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 0:
                logging.warning(f"HA fetch failed, retrying: {e}")
                time.sleep(30)
            else:
                logging.error(f"HA fetch failed twice: {e}")
                raise

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
            "entity_id":        CLIMATE_ENTITY,
            "target_temp_high": cool_f,
            "target_temp_low":  heat_f
        }
    for attempt in range(2):
        try:
            r = requests.post(
                f"{HA_URL}/api/services/climate/set_temperature",
                headers=headers, json=payload, timeout=30
            )
            r.raise_for_status()
            logging.info(f"Set temps -> heat: {heat_c}C ({heat_f}F), cool: {cool_c}C ({cool_f}F)")
            return
        except Exception as e:
            if attempt == 0:
                logging.warning(f"Set temp failed, retrying: {e}")
                time.sleep(15)
            else:
                logging.error(f"Set temp failed twice: {e}")
                raise

# -- CAPACITY -----------------------------------------------------------------
SAVINGS_HISTORY_FILE = "/config/www/savings_history.json"

def snapshot_daily_savings():
    """Called at midnight — saves today's savings contribution to history."""
    try:
        import json as _json
        counters = load_counters()
        total_now = round(
            counters.get("total_savings_house", 0) +
            counters.get("total_savings_tesla", 0), 2
        )

        # Load existing history
        history = []
        if os.path.exists(SAVINGS_HISTORY_FILE):
            with open(SAVINGS_HISTORY_FILE, "r") as f:
                data = _json.load(f)
                history = data.get("daily", [])

        # Today's contribution = total now minus last snapshot total
        last_total = history[-1]["cumulative"] if history else 0.0
        today_savings = round(max(0, total_now - last_total), 2)

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        # Add today's entry
        history.append({
            "date":       yesterday,
            "house":      round(counters.get("total_savings_house", 0) - sum(d.get("house", 0) for d in history), 2),
            "tesla":      round(counters.get("total_savings_tesla", 0) - sum(d.get("tesla", 0) for d in history), 2),
            "total":      today_savings,
            "cumulative": total_now
        })

        # Write back
        with open(SAVINGS_HISTORY_FILE, "w") as f:
            _json.dump({"daily": history}, f)

        logging.info(f"Daily snapshot: ${today_savings:.2f} today, ${total_now:.2f} cumulative")
    except Exception as e:
        logging.error(f"Snapshot error: {e}")

def check_capacity_day(state):
    now = datetime.now()
    if now.hour == 0 and not state.get("daily_snapshot_done"):
        snapshot_daily_savings()
        state["daily_snapshot_done"] = True
    if now.hour == 1:
        state["daily_snapshot_done"] = False
    if now.hour == 0 and not state.get("capacity_day_checked"):
        f = analyze_tomorrow_weather()
        if f and f["capacity_day"] and now.month in CAPACITY_MONTHS:
            state["capacity_day"] = True
            logging.info("Tomorrow flagged as capacity day")
            send_email(
                "Warning: Capacity Day Tomorrow",
                f"Tomorrow afternoon forecast: {f['max_afternoon']:.1f}C\n\n"
                f"System will:\n"
                f"- Hold thermostat at 23.5C all day\n"
                f"- Stop Tesla charging (no exceptions)\n"
                f"- No pre-cooling below comfort baseline\n\n"
                f"Avoid heavy appliance use 2-6 PM tomorrow."
            )
        else:
            state["capacity_day"] = False
        state["capacity_day_checked"] = True
    if now.hour == 1:
        state["capacity_day_checked"] = False
    return state

def is_potential_peak_hour(price, hour_avg):
    now = datetime.now()
    if now.month not in CAPACITY_MONTHS: return False
    if now.hour not in CAPACITY_HOURS:   return False
    if now.weekday() >= 5:               return False
    try:
        w = get_weather()
        if w and w["hourly"]["temperature_2m"][0] < CAPACITY_TEMP_C:
            return False
    except:
        pass
    return hour_avg >= CAPACITY_PRICE

def handle_capacity_peak(state, hour_avg, current_temp):
    peak_key = f"peak_{datetime.now().strftime('%Y%m%d%H')}"
    if peak_key in state.get("capacity_peaks", {}):
        return state
    state.setdefault("capacity_peaks", {})[peak_key] = {
        "price": hour_avg, "temp": current_temp,
        "time":  datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    this_month  = datetime.now().strftime("%Y%m")
    peaks_month = sum(1 for k in state["capacity_peaks"] if k.startswith(f"peak_{this_month}"))
    state.setdefault("tesla_events", []).append({
        "time":   datetime.now().strftime("%I:%M %p"),
        "action": "stopped",
        "reason": f"Capacity peak - price {hour_avg:.2f}c, temp {current_temp:.1f}C",
        "price":  hour_avg
    })

    counters = load_counters()
    counters["capacity_peaks"] += 1
    save_counters(counters)

    send_email(
        "Warning: Capacity Peak - Reduce Usage Now!",
        f"Price: {hour_avg:.2f}c/kWh | Temp: {current_temp:.1f}C\n"
        f"Time: {datetime.now().strftime('%I:%M %p')}\n\n"
        f"- Avoid dishwasher, laundry, oven\n"
        f"- Tesla charging stopped\n"
        f"- Thermostat held at baseline\n\n"
        f"Peak hours this month: {peaks_month}"
    )
    logging.info(f"Capacity peak: {hour_avg:.2f}c, {current_temp:.1f}C")
    return state

# -- TESLA --------------------------------------------------------------------

def get_tesla_state():
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}

    r_loc = requests.get(
        f"{HA_URL}/api/states/{TESLA_LOCATION}",
        headers=headers, timeout=30
    )
    r_loc.raise_for_status()
    location = r_loc.json()["state"]

    r_chg = requests.get(
        f"{HA_URL}/api/states/{TESLA_CHARGING_SENSOR}",
        headers=headers, timeout=30
    )
    r_chg.raise_for_status()
    charging_state = r_chg.json()["state"]

    plugged_in = location == "home" and charging_state.lower() != "disconnected"
    logging.info(f"Location: {location} | Charging: {charging_state} | Plugged in: {plugged_in}")

    if not plugged_in:
        return {
            "plugged_in": False,
            "charging":   charging_state,
            "battery":    None,
            "switch":     "off",
            "wall_power": 0.0
        }

    r2 = requests.get(f"{HA_URL}/api/states/{TESLA_BATTERY_SENSOR}", headers=headers, timeout=30)
    r2.raise_for_status()
    r3 = requests.get(f"{HA_URL}/api/states/{TESLA_CHARGE_SWITCH}", headers=headers, timeout=30)
    r3.raise_for_status()

    try:
        r4 = requests.get(f"{HA_URL}/api/states/{WALL_CONNECTOR_POWER}", headers=headers, timeout=30)
        wall_power = float(r4.json()["state"])
    except:
        wall_power = 0.0

    try:
        battery_val = float(r2.json()["state"])
    except:
        battery_val = None

    return {
        "plugged_in": True,
        "charging":   charging_state,
        "battery":    battery_val,
        "switch":     r3.json()["state"],
        "wall_power": wall_power
    }

def wake_tesla():
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type":  "application/json"
    }
    try:
        r = requests.post(
            f"{HA_URL}/api/services/button/press",
            headers=headers,
            json={"entity_id": TESLA_WAKE_BUTTON},
            timeout=60
        )
        r.raise_for_status()
        logging.info("Tesla wake up sent - waiting 60s")
        time.sleep(60)
        return True
    except Exception as e:
        logging.error(f"Tesla wake up failed: {e}")
        return False

def should_check_tesla(hour_avg, state):
    now             = datetime.now()
    hour            = now.hour
    minute          = now.minute
    last_avg        = state.get("last_hour_avg", 0.0)
    last_check_hour = state.get("tesla_last_check_hour", -1)

    thresholds = [TESLA_CHARGE_PRICE, TESLA_STOP_PRICE_DAY, TESLA_STOP_PRICE_NIGHT]
    for t in thresholds:
        if (last_avg <= t < hour_avg) or (last_avg >= t > hour_avg):
            return True

    if 0 <= hour < 6 and minute % 30 == 0:                return True
    if state.get("tesla_plugged_in") and minute % 30 == 0: return True
    if last_check_hour != hour:                            return True
    return False

def set_tesla_charging(enable, reason=""):
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type":  "application/json"
    }
    service = "turn_on" if enable else "turn_off"
    r = requests.post(
        f"{HA_URL}/api/services/switch/{service}",
        headers=headers,
        json={"entity_id": TESLA_CHARGE_SWITCH},
        timeout=30
    )
    r.raise_for_status()
    action = "Started" if enable else "Stopped"
    logging.info(f"{action} Tesla charging - {reason}" if reason else f"{action} Tesla charging")

    counters = load_counters()
    if enable:
        counters["tesla_started"] += 1
    else:
        reason_lower = reason.lower()
        if "spike" in reason_lower or "stop threshold" in reason_lower:
            counters["tesla_stopped_spike"] += 1
        elif "capacity" in reason_lower:
            counters["tesla_stopped_capacity"] += 1
        elif "protected" in reason_lower or "noon" in reason_lower:
            counters["tesla_stopped_protected"] += 1
        else:
            counters["tesla_stopped_spike"] += 1
    save_counters(counters)

def handle_tesla_charging(hour_avg, state, is_capacity_peak, is_capacity_day):
    try:
        if not should_check_tesla(hour_avg, state):
            return state

        state["tesla_last_check_hour"] = datetime.now().hour
        state["tesla_last_check_min"]  = datetime.now().minute
        state["last_hour_avg"]         = hour_avg

        tesla = get_tesla_state()
        state["tesla_plugged_in"] = tesla["plugged_in"]

        if not tesla["plugged_in"]:
            logging.info("Tesla not plugged in - skipping charging logic")
            return state

        battery     = tesla["battery"]
        is_charging = tesla["switch"] == "on"
        now_hour    = datetime.now().hour

        if battery is None:
            if hour_avg <= TESLA_CHARGE_PRICE and not is_capacity_day and not is_capacity_peak:
                if state.get("tesla_wake_hour") == now_hour:
                    logging.info("Already attempted wake this hour - skipping")
                    return state
                state["tesla_wake_hour"] = now_hour
                logging.info(f"Tesla asleep - price {hour_avg:.2f}c is cheap, waking up")
                if wake_tesla():
                    tesla   = get_tesla_state()
                    battery = tesla["battery"]
                    if battery is None:
                        logging.info("Tesla still waking up - will retry next cycle")
                        return state
                else:
                    logging.info("Wake up failed - will retry next cycle")
                    return state
            elif is_charging:
                in_night   = is_overnight_charging_window()
                stop_price = TESLA_STOP_PRICE_NIGHT if in_night else TESLA_STOP_PRICE_DAY
                if hour_avg > stop_price:
                    logging.info(f"Tesla charging but price {hour_avg:.2f}c > {stop_price}c - waking to stop")
                    if wake_tesla():
                        tesla   = get_tesla_state()
                        battery = tesla["battery"]
                        if battery is None:
                            logging.info("Tesla still waking - will retry next cycle")
                            return state
                    else:
                        logging.info("Wake up failed - will retry next cycle")
                        return state
                else:
                    logging.info("Tesla charging, price ok - leaving asleep")
                    return state
            else:
                logging.info(f"Tesla asleep - price {hour_avg:.2f}c not cheap enough to wake")
                return state

        if is_overnight_charging_window():
            if state.get("overnight_start_battery") is None:
                state["overnight_start_battery"] = battery
                state["overnight_prices"] = []
            state.setdefault("overnight_prices", []).append(hour_avg)

        if battery >= TESLA_MAX_BATTERY:
            if is_charging:
                reason = f"Battery reached {battery:.0f}%"
                set_tesla_charging(False, reason)
                state.setdefault("tesla_events", []).append({
                    "time": datetime.now().strftime("%I:%M %p"),
                    "action": "stopped", "reason": reason, "price": hour_avg
                })
            return state

        in_night         = is_overnight_charging_window()
        in_protect_hours = TESLA_PROTECT_START <= now_hour < TESLA_PROTECT_END
        stop_price       = TESLA_STOP_PRICE_NIGHT if in_night else TESLA_STOP_PRICE_DAY

        if is_capacity_peak or is_capacity_day:
            if is_charging:
                reason = f"Capacity {'peak' if is_capacity_peak else 'day'} - price {hour_avg:.2f}c"
                set_tesla_charging(False, reason)
                state.setdefault("tesla_events", []).append({
                    "time": datetime.now().strftime("%I:%M %p"),
                    "action": "stopped", "reason": reason, "price": hour_avg
                })
                state["tesla_paused"] = True
            return state

        if hour_avg <= TESLA_CHARGE_PRICE:
            if not is_charging and battery < TESLA_MAX_BATTERY:
                reason = f"Cheap price {hour_avg:.2f}c - always charge below {TESLA_CHARGE_PRICE}c"
                set_tesla_charging(True, reason)
                state.setdefault("tesla_events", []).append({
                    "time": datetime.now().strftime("%I:%M %p"),
                    "action": "started", "reason": reason, "price": hour_avg
                })
                state["tesla_paused"] = False
            return state

        if hour_avg > stop_price:
            if is_charging:
                reason = f"Price {hour_avg:.2f}c > stop threshold {stop_price}c"
                set_tesla_charging(False, reason)
                state.setdefault("tesla_events", []).append({
                    "time": datetime.now().strftime("%I:%M %p"),
                    "action": "stopped", "reason": reason, "price": hour_avg
                })
                state["tesla_paused"] = True
            return state

        if in_protect_hours and hour_avg > TESLA_CHARGE_PRICE:
            if is_charging:
                reason = f"Protected hours noon-7PM - price {hour_avg:.2f}c"
                set_tesla_charging(False, reason)
                state.setdefault("tesla_events", []).append({
                    "time": datetime.now().strftime("%I:%M %p"),
                    "action": "stopped", "reason": reason, "price": hour_avg
                })
                state["tesla_paused"] = True
            return state

        if in_night and hour_avg <= TESLA_STOP_PRICE_NIGHT:
            if not is_charging and battery < TESLA_MAX_BATTERY:
                reason = f"Overnight window - price {hour_avg:.2f}c"
                set_tesla_charging(True, reason)
                state.setdefault("tesla_events", []).append({
                    "time": datetime.now().strftime("%I:%M %p"),
                    "action": "started", "reason": reason, "price": hour_avg
                })
                state["tesla_paused"] = False

    except Exception as e:
        logging.error(f"Tesla charging error: {e}")

    return state

# -- EMAIL --------------------------------------------------------------------

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
    try:
        tesla       = get_tesla_state()
        battery_now = tesla["battery"] if tesla["plugged_in"] and tesla["battery"] else 0
        start_batt  = state.get("overnight_start_battery") or 0
        o_prices    = state.get("overnight_prices", [])
        avg_price   = sum(o_prices) / len(o_prices) if o_prices else 0
        pct_charged = max(0, battery_now - start_batt)
        kwh_charged = pct_charged * TESLA_KWH_PER_PERCENT
        savings     = (FLAT_RATE - avg_price) / 100 * kwh_charged
        pred_price, pred_note = get_tonight_price_prediction()
        events           = state.get("tesla_events", [])
        overnight_events = [e for e in events if _is_overnight_time(e["time"])]
        daytime_events   = [e for e in events if not _is_overnight_time(e["time"])]

        o_section = "\nOVERNIGHT EVENTS:\nNone\n"
        if overnight_events:
            o_section = "\nOVERNIGHT EVENTS:\n"
            for e in overnight_events:
                o_section += f"  {e['time']} - Charging {e['action']}: {e['reason']}\n"

        d_section = "\nDAYTIME INTERACTIONS:\nNone\n"
        if daytime_events:
            d_section = "\nDAYTIME INTERACTIONS:\n"
            for e in daytime_events:
                d_section += f"  {e['time']} - Charging {e['action']}: {e['reason']}\n"

        counters = load_counters()
        counters["total_savings_tesla"] = round(
            counters.get("total_savings_tesla", 0) + max(0, savings), 2
        )
        save_counters(counters)

        send_email(
            f"Daily Charging Report - {datetime.now().strftime('%B %d')}",
            f"OVERNIGHT (12:00 AM - 6:00 AM):\n"
            f"Average price: {avg_price:.2f}c/kWh\n"
            f"vs flat rate:  {FLAT_RATE}c/kWh\n"
            f"Savings:       ${savings:.2f}\n"
            f"{o_section}"
            f"\nBATTERY:\n"
            f"Started: {start_batt:.0f}%\n"
            f"Current: {battery_now:.0f}%\n"
            f"Charged: {pct_charged:.0f}% ({kwh_charged:.1f} kWh est.)\n"
            f"{d_section}"
            f"\nTONIGHT:\n"
            f"{pred_note}\n"
            f"Est. price: {f'{pred_price:.1f}c' if pred_price else 'Unknown'}"
        )
        state["overnight_start_battery"] = None
        state["overnight_prices"]        = []
        state["tesla_events"]            = []
    except Exception as e:
        logging.error(f"Morning report error: {e}")
    return state

def _is_overnight_time(time_str):
    try:
        hour = int(time_str.split(":")[0])
        ampm = time_str.split(" ")[1] if " " in time_str else "AM"
        return ampm == "AM" and hour != 12 and hour < 6
    except:
        return False

# -- MAIN ---------------------------------------------------------------------

def main():
    now          = datetime.now()
    current_hour = now.hour
    state        = load_state()

    try:
        price_5min, hour_avg = get_comed_price()
    except:
        save_state(state)
        return

    daily = state.setdefault("daily_hours", {})
    daily[str(current_hour)] = hour_avg
    state["daily_hours"] = daily
    # ML data collection — record one row per hour
    try:
        import sys
        sys.path.insert(0, "/config/comed_ml")
        from collect_data import record_hour
        record_hour(hour_avg)
    except Exception as e:
        logging.warning(f"Data collection skipped: {e}")

    try:
        w              = get_weather()
        current_temp_c = w["hourly"]["temperature_2m"][0] if w else 25.0
    except:
        current_temp_c = 25.0

    state            = check_capacity_day(state)
    is_capacity_day  = state.get("capacity_day", False)
    is_capacity_peak = is_potential_peak_hour(price_5min, hour_avg)

    if is_capacity_peak:
        state = handle_capacity_peak(state, hour_avg, current_temp_c)

    state = handle_tesla_charging(hour_avg, state, is_capacity_peak, is_capacity_day)

    # Low price alert
    if hour_avg <= PRICE_LOW:
        if not state.get("low_alert_sent"):
            send_email(
                f"Low Price Alert: {hour_avg:.2f}c/kWh",
                f"Current price: {hour_avg:.2f}c/kWh\n"
                f"Great time to run dishwasher, laundry!\n"
                f"5-min price: {price_5min:.2f}c"
            )
            state["low_alert_sent"] = True
    else:
        state["low_alert_sent"] = False

    # 8 AM morning report
    if current_hour == 8 and now.minute < 6:
        if not state.get("morning_report_sent"):
            state = send_morning_report(state)
            state["morning_report_sent"] = True
    elif current_hour == 9:
        state["morning_report_sent"] = False

    # 5 PM forecast
    if current_hour == 17 and now.minute < 6:
        if not state.get("forecast_sent"):
            precool, forecast = should_precool()
            pred_price, pred_note = get_tonight_price_prediction()
            if forecast:
                if forecast["very_expensive"]:   action = "Pre-cooling to 22C tonight!"
                elif forecast["expensive"]:      action = "Pre-cooling slightly tonight."
                else:                            action = "Normal prices expected."
                cap_note = "\nWARNING: CAPACITY DAY TOMORROW - Avoid heavy usage 2-6 PM!" if forecast.get("capacity_day") else ""
                send_email(
                    f"Evening Forecast - {(now + timedelta(days=1)).strftime('%B %d')}",
                    f"TOMORROW:\n"
                    f"High: {forecast['max_temp']:.1f}C | Afternoon: {forecast['max_afternoon']:.1f}C\n"
                    f"{'Expensive afternoon expected' if forecast['expensive'] else 'Normal prices expected'}\n"
                    f"{action}{cap_note}\n\n"
                    f"TONIGHT CHARGING:\n"
                    f"{pred_note}\n"
                    f"Est. price: {f'{pred_price:.1f}c' if pred_price else 'Unknown'}"
                )
            state["forecast_sent"] = True
    elif current_hour == 18:
        state["forecast_sent"] = False

    # 10:30 PM daily report
    if current_hour == 22 and now.minute >= 30:
        if not state.get("daily_report_sent"):
            hours_data = [v for v in daily.values() if isinstance(v, float)]
            if hours_data:
                avg_today   = sum(hours_data) / len(hours_data)
                hours_beat  = sum(1 for h in hours_data if h < FLAT_RATE)
                kwh_per_hr  = get_hvac_kwh_per_hour(current_temp_c)
                savings     = (FLAT_RATE - avg_today) / 100 * (len(hours_data) * kwh_per_hr)
                events      = state.get("tesla_events", [])
                n_paused    = sum(1 for e in events if e["action"] == "stopped")
                n_resumed   = sum(1 for e in events if e["action"] == "started")
                peaks_today = sum(1 for k in state.get("capacity_peaks", {}) if k.startswith(f"peak_{now.strftime('%Y%m%d')}"))

                counters = load_counters()
                counters["total_savings_house"] = round(
                    counters.get("total_savings_house", 0) + max(0, savings), 2
                )
                save_counters(counters)

                try:
                    tesla      = get_tesla_state()
                    tesla_line = f"Battery: {tesla['battery']:.0f}%\n" if tesla["plugged_in"] and tesla["battery"] else ""
                except:
                    tesla_line = ""
                send_email(
                    f"Daily Energy Report - {now.strftime('%B %d')}",
                    f"HOUSE:\n"
                    f"Avg price:      {avg_today:.2f}c/kWh\n"
                    f"Flat rate:      {FLAT_RATE}c/kWh\n"
                    f"Beat rate:      {hours_beat}/{len(hours_data)} hours\n"
                    f"Est HVAC use:   {kwh_per_hr:.2f} kWh/hr\n"
                    f"Est savings:    ${savings:.2f}\n\n"
                    f"TESLA:\n"
                    f"{tesla_line}"
                    f"Paused:         {n_paused} time(s)\n"
                    f"Resumed:        {n_resumed} time(s)\n"
                    f"Cap peaks today:{peaks_today}\n\n"
                    f"See 5 PM email for tomorrow's forecast."
                )
            state["daily_report_sent"] = True
    elif current_hour == 23:
        state["daily_report_sent"] = False
        state["daily_hours"]       = {}

    # Pre-cool overnight — aggressive mode when price is cheap
    if is_sleep_time() and not is_capacity_day:
        aggressive, aggressive_target = should_precool_aggressive(hour_avg)
        if aggressive:
            set_temperature(DYNAMIC_HEAT, aggressive_target)
            logging.info(f"Aggressive pre-cool: {aggressive_target}C (price: {hour_avg:.2f}c)")
            counters = load_counters()
            counters["precool_triggered"] += 1
            save_counters(counters)
            state["last_cool_setpoint"]     = aggressive_target
            state["last_thermostat_update"] = time.time()
            save_state(state)
            return
        else:
            precool, forecast = should_precool()
            if precool:
                target_cool = 20.0 if forecast["very_expensive"] else 21.0
                set_temperature(DYNAMIC_HEAT, target_cool)
                logging.info(f"Pre-cooling for tomorrow: {target_cool}C")
                counters = load_counters()
                counters["precool_triggered"] += 1
                save_counters(counters)
                state["last_cool_setpoint"]     = target_cool
                state["last_thermostat_update"] = time.time()
                save_state(state)
                return

    # Dynamic thermostat
    try:
        ha_state = get_ha_state()
        preset   = ha_state["attributes"].get("preset_mode", "")
        mins_since_update = minutes_since(state.get("last_thermostat_update", 0))

        if "away" in preset.lower():
            if mins_since_update >= THERMOSTAT_UPDATE_MINS:
                set_temperature(18.0, min(26.0, CAT_MAX_C))
                state["last_thermostat_update"] = time.time()
                logging.info("Away mode active")

        elif is_capacity_peak:
            target_cool  = 23.5
        elif is_capacity_day and now.hour >= 12 and now.hour < 19:
            target_cool  = 23.5
            current_cool = state.get("last_cool_setpoint", 23.5)
            if abs(current_cool - target_cool) > 0.1 and mins_since_update >= THERMOSTAT_UPDATE_MINS:
                new_cool = smooth_setpoint(current_cool, target_cool)
                set_temperature(DYNAMIC_HEAT, new_cool)
                state["last_cool_setpoint"]     = new_cool
                state["last_thermostat_update"] = time.time()
                counters = load_counters()
                counters["thermostat_raised"] += 1
                save_counters(counters)
                logging.info(f"Capacity mode - holding baseline: {new_cool}C")

        else:
            sleep        = is_sleep_time()
            target_cool  = get_dynamic_cool(hour_avg, sleep)
            current_cool = state.get("last_cool_setpoint", 23.5)
            diff         = abs(current_cool - target_cool)

            if diff > 0.1 and mins_since_update >= THERMOSTAT_UPDATE_MINS:
                new_cool = smooth_setpoint(current_cool, target_cool)
                set_temperature(DYNAMIC_HEAT, new_cool)
                state["last_cool_setpoint"]     = new_cool
                state["last_thermostat_update"] = time.time()
                counters = load_counters()
                if new_cool < current_cool:
                    counters["thermostat_cooled"] += 1
                else:
                    counters["thermostat_raised"] += 1
                save_counters(counters)
                logging.info(f"Dynamic setpoint: {new_cool}C (target: {target_cool}C, price: {hour_avg:.2f}c)")

    except Exception as e:
        logging.error(f"Thermostat error: {e}")

    save_state(state)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
