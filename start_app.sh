#!/bin/bash
# Auto-start wrapper used by the launchd job (com.niveshcouncil.autostart.plist).
# Skips launching if the app is already running (e.g. you started it yourself
# manually) so launchd never causes a "port 5000 already in use" conflict.
cd "/Users/vishaltala/stock-agent-dashboard" || exit 1

if lsof -i :5000 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "$(date): already running on port 5000, skipping auto-start" >> launchd.log
    exit 0
fi

echo "$(date): auto-starting app.py" >> launchd.log
exec ./venv/bin/python3 app.py
