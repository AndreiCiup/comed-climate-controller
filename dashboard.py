#!/usr/bin/env python3
"""
ComEd Energy Dashboard
Accessible at http://climatecontrolledemo.local:5000
"""

from flask import Flask, jsonify, render_template_string
import requests
import json
import time
from datetime import datetime

app = Flask(__name__)

HA_URL     = "http://homeassistant.local:8123"
HA_TOKEN   = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJiODY1NDg5NzRlZWY0N2Q3YTgzMTQ2YjNiMmJmNjMzOCIsImlhdCI6MTc3OTg0NDk5OCwiZXhwIjoyMDk1MjA0OTk4fQ.4s-vIvvkyRblukNoeLIYqx4qv32vPXg_LywB_j_qnxo"
FLAT_RATE  = 11.5
LATITUDE   = 41.7421
LONGITUDE  = -88.2456

# ── DATA FETCHERS ─────────────────────────────────────────────────────────────

def get_comed_price():
    try:
        r = requests.get(
            "https://hourlypricing.comed.com/api?type=5minutefeed",
            timeout=10
        )
        current = float(r.json()[0]["price"])
        r2 = requests.get(
            "https://hourlypricing.comed.com/api?type=currenthouraverage",
            timeout=10
        )
        avg = float(r2.json()[0]["price"])
        return current, avg
    except:
        return None, None

def get_comed_today():
    """Get today's hourly prices for chart."""
    try:
        r = requests.get(
            "https://hourlypricing.comed.com/api?type=houraverage",
            timeout=10
        )
        data = r.json()
        return [{"time": d["millisUTC"], "price": float(d["price"])} for d in data]
    except:
        return []

def get_ha_entity(entity_id):
    try:
        headers = {"Authorization": f"Bearer {HA_TOKEN}"}
        r = requests.get(
            f"{HA_URL}/api/states/{entity_id}",
            headers=headers, timeout=10
        )
        return r.json()
    except:
        return None

def get_dashboard_data():
    price_5min, hour_avg = get_comed_price()

    # Price tier
    if hour_avg is None:
        tier = "unknown"
        tier_color = "#888"
    elif hour_avg < 0:
        tier = "NEGATIVE"
        tier_color = "#00ff88"
    elif hour_avg < 1:
        tier = "FREE"
        tier_color = "#00ff88"
    elif hour_avg < 3:
        tier = "VERY CHEAP"
        tier_color = "#00cc66"
    elif hour_avg < 5:
        tier = "CHEAP"
        tier_color = "#88cc00"
    elif hour_avg < 9:
        tier = "NORMAL"
        tier_color = "#ffaa00"
    elif hour_avg < 12:
        tier = "EXPENSIVE"
        tier_color = "#ff6600"
    else:
        tier = "PEAK"
        tier_color = "#ff2200"

    # Savings per hour
    savings_hr = (FLAT_RATE - hour_avg) / 100 * 1.5 if hour_avg else 0

    # Thermostat
    ecobee = get_ha_entity("climate.my_ecobee")
    if ecobee:
        indoor_temp_f = ecobee["attributes"].get("current_temperature", 0)
        indoor_temp_c = round((indoor_temp_f - 32) * 5/9, 1)
        target_f      = ecobee["attributes"].get("temperature", 0)
        target_c      = round((target_f - 32) * 5/9, 1) if target_f else 0
        hvac_mode     = ecobee["state"]
        hvac_action   = ecobee["attributes"].get("hvac_action", "idle")
        preset        = ecobee["attributes"].get("preset_mode", "home")
    else:
        indoor_temp_c = target_c = 0
        hvac_mode = hvac_action = preset = "unknown"

    # Tesla
    location_entity  = get_ha_entity("device_tracker.lady_t_location")
    battery_entity   = get_ha_entity("sensor.lady_t_battery_level")
    charging_entity  = get_ha_entity("sensor.lady_t_charging")
    switch_entity    = get_ha_entity("switch.lady_t_charge")
    power_entity     = get_ha_entity("sensor.wall_connector_power")

    tesla_location   = location_entity["state"] if location_entity else "unknown"
    tesla_battery    = battery_entity["state"] if battery_entity else "unknown"
    tesla_charging   = charging_entity["state"] if charging_entity else "unknown"
    tesla_switch     = switch_entity["state"] if switch_entity else "off"
    tesla_power      = power_entity["state"] if power_entity else "0"

    # Load state for savings
    try:
        with open("/config/comed_ecobee/state.json", "r") as f:
            state = json.load(f)
        daily_hours = state.get("daily_hours", {})
        hours_data  = [v for v in daily_hours.values() if isinstance(v, float)]
        avg_today   = sum(hours_data) / len(hours_data) if hours_data else 0
        savings_today = (FLAT_RATE - avg_today) / 100 * (len(hours_data) * 1.5)
        hours_beat  = sum(1 for h in hours_data if h < FLAT_RATE)
        capacity_peaks = len(state.get("capacity_peaks", {}))
    except:
        savings_today = avg_today = 0
        hours_beat    = 0
        capacity_peaks = 0

    # Recommendation
    recommendation = ""
    if hour_avg and hour_avg < 3:
        recommendation = "Great time to run dishwasher, laundry, or charge devices!"
    elif hour_avg and hour_avg < 5:
        recommendation = "Good price — comfortable to run appliances."
    elif hour_avg and hour_avg > 12:
        recommendation = "Peak price — avoid heavy appliance use."
    elif hour_avg and hour_avg > 9:
        recommendation = "High price — reduce usage if possible."
    else:
        recommendation = "Normal pricing — no action needed."

    return {
        "price_5min":     price_5min,
        "hour_avg":       hour_avg,
        "tier":           tier,
        "tier_color":     tier_color,
        "savings_hr":     round(savings_hr, 3),
        "indoor_temp_c":  indoor_temp_c,
        "target_c":       target_c,
        "hvac_mode":      hvac_mode,
        "hvac_action":    hvac_action,
        "preset":         preset,
        "tesla_location": tesla_location,
        "tesla_battery":  tesla_battery,
        "tesla_charging": tesla_charging,
        "tesla_switch":   tesla_switch,
        "tesla_power":    tesla_power,
        "savings_today":  round(savings_today, 2),
        "avg_today":      round(avg_today, 2),
        "hours_beat":     hours_beat,
        "total_hours":    len(hours_data) if 'hours_data' in dir() else 0,
        "capacity_peaks": capacity_peaks,
        "recommendation": recommendation,
        "last_updated":   datetime.now().strftime("%I:%M:%S %p"),
    }

# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    return jsonify(get_dashboard_data())

@app.route("/api/chart")
def api_chart():
    return jsonify(get_comed_today())

@app.route("/")
def index():
    return render_template_string(HTML)

# ── HTML TEMPLATE ─────────────────────────────────────────────────────────────

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Home Energy Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            background: #0f0f0f;
            color: #e0e0e0;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            min-height: 100vh;
            padding: 16px;
        }

        h1 {
            text-align: center;
            font-size: 1.1rem;
            color: #888;
            margin-bottom: 16px;
            letter-spacing: 2px;
            text-transform: uppercase;
        }

        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            max-width: 800px;
            margin: 0 auto;
        }

        .card {
            background: #1a1a1a;
            border-radius: 16px;
            padding: 16px;
            border: 1px solid #2a2a2a;
        }

        .card.full-width {
            grid-column: 1 / -1;
        }

        .card-title {
            font-size: 0.7rem;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }

        .price-big {
            font-size: 3.5rem;
            font-weight: 700;
            line-height: 1;
            margin-bottom: 4px;
        }

        .tier-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 600;
            margin-bottom: 8px;
        }

        .savings-line {
            font-size: 0.85rem;
            color: #aaa;
        }

        .savings-amount {
            color: #00cc66;
            font-weight: 600;
        }

        .stat-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 6px 0;
            border-bottom: 1px solid #2a2a2a;
            font-size: 0.85rem;
        }

        .stat-row:last-child { border-bottom: none; }

        .stat-label { color: #888; }

        .stat-value { font-weight: 500; }

        .status-dot {
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
        }

        .dot-green  { background: #00cc66; }
        .dot-yellow { background: #ffaa00; }
        .dot-red    { background: #ff2200; }
        .dot-grey   { background: #888; }

        .recommendation {
            background: #1a2a1a;
            border: 1px solid #2a4a2a;
            border-radius: 12px;
            padding: 12px 16px;
            font-size: 0.85rem;
            color: #aaddaa;
            text-align: center;
        }

        .updated {
            text-align: center;
            font-size: 0.7rem;
            color: #444;
            margin-top: 12px;
        }

        canvas { max-height: 150px; }

        @media (max-width: 400px) {
            .grid { grid-template-columns: 1fr; }
            .card.full-width { grid-column: 1; }
            .price-big { font-size: 2.8rem; }
        }
    </style>
</head>
<body>
    <h1>🏠 Home Energy — Aurora IL</h1>

    <div class="grid">

        <!-- CURRENT PRICE -->
        <div class="card">
            <div class="card-title">Current Price</div>
            <div class="price-big" id="price">--</div>
            <div>
                <span class="tier-badge" id="tier-badge">--</span>
            </div>
            <div class="savings-line">
                Saving <span class="savings-amount" id="savings-hr">--</span>/hr
            </div>
            <div class="savings-line" style="margin-top:4px">
                5-min: <span id="price-5min">--</span>
            </div>
        </div>

        <!-- TODAY'S SAVINGS -->
        <div class="card">
            <div class="card-title">Today's Savings</div>
            <div class="stat-row">
                <span class="stat-label">Est. savings</span>
                <span class="stat-value savings-amount" id="savings-today">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Avg price</span>
                <span class="stat-value" id="avg-today">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Beat flat rate</span>
                <span class="stat-value" id="hours-beat">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Cap peaks</span>
                <span class="stat-value" id="capacity-peaks">--</span>
            </div>
        </div>

        <!-- THERMOSTAT -->
        <div class="card">
            <div class="card-title">Thermostat</div>
            <div class="stat-row">
                <span class="stat-label">Indoor</span>
                <span class="stat-value" id="indoor-temp">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Setpoint</span>
                <span class="stat-value" id="target-temp">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Mode</span>
                <span class="stat-value" id="hvac-mode">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Status</span>
                <span class="stat-value" id="hvac-action">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Preset</span>
                <span class="stat-value" id="preset">--</span>
            </div>
        </div>

        <!-- TESLA -->
        <div class="card">
            <div class="card-title">Lady T 🚗</div>
            <div class="stat-row">
                <span class="stat-label">Location</span>
                <span class="stat-value" id="tesla-location">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Battery</span>
                <span class="stat-value" id="tesla-battery">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Charging</span>
                <span class="stat-value" id="tesla-charging">--</span>
            </div>
            <div class="stat-row">
                <span class="stat-label">Power</span>
                <span class="stat-value" id="tesla-power">--</span>
            </div>
        </div>

        <!-- PRICE CHART -->
        <div class="card full-width">
            <div class="card-title">Today's Prices</div>
            <canvas id="priceChart"></canvas>
        </div>

        <!-- RECOMMENDATION -->
        <div class="card full-width">
            <div class="recommendation" id="recommendation">Loading...</div>
        </div>

    </div>

    <div class="updated">Last updated: <span id="last-updated">--</span></div>

    <script>
        let chart = null;

        function getDotClass(action) {
            if (action === 'cooling' || action === 'heating') return 'dot-green';
            if (action === 'idle') return 'dot-yellow';
            return 'dot-grey';
        }

        function getTeslaColor(charging) {
            if (charging && charging.toLowerCase() === 'charging') return '#00cc66';
            if (charging && charging.toLowerCase() === 'stopped') return '#ffaa00';
            return '#888';
        }

        async function updateData() {
            try {
                const r = await fetch('/api/data');
                const d = await r.json();

                // Price
                document.getElementById('price').textContent =
                    d.hour_avg !== null ? d.hour_avg.toFixed(2) + '¢' : '--';
                document.getElementById('price').style.color = d.tier_color;
                document.getElementById('price-5min').textContent =
                    d.price_5min !== null ? d.price_5min.toFixed(2) + '¢' : '--';

                // Tier badge
                const badge = document.getElementById('tier-badge');
                badge.textContent = d.tier;
                badge.style.backgroundColor = d.tier_color + '22';
                badge.style.color = d.tier_color;
                badge.style.border = '1px solid ' + d.tier_color + '44';

                // Savings
                const savHr = d.savings_hr >= 0
                    ? '+$' + d.savings_hr.toFixed(3)
                    : '-$' + Math.abs(d.savings_hr).toFixed(3);
                document.getElementById('savings-hr').textContent = savHr;
                document.getElementById('savings-hr').style.color =
                    d.savings_hr >= 0 ? '#00cc66' : '#ff6600';

                // Today savings
                document.getElementById('savings-today').textContent =
                    '$' + d.savings_today.toFixed(2);
                document.getElementById('avg-today').textContent =
                    d.avg_today.toFixed(2) + '¢/kWh';
                document.getElementById('hours-beat').textContent =
                    d.hours_beat + '/' + d.total_hours + ' hrs';
                document.getElementById('capacity-peaks').textContent =
                    d.capacity_peaks + ' this month';

                // Thermostat
                document.getElementById('indoor-temp').textContent =
                    d.indoor_temp_c + '°C';
                document.getElementById('target-temp').textContent =
                    d.target_c + '°C';
                document.getElementById('hvac-mode').textContent =
                    d.hvac_mode;
                document.getElementById('hvac-action').innerHTML =
                    '<span class="status-dot ' + getDotClass(d.hvac_action) + '"></span>' +
                    d.hvac_action;
                document.getElementById('preset').textContent = d.preset;

                // Tesla
                document.getElementById('tesla-location').textContent =
                    d.tesla_location === 'home' ? '🏠 Home' : '🚗 Away';
                document.getElementById('tesla-battery').textContent =
                    d.tesla_battery + '%';
                document.getElementById('tesla-charging').innerHTML =
                    '<span style="color:' + getTeslaColor(d.tesla_charging) + '">' +
                    d.tesla_charging + '</span>';
                document.getElementById('tesla-power').textContent =
                    d.tesla_power + ' kW';

                // Recommendation
                document.getElementById('recommendation').textContent =
                    '💡 ' + d.recommendation;

                // Last updated
                document.getElementById('last-updated').textContent = d.last_updated;

            } catch(e) {
                console.error('Data fetch failed:', e);
            }
        }

        async function updateChart() {
            try {
                const r = await fetch('/api/chart');
                const data = await r.json();
                if (!data.length) return;

                const labels = data.map(d => {
                    const date = new Date(parseInt(d.time));
                    return date.getHours() + ':' +
                        String(date.getMinutes()).padStart(2, '0');
                });
                const prices = data.map(d => d.price);
                const colors = prices.map(p =>
                    p < 3 ? '#00cc66' :
                    p < 5 ? '#88cc00' :
                    p < 9 ? '#ffaa00' :
                    p < 12 ? '#ff6600' : '#ff2200'
                );

                if (chart) {
                    chart.data.labels = labels;
                    chart.data.datasets[0].data = prices;
                    chart.data.datasets[0].backgroundColor = colors;
                    chart.update();
                } else {
                    const ctx = document.getElementById('priceChart').getContext('2d');
                    chart = new Chart(ctx, {
                        type: 'bar',
                        data: {
                            labels: labels,
                            datasets: [{
                                data: prices,
                                backgroundColor: colors,
                                borderRadius: 4,
                            }]
                        },
                        options: {
                            responsive: true,
                            plugins: { legend: { display: false } },
                            scales: {
                                x: {
                                    ticks: {
                                        color: '#666',
                                        maxTicksLimit: 12,
                                        font: { size: 10 }
                                    },
                                    grid: { color: '#222' }
                                },
                                y: {
                                    ticks: {
                                        color: '#666',
                                        callback: v => v + '¢',
                                        font: { size: 10 }
                                    },
                                    grid: { color: '#222' }
                                }
                            }
                        }
                    });
                }
            } catch(e) {
                console.error('Chart fetch failed:', e);
            }
        }

        // Initial load
        updateData();
        updateChart();

        // Refresh data every 60 seconds
        setInterval(updateData, 60000);

        // Refresh chart every 5 minutes
        setInterval(updateChart, 300000);
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
