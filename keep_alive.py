"""
Minimal Flask server for uptime monitoring (Koyeb, Render, UptimeRobot).
Runs in a background thread and does not interfere with the Telegram bot.
"""
import threading
from flask import Flask
import logging

logging.getLogger("werkzeug").setLevel(logging.ERROR)

app = Flask(__name__)

_server_started = False  # ✅ REQUIRED


@app.route("/")
def index():
    return "✅ K-Drama Bot is running", 200


@app.route("/health")
def health():
    return {"status": "ok", "service": "kdrama-bot"}, 200


def start_server(port=10000):
    global _server_started

    if _server_started:
        return

    _server_started = True

    def run():
        try:
            app.run(
                host="0.0.0.0",
                port=port,
                debug=False,
                use_reloader=False,
                threaded=False
            )
        except Exception as e:
            logging.warning(f"Keep-alive server error: {e}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
