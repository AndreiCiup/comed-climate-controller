#!/bin/sh

# Ensure Python and dependencies are installed on every boot
apk add --quiet python3 py3-pip py3-scikit-learn 2>/dev/null
pip3 install --quiet requests --break-system-packages 2>/dev/null

# Run the controller every 5 minutes
while true; do
    /usr/bin/python3 /config/comed_ecobee/controller.py
    sleep 300
done
