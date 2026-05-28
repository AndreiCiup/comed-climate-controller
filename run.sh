#!/bin/sh
while true; do
    python3 /config/comed_ecobee/controller.py
    sleep 300
done
