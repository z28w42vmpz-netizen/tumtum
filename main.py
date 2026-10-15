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
    PushMessageRequest,
    TextMessage,
)

from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
)


# ========================================
# 基本設定
# ========================================

app = Flask(__name__)

# LINE Messaging APIの認証情報
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get(
    "LINE_CHANNEL_ACCESS_TOKEN", ""
)

# リマインド送信先のLINEグループID
LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID", "")

# 外部スケジュールサービスからの認証用秘密文字列
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# 日本時間
JST = ZoneInfo("Asia/Tokyo")

# SQLiteデータベースの保存先
DB_PATH = os.environ.get("DB_PATH", "coins.db")

# LINE Botの設定
handler = WebhookHandler(CHANNEL_SECRET)

configuration = Configuration(
    access_token=CHANNEL_ACCESS_TOKEN
)


# ========================================
# データベース
# ========================================

def get_connection():
    """SQLiteデータベースへの接続を取得する。"""
    return sqlite3.connect(DB_PATH)


def initialize_database():
    """必要なテーブルを作成する。"""

    with get_connection() as conn:

        # コイン報告テーブル
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                report_date TEXT NOT NULL,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                coins INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (
                    report_date,
                    group_id,
                    user_id
                )
            )
        """)

        # 発言したメンバーの登録テーブル
        conn.execute("""
            CREATE TABLE IF NOT EXISTS members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                PRIMARY KEY (
                    group_id,
                    user_id
                )
            )
        """)


def register_member(group_id, user_id):
    """グループで発言したメンバーを登録する。"""

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


def parse_coins(text):
    """
    コイン報告メッセージを解析する。

    例：
    今日稼いだコイン：100枚
    今日稼いだコイン:100枚

    数字は0以上の整数を受け付ける。
    """

    pattern = r"^\s*今日稼いだコイン[：:]\s*(\d+)\s*枚\s*$"

    match = re.match(pattern, text)

    if not match:
        return None

    return int(match.group(1))


def save_report(report_date, group_id, user_id, coins):
    """コイン報告を保存する。同じ日の報告は上書きする。"""

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

            ON CONFLICT (
                report_date,
                group_id,
                user_id
            )
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


def get_report_status(report_date, group_id):
    """登録メンバー数と報告済み人数を取得する。"""

    with get_connection() as conn:

        member_result = conn.execute("""
            SELECT COUNT(*)
            FROM members
            WHERE group_id = ?
        """, (
            group_id,
        )).fetchone()

        report_result = conn.execute("""
            SELECT COUNT(*)
            FROM reports
            WHERE report_date = ?
              AND group_id = ?
        """, (
            report_date,
            group_id,
        )).fetchone()

    total_members = member_result[0]
    reported_members = report_result[0]

    return reported_members, total_members


# ========================================
# Webページ
# ========================================

@app.route("/", methods=["GET"])
def home():
    """Renderの動作確認用ページ。"""

    return "LINE Bot is running!", 200


# ========================================
# LINE Webhook
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
# LINEメッセージ受信処理
# ========================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):

    text = event.message.text
    source = event.source

    # グループIDとユーザーIDを取得
    group_id = getattr(source, "group_id", None)
    user_id = getattr(source, "user_id", None)

    print("GROUP_ID:", group_id, flush=True)
    print("USER_ID:", user_id, flush=True)
    print("MESSAGE:", text, flush=True)

    # グループ以外のメッセージは処理しない
    if not group_id or not user_id:
        print(
            "グループまたはユーザーIDを取得できませんでした",
            flush=True,
        )
        return

    # 発言したメンバーを登録
    register_member(group_id, user_id)

    # コイン報告形式かどうかを確認
    coins = parse_coins(text)

    if coins is None:
        return

    # 日本時間で報告日を決定
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
    reported_count, total_count = get_report_status(
        report_date,
        group_id,
    )

    print(
        f"報告状況: {reported_count}/{total_count}人",
        flush=True,
    )

    # LINEに受付結果を返信
    with ApiClient(configuration) as api_client:

        api = MessagingApi(api_client)

        api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text=(
                            f"{coins}枚で報告を受け付けました！\n"
                            f"現在の報告状況："
                            f"{reported_count}/{total_count}人"
                        )
                    )
                ],
            )
        )


# ========================================
# 23時のリマインド送信
# ========================================

@app.route("/remind", methods=["POST"])
def send_reminder():
    """認証されたリクエストに対してリマインドを送信する。"""

    # 必須設定の確認
    if not LINE_GROUP_ID or not CRON_SECRET:
        print(
            "LINE_GROUP_IDまたはCRON_SECRETが未設定です",
            flush=True,
        )
        return "Environment variables are not set", 500

    # リクエストの認証
    secret = request.headers.get("X-Cron-Secret", "")

    if secret != CRON_SECRET:
        abort(403)

    # LINEグループへ通知
    try:
        with ApiClient(configuration) as api_client:

            api = MessagingApi(api_client)

            api.push_message(
                PushMessageRequest(
                    to=LINE_GROUP_ID,
                    messages=[
                        TextMessage(
                            text=(
                                "📢 コイン報告の時間です！\n\n"
                                "今日稼いだコイン数を報告してください。\n\n"
                                "報告形式：\n"
                                "今日稼いだコイン：100枚"
                            )
                        )
                    ],
                )
            )

        print(
            "コイン報告リマインドを送信しました",
            flush=True,
        )

        return "Reminder sent", 200

    except Exception as e:

        print(
            f"リマインド送信エラー: {e}",
            flush=True,
        )

        return "Failed to send reminder", 500


# ========================================
# アプリ起動時の初期化
# ========================================

initialize_database()
