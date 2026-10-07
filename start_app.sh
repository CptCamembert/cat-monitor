#!/bin/bash

# Start Cat Monitor Application (MediaMTX + Flask app) in a tmux session
# This script creates the tmux session named 'cat-monitor' running both
# MediaMTX (for streaming) and the Flask app, matching start_streams.sh

SESSION_NAME="cat-monitor"
APP_DIR="/home/pi5-max/cat_monitor"

# Kill existing session and any stray processes if they exist
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
pkill -f './mediamtx' 2>/dev/null || true
pkill -f 'python.*app.py' 2>/dev/null || true
pkill -f 'rpicam-vid' 2>/dev/null || true

# Create new tmux session in the background
tmux new-session -d -s "$SESSION_NAME" -c "$APP_DIR"

# Start MediaMTX and the Flask app together, same as start_streams.sh
# MediaMTX is backgrounded inside a subshell so the venv activation and PATH
# still apply to the foreground "python app.py" (bare "cmd &" would otherwise
# run everything after it in the original, non-activated shell).
tmux send-keys -t "$SESSION_NAME" "cd $APP_DIR && source .venv/bin/activate && python app.py" Enter

# Give it a moment to start
sleep 2

exit 0
