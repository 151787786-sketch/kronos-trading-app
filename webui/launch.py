#!/usr/bin/env python3
"""Clean launcher for the Kronos Web UI.

Unlike run.py / app.py this does not enable the Flask debug reloader
(avoids spawning a second process) and only opens the browser once.
Run: python launch.py  (or double-click 启动Kronos.bat)
"""
import os
import sys
import time
import webbrowser

WEBUI_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WEBUI_DIR)
sys.path.insert(0, WEBUI_DIR)
sys.path.insert(0, PROJECT_ROOT)

# Prefer local model weights (D:\a\kronos\models) when present.
os.environ.setdefault("KRONOS_MODELS_DIR", os.path.join(PROJECT_ROOT, "models"))
# Use the HF mirror for any online downloads (models not cached locally).
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

if __name__ == "__main__":
    from app import app
    print("=" * 52)
    print("  Kronos Web UI - Financial K-line Foundation Model")
    print("=" * 52)
    print(f"  URL:  http://localhost:7070")
    print(f"  Press Ctrl+C in this window to stop the server.")
    print("=" * 52)
    # Give the user a moment to read the banner, then open the browser.
    webbrowser.open("http://localhost:7070")
    # Debug=False => no reloader child process; use_reloader=False is a belt & braces.
    app.run(host="127.0.0.1", port=7070, debug=False, use_reloader=False)
