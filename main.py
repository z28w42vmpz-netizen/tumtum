
import os
from flask import Flask, request

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    for event in data.get("events", []):
        source = event.get("source", {})
        group_id = source.get("groupId")

        if group_id:
            print("GROUP_ID:", group_id)

    return "OK", 200
