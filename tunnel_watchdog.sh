#!/bin/bash
# Keeps the Flask app and a public Cloudflare quick-tunnel alive.
# Restarts either process if it dies, and re-checks/logs the public URL.
#
# Usage:
#   nohup ./tunnel_watchdog.sh > /tmp/watchdog.log 2>&1 &
#
# Current public URL is always written to /tmp/tunnel_url.txt

cd "$(dirname "$0")"

CLOUDFLARED="$HOME/bin/cloudflared"
FLASK_LOG="/tmp/flask_out.log"
TUNNEL_LOG="/tmp/cloudflared.log"
URL_FILE="/tmp/tunnel_url.txt"

start_flask() {
  echo "[$(date)] starting flask"
  nohup python3 app.py > "$FLASK_LOG" 2>&1 &
  FLASK_PID=$!
}

start_tunnel() {
  echo "[$(date)] starting cloudflared tunnel"
  : > "$TUNNEL_LOG"
  nohup "$CLOUDFLARED" tunnel --url http://localhost:5001 --no-autoupdate > "$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!
}

start_flask
start_tunnel

while true; do
  sleep 5

  if ! kill -0 "$FLASK_PID" 2>/dev/null; then
    echo "[$(date)] flask died, restarting"
    start_flask
  fi

  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "[$(date)] tunnel died, restarting"
    start_tunnel
  fi

  # Extract/update the current public URL from the tunnel log
  url=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$TUNNEL_LOG" | tail -1)
  if [ -n "$url" ] && [ "$url" != "$(cat "$URL_FILE" 2>/dev/null)" ]; then
    echo "$url" > "$URL_FILE"
    echo "[$(date)] tunnel URL: $url"
  fi
done
