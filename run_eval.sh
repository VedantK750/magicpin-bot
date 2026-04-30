#!/bin/bash
pkill -f uvicorn || true
./.venv/bin/python -m uvicorn bot:app --port 8080 --host 0.0.0.0 &
UVICORN_PID=$!
sleep 5
./.venv/bin/python judge_simulator.py
kill $UVICORN_PID
