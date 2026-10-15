import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
)

app = Flask(__name__)

# LINE設定
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get(
    "LINE_CHANNEL_ACCESS_TOKEN", ""
)

handler = WebhookHandler(CHANNEL_SECRET)

configuration = Configuration(
    access_token=CHANNEL_ACCESS_TOKEN
)

# 日本時間
JST = ZoneInfo("Asia/Tokyo")

# データベース
DB_PATH = os.environ.get("DB_PATH", "coins.db")


def get_connection():
    return sqlite3.connect(DB_PATH)


def initialize_database():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                report_date TEXT NOT NULL,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                coins INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (report_date, group_id, user_id)
            )
        """)


def parse_coins(text):
    """
    例：
    今日稼いだコイン：100枚
    今日稼いだコイン:100枚
    """
    pattern = r"^\s*今日稼いだコイン[：:]\s*(\d+)\s*枚\s*$"
    match = re.match(pattern, text)

    if not match:
        return None

    return int(match.group(1))


def save_report(report_date, group_id, user_id, coins):
    now = datetime.now(JST).isoformat()

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO reports (
                report_date,
                user_id,
                group_id,
                coins,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(report_date, group_id, user_id)
            DO UPDATE SET
                coins = excluded.coins,
                updated_at = excluded.updated_at
        """, (
            report_date,
            user_id,
            group_id,
            coins,
            now,
        ))


@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK", 200


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text
    source = event.source

    # グループ以外での報告は処理しない
    group_id = getattr(source, "group_id", None)
    user_id = getattr(source, "user_id", None)

    if not group_id or not user_id:
        return

    coins = parse_coins(text)

    # 指定された形式以外は無視
    if coins is None:
        return

    now = datetime.now(JST)
    report_date = now.date().isoformat()

    save_report(
        report_date,
        group_id,
        user_id,
        coins,
    )

    # 報告を受け付けたことを返信
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)

        api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text=f"{coins}枚で報告を受け付けました！"
                    )
                ],
            )
        )


initialize_database()
