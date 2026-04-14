#!/usr/bin/env python3
"""指定ユーザーのcorrection_point履歴を取得するワンショットスクリプト"""
import os, sys, json, requests
from datetime import datetime

INTRA_API_BASE = "https://api.intra.42.fr"

def get_token():
    resp = requests.post(f"{INTRA_API_BASE}/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": os.environ["CLIENT_ID"],
        "client_secret": os.environ["CLIENT_SECRET"],
    })
    resp.raise_for_status()
    return resp.json()["access_token"]

login = sys.argv[1] if len(sys.argv) > 1 else "hirosuzu"
token = get_token()
headers = {"Authorization": f"Bearer {token}"}

# ユーザーID取得
resp = requests.get(f"{INTRA_API_BASE}/v2/users/{login}", headers=headers)
resp.raise_for_status()
user = resp.json()
user_id = user["id"]
print(f"User: {login} (id={user_id})")
print(f"Current correction_point: {user.get('correction_point')}")
print(f"Current wallet: {user.get('wallet')}")
print()

# correction_point_historics 取得（全ページ）
all_history = []
page = 1
while True:
    resp = requests.get(
        f"{INTRA_API_BASE}/v2/users/{user_id}/correction_point_historics",
        headers=headers,
        params={"page[size]": 100, "page[number]": page, "sort": "-created_at"}
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        break
    all_history.extend(data)
    page += 1
    if len(data) < 100:
        break

print(f"Total history entries: {len(all_history)}")
print()

# reason ごとの集計
reasons = {}
for h in all_history:
    r = h.get("reason", "unknown")
    if r not in reasons:
        reasons[r] = {"count": 0, "total_sum": 0, "dates": []}
    reasons[r]["count"] += 1
    reasons[r]["total_sum"] += h.get("sum", 0)
    reasons[r]["dates"].append(h.get("created_at", "")[:10])

print("=== Reason別集計 ===")
for r, info in sorted(reasons.items(), key=lambda x: x[1]["count"], reverse=True):
    dates = sorted(set(info["dates"]))
    date_range = f"{dates[0]} ~ {dates[-1]}" if dates else ""
    print(f"  {r}: {info['count']}回, 合計{info['total_sum']:+d}pt ({date_range})")

print()

# 3月末付近のデータ（エバポセール調査）
print("=== 2025-03-25 〜 2025-04-05 の履歴 ===")
for h in all_history:
    dt = h.get("created_at", "")[:10]
    if "2025-03-25" <= dt <= "2025-04-05":
        print(f"  {h.get('created_at', '')[:19]}  {h.get('sum', 0):+3d}pt  reason={h.get('reason', '?')}  total={h.get('total', '?')}")

# 直近20件を表示
print()
print("=== 直近20件 ===")
for h in all_history[:20]:
    print(f"  {h.get('created_at', '')[:19]}  {h.get('sum', 0):+3d}pt  reason={h.get('reason', '?')}  total={h.get('total', '?')}")
