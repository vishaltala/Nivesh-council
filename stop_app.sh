#!/bin/bash
# Auto-stop wrapper used by the launchd job (com.niveshcouncil.autostop.plist).
# Safe to run even if nothing is listening on port 5000 — just logs and exits.
cd "/Users/vishaltala/stock-agent-dashboard" || exit 1

PID=$(lsof -ti :5000)
if [ -z "$PID" ]; then
    echo "$(date): nothing running on port 5000, nothing to stop" >> launchd.log
    exit 0
fi

echo "$(date): stopping app.py (pid $PID)" >> launchd.log
kill "$PID"
