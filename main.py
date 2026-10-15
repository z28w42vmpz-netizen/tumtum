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

# ========================================
# LINE設定
# ========================================

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get(
    "LINE_CHANNEL_ACCESS_TOKEN", ""
)

handler = WebhookHandler(CHANNEL_SECRET)

configuration = Configuration(
    access_token=CHANNEL_ACCESS_TOKEN
)

# ========================================
# 基本設定
# ========================================

JST = ZoneInfo("Asia/Tokyo")
DB_PATH = os.environ.get("DB_PATH", "coins.db")


# ========================================
# データベース
# ========================================

def get_connection():
    return sqlite3.connect(DB_PATH)


def initialize_database():
    with get_connection() as conn:

        # コイン報告
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

        # グループメンバー
        conn.execute("""
            CREATE TABLE IF NOT EXISTS members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id)
            )
        """)


# ========================================
# メンバー登録
# ========================================

def register_member(group_id, user_id):
    """グループで発言したメンバーを登録する"""

    now = datetime.now(JST).isoformat()

    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO members (
                group_id,
                user_id,
                first_seen
            )
            VALUES (?, ?, ?)
        """, (
            group_id,
            user_id,
            now,
        ))


# ========================================
# コイン報告の解析
# ========================================

def parse_coins(text):
    pattern = r"^\s*今日稼いだコイン[：:]\s*(\d+)\s*枚\s*$"

    match = re.match(pattern, text)

    if not match:
        return None

    return int(match.group(1))


# ========================================
# コイン報告の保存
# ========================================

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


# ========================================
# 報告状況の取得
# ========================================

def get_report_status(report_date, group_id):
    """登録メンバーの報告状況を取得する"""

    with get_connection() as conn:
        members = conn.execute("""
            SELECT user_id
            FROM members
            WHERE group_id = ?
        """, (group_id,)).fetchall()

        reports = conn.execute("""
            SELECT user_id, coins
            FROM reports
            WHERE report_date = ?
              AND group_id = ?
        """, (
            report_date,
            group_id,
        )).fetchall()

    report_map = {
        user_id: coins
        for user_id, coins in reports
    }

    status = []

    for (user_id,) in members:
        status.append({
            "user_id": user_id,
            "reported": user_id in report_map,
            "coins": report_map.get(user_id),
        })

    return status


# ========================================
# トップページ
# ========================================

@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running!", 200


# ========================================
# Webhook
# ========================================

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get(
        "X-Line-Signature", ""
    )

    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)

    except InvalidSignatureError:
        abort(400)

    return "OK", 200


# ========================================
# メッセージ受信
# ========================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):

    text = event.message.text
    source = event.source

    group_id = getattr(source, "group_id", None)
    user_id = getattr(source, "user_id", None)

    print("GROUP_ID:", group_id, flush=True)
    print("USER_ID:", user_id, flush=True)
    print("MESSAGE:", text, flush=True)

    # グループ以外は対象外
    if not group_id or not user_id:
        return

    # 発言したメンバーを登録
    register_member(group_id, user_id)

    print(
        f"メンバー登録確認: {user_id}",
        flush=True,
    )

    # コイン数を解析
    coins = parse_coins(text)

    # コイン報告以外のメッセージは登録だけ行う
    if coins is None:
        return

    now = datetime.now(JST)
    report_date = now.date().isoformat()

    # コイン報告を保存
    save_report(
        report_date,
        group_id,
        user_id,
        coins,
    )

    print(
        f"報告保存成功: date={report_date}, coins={coins}",
        flush=True,
    )

    # 現在の報告状況を取得
    status = get_report_status(
        report_date,
        group_id,
    )

    reported_count = sum(
        1 for member in status
        if member["reported"]
    )

    total_count = len(status)

    print(
        f"報告状況: {reported_count}/{total_count}人",
        flush=True,
    )

    # 受付メッセージ
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


# ========================================
# 起動時の初期化
# ========================================

initialize_database()
