# ComEd Climate & Charging Controller

A Raspberry Pi script that automatically adjusts your Ecobee thermostat 
and Tesla charging based on ComEd hourly electricity prices.

## Features

- **Ecobee control** — adjusts temperature setpoints based on real-time price tiers
- **Tesla charging** — starts/stops charging based on price thresholds
- **Pre-cooling** — cools house overnight when tomorrow is predicted hot/expensive
- **Capacity peak protection** — reduces usage during high-demand summer hours
- **Email notifications** — low price alerts, daily savings reports, charging summaries
- **Weather integration** — uses Open-Meteo forecast to predict expensive days

## Price Tiers

| Price | Tier | Strategy |
|---|---|---|
| < 1¢ | Free | Pre-cool aggressively |
| 1–5¢ | Low | Normal comfort |
| 5–9¢ | Normal | Slight nudge |
| 9–12¢ | High | Reduce runtime |
| > 12¢ | Peak | Maximum savings |

## Tesla Charging Rules

| Situation | Action |
|---|---|
| Price ≤ 3¢ anytime | Charge |
| Price > 8¢ anytime | Stop charging |
| Price drops ≤ 5¢ | Resume charging |
| Noon–7 PM AND price > 3¢ | Stop charging |
| Capacity peak hour | Stop charging — no exceptions |
| Battery ≥ 85% | Stop charging |

## Email Notifications

- **8:00 AM** — overnight charging report
- **5:00 PM** — tomorrow's forecast + tonight's charging prediction
- **10:30 PM** — full day house + car savings summary
- **Anytime** — low price alert (≤ 5¢)
- **Anytime** — capacity peak alert (June–September)
- **Anytime** — Tesla charging paused/resumed

## Hardware

- Raspberry Pi 3
- 32GB microSD card
- Home Assistant OS
- Ecobee thermostat
- Tesla vehicle
- ComEd Hourly Pricing program

## Setup

1. Install Home Assistant OS on Raspberry Pi
2. Add Ecobee integration (no API key needed from HA 2026.3+)
3. Add Tesla Fleet integration
4. Configure Gmail App Password for notifications
5. Edit CONFIG section in controller.py
6. Run via run.sh

## Configuration

Edit the CONFIG section at the top of `controller.py`:

- `HA_TOKEN` — Home Assistant long-lived access token
- `GMAIL_USER` — Gmail address for sending notifications
- `GMAIL_PASS` — Gmail App Password
- `NOTIFY_EMAILS` — List of email addresses to receive notifications
- `FLAT_RATE` — Your old flat rate supply charge in cents/kWh
- `CAT_MAX_C` — Maximum temperature (pet safety ceiling)

## Files

- `controller.py` — main script
- `run.sh` — runs controller every 5 minutes
- `controller.log` — activity log
- `state.json` — persistent state (not uploaded to GitHub)
