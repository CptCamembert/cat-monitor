#!/bin/bash

# Stop Cat Monitor Application
# This script kills the tmux session running MediaMTX and the Flask application

SESSION_NAME="cat-monitor"

# Kill the tmux session
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

# Also try to kill any stray processes
pkill -f "python.*app.py" 2>/dev/null || true
pkill -f "./mediamtx" 2>/dev/null || true

exit 0
