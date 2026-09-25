#!/bin/sh
# Starts the brainrot bot and restarts it automatically if it ever crashes (macOS / Linux).
cd "$(dirname "$0")" || exit 1

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

while true; do
    "$PY" brainrot.py "$@"
    code=$?
    # 0 = finished, 2 = setup problem, 3 = already running, 130 = you stopped it
    case $code in
        0|2|3|130) exit $code ;;
    esac
    echo "The bot stopped unexpectedly (exit code $code). Restarting in 10 seconds... (Ctrl+C to quit)"
    sleep 10
done
