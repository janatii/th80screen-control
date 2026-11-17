#!/usr/bin/env sh
# Start TH80 Screen Studio (Linux / macOS). Opens http://127.0.0.1:8780 in the browser.
cd "$(dirname "$0")" || exit 1
exec python3 app.py "$@"
