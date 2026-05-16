"""
keep_alive.py — Keeps the Render free instance awake
via a simple Flask web server.
"""

from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
