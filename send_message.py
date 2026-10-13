
import os
import json
import urllib.request
import urllib.error

token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
group_id = os.environ["LINE_GROUP_ID"]

message = "日付が変わったよ！今日もよろしく！"

url = "https://api.line.me/v2/bot/message/push"

payload = {
    "to": group_id,
    "messages": [
        {"type": "text", "text": message}
    ]
}

req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    },
    method="POST"
)

with urllib.request.urlopen(req, timeout=20) as response:
    print("送信成功:", response.status)
