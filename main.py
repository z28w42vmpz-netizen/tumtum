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
# 環境変数
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

    if DATABASE_URL.startswith("postgres://"):
        return DATABASE_URL.replace("postgres://", "postgresql://", 1)

    return DATABASE_URL


def db_connect():
    return psycopg.connect(get_db_url())


def initialize_database():
    """必要なテーブルを作成する。"""

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
            digits = match.group(1).translate(
                str.maketrans("０１２３４５６７８９", "0123456789")
            )

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
                INSERT INTO reports
                    (report_date, group_id, user_id, coins, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (report_date, group_id, user_id)
                DO UPDATE SET
                    coins = EXCLUDED.coins,
                    updated_at = NOW()
            """, (report_date, group_id, user_id, coins))


def reset_user_cumulative(group_id, user_id, today):
    """本人の過去の報告を削除して累計をリセットする。当日分の報告は残す。"""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM reports
                WHERE group_id = %s
                  AND user_id = %s
                  AND report_date < %s
            """, (group_id, user_id, today))
            return cur.rowcount


def reset_today_report(group_id, user_id, report_date):
    """本人の指定日の報告1件だけを削除する。通常は今日の誤報告を取り消す。"""
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM reports
                WHERE group_id = %s
                  AND user_id = %s
                  AND report_date = %s
            """, (group_id, user_id, report_date))
            return cur.rowcount


def get_members(group_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id
                FROM members
                WHERE group_id = %s
                ORDER BY first_seen, user_id
            """, (group_id,))

            return [row[0] for row in cur.fetchall()]


def get_reported_user_ids(group_id, report_date):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id
                FROM reports
                WHERE group_id = %s AND report_date = %s
            """, (group_id, report_date))

            return {row[0] for row in cur.fetchall()}


def send_push(messages, to_group=GROUP_ID):
    line_api.push_message(
        PushMessageRequest(
            to=to_group,
            messages=messages
        )
    )


def reply_text(reply_token, text):
    line_api.reply_message(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)]
        )
    )


def get_display_name(group_id, user_id):
    """LINEのグループプロフィールから表示名を取得する。取得できない場合は代替名を返す。"""
    try:
        profile = line_api.get_group_member_profile(group_id, user_id)
        name = getattr(profile, "display_name", None)
        if name:
            return name
    except Exception as exc:
        print(f"LINE表示名取得失敗（ユーザーID末尾 {user_id[-6:]}）: {exc}")

    return "名前を取得できませんでした"


def make_chart_url(user_id, end_date):
    """最初の報告日から当日までの累計コイン数をグラフ化する。"""

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT report_date, coins
                FROM reports
                WHERE group_id = %s
                  AND user_id = %s
                  AND report_date <= %s
                ORDER BY report_date
            """, (GROUP_ID, user_id, end_date))
            rows = cur.fetchall()

    values_by_date = {row[0]: row[1] for row in rows}
    display_name = get_display_name(GROUP_ID, user_id)

    if values_by_date:
        start_date = min(values_by_date)
    else:
        start_date = end_date

    labels = []
    values = []
    day = start_date
    cumulative_total = 0

    while day <= end_date:
        # その日の報告があれば累計に加算。未報告日は加算せず累計を維持。
        cumulative_total += values_by_date.get(day, 0)
        labels.append(day.strftime("%m/%d"))
        values.append(cumulative_total)
        day += timedelta(days=1)

    config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "累計コイン数",
                "data": values,
                "fill": False,
                "lineTension": 0.2,
                "pointRadius": 3
            }]
        },
        "options": {
            "title": {
                "display": True,
                "text": f"{display_name} の累計コイン推移"
            },
            "legend": {"display": True},
            "scales": {
                "yAxes": [{
                    "ticks": {"beginAtZero": True},
                    "scaleLabel": {
                        "display": True,
                        "labelString": "累計コイン数（枚）"
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


def maybe_send_all_graphs(group_id, report_date):
    """全員の報告が揃っていれば、呼び出されるたびに全員のグラフを送る。"""

    members = get_members(group_id)
    if not members:
        return False

    reported = get_reported_user_ids(group_id, report_date)
    if not set(members).issubset(reported):
        return False

    # graph_sendsによる重複制限は使わず、同じ日でも何度でも送信する。
    for user_id in members:
        try:
            display_name = get_display_name(group_id, user_id)
            chart_url = make_chart_url(user_id, report_date)

            line_api.push_message(
                PushMessageRequest(
                    to=group_id,
                    messages=[
                        TextMessage(
                            text=f"📈 {display_name} の累計コイン推移"
                        ),
                        ImageMessage(
                            original_content_url=chart_url,
                            preview_image_url=chart_url
                        )
                    ]
                )
            )
        except Exception as exc:
            print(
                f"グラフ送信失敗（ユーザーID末尾 {user_id[-6:]}）: {exc}"
            )

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

    # グループ以外のメッセージは処理しない
    if not group_id or not user_id:
        return

    if GROUP_ID and group_id != GROUP_ID:
        return

    register_member(group_id, user_id)

    text = event.message.text.strip()

    # 今日の報告1件だけをリセット（誤って大きな数を送った場合など）
    if text in ("報告リセット", "今日の報告リセット", "今日のコインリセット"):
        deleted_count = reset_today_report(group_id, user_id, today_jst())
        if deleted_count:
            reply_text(
                event.reply_token,
                "🗑️ 今日のコイン報告を取り消したよ！\n"
                "正しい枚数をもう一度送信してね。"
            )
        else:
            reply_text(
                event.reply_token,
                "今日の報告データが見つからなかったよ。\n"
                "先にコインを報告しているか確認してね。"
            )
        return

    # 自分の累計リセットコマンド（過去分のみ削除し、今日の報告は残す）
    if text in ("累計リセット", "コイン累計リセット"):
        deleted_count = reset_user_cumulative(group_id, user_id, today_jst())
        reply_text(
            event.reply_token,
            "🔄 累計コインをリセットしたよ！\n"
            f"過去の報告を{deleted_count}日分削除しました。\n"
            "今日の報告は残してあるので、今日のコインは新しい累計のスタートになります。"
        )
        return

    coins = parse_coins(text)

    if coins is None:
        return

    report_date = today_jst()

    save_report(
        group_id,
        user_id,
        report_date,
        coins
    )

    members = get_members(group_id)
    reported = get_reported_user_ids(group_id, report_date)

    status = (
        "コイン報告を保存したよ！\n"
        f"今日の報告状況：{len(set(members) & reported)}/{len(members)}人"
    )

    if members and set(members).issubset(reported):
        status += "\n全員の報告が揃ったので、個人別グラフを送信するね！"

    reply_text(event.reply_token, status)

    if members and set(members).issubset(reported):
        maybe_send_all_graphs(group_id, report_date)


@app.post("/remind")
def remind():
    require_cron_secret()

    send_push([
        TextMessage(
            text=(
                "⏰ 今日のコイン報告をしろ！\n\n"
                "「今日稼いだコイン：100枚」の形式で送信しなさい。"
            )
        )
    ])

    return jsonify({
        "status": "sent",
        "type": "reminder"
    })


@app.post("/missing")
def missing():
    require_cron_secret()

    yesterday = today_jst() - timedelta(days=1)

    members = get_members(GROUP_ID)
    reported = get_reported_user_ids(GROUP_ID, yesterday)

    missing_users = [
        uid for uid in members
        if uid not in reported
    ]

    if not missing_users:
        send_push([
            TextMessage(
                text=(
                    f"✅ {yesterday.strftime('%Y/%m/%d')} の"
                    "コイン報告は全員完了しています！"
                )
            )
        ])

        return jsonify({
            "status": "all_reported",
            "date": str(yesterday)
        })

    # 「今すぐしろ」を必ず別の行に表示する
    lines = [
        f"⚠️ {yesterday.strftime('%Y/%m/%d')} のコイン未報告者",
        "今すぐしろ",
        ""
    ]

    for uid in missing_users:
        display_name = get_display_name(GROUP_ID, uid)
        lines.append(f"・{display_name}")

    # メッセージ上限を超えないように分割する
    message_lines = []
    current = ""

    for line in lines:
        candidate = f"{current}\n{line}" if current else line

        if len(candidate) > 4500 and current:
            message_lines.append(current)
            current = line
        else:
            current = candidate

    if current:
        message_lines.append(current)

    for message_text in message_lines:
        send_push(
            [TextMessage(text=message_text)],
            to_group=GROUP_ID
        )

    return jsonify({
        "status": "sent",
        "date": str(yesterday),
        "missing_count": len(missing_users)
    })


# サーバー起動時にデータベースを初期化する
try:
    initialize_database()
    print("PostgreSQLテーブル初期化完了")

except Exception as exc:
    print(f"PostgreSQL初期化エラー: {exc}")
