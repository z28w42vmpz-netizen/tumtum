import os
import re
import json
import hmac
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import psycopg
from flask import Flask, request, abort, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
    ImageMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# ----------------------------
# Environment variables
# ----------------------------
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_ID = os.environ.get("LINE_GROUP_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

JST = ZoneInfo("Asia/Tokyo")
app = Flask(__name__)

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("WARNING: LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

handler = WebhookHandler(CHANNEL_SECRET) if CHANNEL_SECRET else None
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_api = MessagingApi(api_client)


def now_jst():
    return datetime.now(JST)


def today_jst():
    return now_jst().date()


def get_db_url():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL が設定されていません。")
    # Renderの接続URLがpostgres://で始まる場合にも対応
    if DATABASE_URL.startswith("postgres://"):
        return DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return DATABASE_URL


def db_connect():
    return psycopg.connect(get_db_url())


def initialize_database():
    """必要なテーブルを作成する。過去のSQLiteデータは移行しない。"""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (group_id, user_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    report_date DATE NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    coins INTEGER NOT NULL CHECK (coins >= 0),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (report_date, group_id, user_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS graph_sends (
                    report_date DATE NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (report_date, group_id, user_id)
                )
            """)


def register_member(group_id, user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO members (group_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT (group_id, user_id) DO NOTHING
            """, (group_id, user_id))


def parse_coins(text):
    """
    例:
      今日稼いだコイン：100枚
      今日稼いだコイン: 100
      コイン 100枚
    """
    patterns = [
        r"今日稼いだコイン\s*[:：]\s*([0-9０-９]+)\s*枚?",
        r"コイン\s*[:：]?\s*([0-9０-９]+)\s*枚",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            digits = match.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            try:
                coins = int(digits)
                if 0 <= coins <= 2_000_000_000:
                    return coins
            except ValueError:
                pass
    return None


def save_report(group_id, user_id, report_date, coins):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reports (report_date, group_id, user_id, coins, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (report_date, group_id, user_id)
                DO UPDATE SET coins = EXCLUDED.coins, updated_at = NOW()
            """, (report_date, group_id, user_id, coins))


def get_members(group_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id FROM members
                WHERE group_id = %s
                ORDER BY first_seen, user_id
            """, (group_id,))
            return [row[0] for row in cur.fetchall()]


def get_reported_user_ids(group_id, report_date):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id FROM reports
                WHERE group_id = %s AND report_date = %s
            """, (group_id, report_date))
            return {row[0] for row in cur.fetchall()}


def send_push(messages, to_group=GROUP_ID):
    line_api.push_message(
        PushMessageRequest(to=to_group, messages=messages)
    )


def reply_text(reply_token, text):
    line_api.reply_message(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)]
        )
    )


def make_chart_url(user_id, end_date):
    """対象ユーザーの直近30日間の推移をQuickChartの画像URLにする。"""
    start_date = end_date - timedelta(days=29)
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT report_date, coins
                FROM reports
                WHERE group_id = %s AND user_id = %s
                  AND report_date BETWEEN %s AND %s
                ORDER BY report_date
            """, (GROUP_ID, user_id, start_date, end_date))
            rows = cur.fetchall()

    values_by_date = {row[0]: row[1] for row in rows}
    labels = []
    values = []
    for offset in range(30):
        day = start_date + timedelta(days=offset)
        labels.append(day.strftime("%m/%d"))
        # 未報告の日は0として表示する
        values.append(values_by_date.get(day, 0))

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "獲得コイン数",
                "data": values,
                "fill": False,
                "lineTension": 0.2,
                "pointRadius": 3
            }]
        },
        "options": {
            "title": {
                "display": True,
                "text": f"コイン推移（直近30日）・ユーザー末尾 {user_id[-6:]}"
            },
            "legend": {"display": True},
            "scales": {
                "yAxes": [{
                    "ticks": {"beginAtZero": True},
                    "scaleLabel": {
                        "display": True,
                        "labelString": "コイン数（枚）"
                    }
                }],
                "xAxes": [{
                    "scaleLabel": {
                        "display": True,
                        "labelString": "日付"
                    }
                }]
            }
        }
    }
    query = urlencode({
        "width": 1000,
        "height": 500,
        "format": "png",
        "backgroundColor": "white",
        "c": json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    })
    return f"https://quickchart.io/chart?{query}"


def reserve_graph_send(group_id, report_date, user_id):
    """DBの一意制約で同日の重複グラフ送信を抑止する。"""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO graph_sends (report_date, group_id, user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (report_date, group_id, user_id) DO NOTHING
                RETURNING user_id
            """, (report_date, group_id, user_id))
            return cur.fetchone() is not None


def remove_graph_send_reservation(group_id, report_date, user_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM graph_sends
                WHERE report_date = %s AND group_id = %s AND user_id = %s
            """, (report_date, group_id, user_id))


def maybe_send_all_graphs(group_id, report_date):
    members = get_members(group_id)
    if not members:
        return False

    reported = get_reported_user_ids(group_id, report_date)
    if not set(members).issubset(reported):
        return False

    # 全員の当日分が揃った。未送信の各メンバーのグラフを送信する。
    for user_id in members:
        if not reserve_graph_send(group_id, report_date, user_id):
            continue
        try:
            chart_url = make_chart_url(user_id, report_date)
            # Mentionモデルに依存せず、ユーザーID末尾でグラフを識別する
            line_api.push_message(PushMessageRequest(
                to=group_id,
                messages=[
                    TextMessage(text=f"📈 ユーザーID末尾 {user_id[-6:]} の直近30日間のコイン推移"),
                    ImageMessage(original_content_url=chart_url, preview_image_url=chart_url)
                ]
            ))
        except Exception:
            # 送信失敗時は予約を消し、次の報告時などに再試行できるようにする
            remove_graph_send_reservation(group_id, report_date, user_id)
            raise
    return True


def cron_authorized():
    expected = CRON_SECRET
    supplied = request.headers.get("X-Cron-Secret", "")
    return bool(expected) and hmac.compare_digest(expected, supplied)


def require_cron_secret():
    if not cron_authorized():
        abort(401, description="Unauthorized")


@app.get("/")
def health():
    return jsonify({"status": "ok"})


@app.post("/webhook")
def webhook():
    if handler is None:
        abort(500, description="LINE_CHANNEL_SECRET is not configured")
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, description="Invalid signature")
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent) if handler else (lambda f: f)
def handle_message(event):
    source = event.source
    group_id = getattr(source, "group_id", None)
    user_id = getattr(source, "user_id", None)

    # 指定グループ以外は処理しない。グループ外の個人チャットも対象外。
    if not group_id or not user_id:
        return
    if GROUP_ID and group_id != GROUP_ID:
        return

    register_member(group_id, user_id)
    text = event.message.text.strip()
    coins = parse_coins(text)

    if coins is None:
        return

    report_date = today_jst()
    save_report(group_id, user_id, report_date, coins)

    members = get_members(group_id)
    reported = get_reported_user_ids(group_id, report_date)
    status = f"コイン報告を保存したよ！\n今日の報告状況：{len(set(members) & reported)}/{len(members)}人"
    if len(members) > 0 and set(members).issubset(reported):
        status += "\n全員の報告が揃ったので、個人別グラフを送信するね！"

    reply_text(event.reply_token, status)

    if members and set(members).issubset(reported):
        maybe_send_all_graphs(group_id, report_date)


@app.post("/remind")
def remind():
    require_cron_secret()
    send_push([TextMessage(
        text="⏰ 今日のコイン報告をしろ！\n\n「今日稼いだコイン：100枚」の形式で送信しなさい。"
    )])
    return jsonify({"status": "sent", "type": "reminder"})


@app.post("/missing")
def missing():
    require_cron_secret()
    yesterday = today_jst() - timedelta(days=1)
    members = get_members(GROUP_ID)
    reported = get_reported_user_ids(GROUP_ID, yesterday)

    missing_users = [uid for uid in members if uid not in reported]
    if not missing_users:
        send_push([TextMessage(text=f"✅ {yesterday.strftime('%Y/%m/%d')} のコイン報告は全員完了しています！")])
        return jsonify({"status": "all_reported", "date": str(yesterday)})

    prefix = f"⚠️ {yesterday.strftime('%Y/%m/%d')} のコイン未報告者。今すぐしろ\n"
    # Mentionモデルに依存せず、未報告者のユーザーID末尾を通知する
    lines = [prefix.rstrip()]
    lines.extend(f"・ユーザーID末尾 {uid[-6:]}" for uid in missing_users)

    # LINEのテキストメッセージ上限を超えないよう分割して送信
    message_lines = []
    current = ""
    for line in lines:
        candidate = f"{current}\\n{line}" if current else line
        if len(candidate) > 4500 and current:
            message_lines.append(current)
            current = line
        else:
            current = candidate
    if current:
        message_lines.append(current)

    for message_text in message_lines:
        send_push([TextMessage(text=message_text)], to_group=GROUP_ID)

    return jsonify({
        "status": "sent",
        "date": str(yesterday),
        "missing_count": len(missing_users)
    })


# Initialize schema when the web service starts.
try:
    initialize_database()
    print("PostgreSQLテーブル初期化完了")
except Exception as exc:
    # ログに原因を出し、Renderのログから接続エラーを確認できるようにする
    print(f"PostgreSQL初期化エラー: {exc}")
