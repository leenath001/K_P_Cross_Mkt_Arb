#!/bin/bash
# Start.command — double-click to open the dashboard (run Setup.command once first).
# Keep this Terminal window open while trading; closing it (or Ctrl+C) stops the app.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ] || [ ! -f .env ]; then
  osascript -e 'display dialog "Run Setup.command first — it installs everything and asks for your API keys." buttons {"OK"} default button 1' >/dev/null 2>&1
  exit 1
fi
echo "Starting the K/P dashboard — it opens in your browser. Keep this window open; close it to stop the app."
exec .venv/bin/python -m streamlit run trade/web_app.py
