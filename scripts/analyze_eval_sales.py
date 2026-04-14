#!/usr/bin/env python3
"""
エバポセールの分析スクリプト
- キャンパス全体のcorrection_point_historicsから「Refund during sales」を集計
- セール発動日、対象人数、返還ポイント総数を分析
- プール→解放のサイクルを可視化
"""
import os, sys, json, time, requests
from datetime import datetime, timedelta
from collections import defaultdict

INTRA_API_BASE = "https://api.intra.42.fr"
CAMPUS_ID = 26  # 42 Tokyo

def get_token():
    resp = requests.post(f"{INTRA_API_BASE}/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": os.environ["CLIENT_ID"],
        "client_secret": os.environ["CLIENT_SECRET"],
    })
    resp.raise_for_status()
    return resp.json()["access_token"]

token = get_token()
headers = {"Authorization": f"Bearer {token}"}

# 1. キャンパスの全ユーザーを取得（cursus_id=21 = 42cursus 本科生）
print("[1] Fetching 42cursus users at campus 26...")
all_users = []
page = 1
while True:
    resp = requests.get(f"{INTRA_API_BASE}/v2/cursus/21/cursus_users", headers=headers, params={
        "filter[campus_id]": CAMPUS_ID,
        "page[size]": 100,
        "page[number]": page,
        "sort": "user_id",
    })
    if resp.status_code == 429:
        print(f"  429 rate limit, waiting 15s...")
        time.sleep(15)
        continue
    resp.raise_for_status()
    data = resp.json()
    if not data:
        break
    for item in data:
        u = item.get("user", {})
        if u.get("login"):
            all_users.append({
                "id": u["id"],
                "login": u["login"],
                "correction_point": u.get("correction_point", 0),
            })
    page += 1
    if len(data) < 100:
        break
    time.sleep(0.5)

print(f"  Total users: {len(all_users)}")

# 2. サンプルユーザー（最大50人）のcorrection_point_historicsを取得
# 全員取ると時間がかかりすぎるので、サンプリング
sample_size = min(50, len(all_users))
# ポイントが多い人から順にサンプル（セール対象になりやすい）
all_users.sort(key=lambda u: u["correction_point"], reverse=True)
sample = all_users[:sample_size]

print(f"\n[2] Fetching correction_point_historics for {sample_size} users...")
all_sales = []  # (date, user_login, sum, total)
all_reasons = defaultdict(int)
user_histories = {}

for i, u in enumerate(sample):
    uid = u["id"]
    login = u["login"]
    print(f"  [{i+1}/{sample_size}] {login} (cp={u['correction_point']})...", end="", flush=True)

    history = []
    page = 1
    while True:
        resp = requests.get(f"{INTRA_API_BASE}/v2/users/{uid}/correction_point_historics", headers=headers, params={
            "page[size]": 100,
            "page[number]": page,
        })
        if resp.status_code == 429:
            time.sleep(15)
            continue
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        history.extend(data)
        page += 1
        if len(data) < 100:
            break
        time.sleep(0.3)

    user_histories[login] = history
    sales_count = 0
    for h in history:
        reason = h.get("reason", "unknown")
        all_reasons[reason] += 1
        if "sales" in reason.lower() or "refund during" in reason.lower():
            all_sales.append({
                "date": h.get("created_at", "")[:10],
                "datetime": h.get("created_at", "")[:19],
                "login": login,
                "sum": h.get("sum", 0),
                "total": h.get("total", 0),
            })
            sales_count += 1

    print(f" {len(history)} entries, {sales_count} sales")
    time.sleep(0.3)

# 3. 分析結果
print(f"\n{'='*60}")
print(f"=== エバポセール分析結果 ===")
print(f"{'='*60}")

print(f"\n[A] 全reason集計（サンプル{sample_size}人）")
for reason, count in sorted(all_reasons.items(), key=lambda x: x[1], reverse=True):
    print(f"  {reason}: {count}回")

print(f"\n[B] セール発動日の特定（Refund during sales）")
print(f"  総セールイベント: {len(all_sales)}件")

# 日付ごとに集計
sales_by_date = defaultdict(lambda: {"count": 0, "total_points": 0, "users": set()})
for s in all_sales:
    d = s["date"]
    sales_by_date[d]["count"] += 1
    sales_by_date[d]["total_points"] += s["sum"]
    sales_by_date[d]["users"].add(s["login"])

print(f"\n  セール発動日一覧（日付 / 対象人数 / 返還ポイント合計）:")
for date in sorted(sales_by_date.keys()):
    info = sales_by_date[date]
    print(f"    {date}: {len(info['users'])}人, +{info['total_points']}pt ({info['count']}件)")

# 4. セール間の間隔を計算
dates = sorted(sales_by_date.keys())
if len(dates) >= 2:
    print(f"\n[C] セール間隔の分析")
    intervals = []
    for i in range(1, len(dates)):
        d1 = datetime.strptime(dates[i-1], "%Y-%m-%d")
        d2 = datetime.strptime(dates[i], "%Y-%m-%d")
        interval = (d2 - d1).days
        intervals.append(interval)
        print(f"    {dates[i-1]} → {dates[i]}: {interval}日")
    if intervals:
        avg_interval = sum(intervals) / len(intervals)
        print(f"  平均間隔: {avg_interval:.0f}日")
        last_sale = datetime.strptime(dates[-1], "%Y-%m-%d")
        predicted_next = last_sale + timedelta(days=int(avg_interval))
        print(f"  次回セール予測: {predicted_next.strftime('%Y-%m-%d')} (平均間隔ベース)")

# 5. ポイントプール推移の推定
print(f"\n[D] correction_point の現在分布（サンプル{sample_size}人）")
points = [u["correction_point"] for u in sample]
print(f"  平均: {sum(points)/len(points):.1f}pt")
print(f"  中央値: {sorted(points)[len(points)//2]}pt")
print(f"  最大: {max(points)}pt")
print(f"  最小: {min(points)}pt")
print(f"  5pt以上: {sum(1 for p in points if p >= 5)}人")
print(f"  10pt以上: {sum(1 for p in points if p >= 10)}人")
print(f"  20pt以上: {sum(1 for p in points if p >= 20)}人")

# 6. 全ユーザーのポイント分布（API取得済み分）
print(f"\n[E] 全ユーザー ({len(all_users)}人) のcorrection_point分布")
all_points = [u["correction_point"] for u in all_users]
print(f"  合計プールポイント: {sum(all_points)}pt")
print(f"  平均: {sum(all_points)/len(all_points):.1f}pt")
print(f"  0pt: {sum(1 for p in all_points if p == 0)}人")
print(f"  1-4pt: {sum(1 for p in all_points if 1 <= p <= 4)}人")
print(f"  5-9pt: {sum(1 for p in all_points if 5 <= p <= 9)}人")
print(f"  10-19pt: {sum(1 for p in all_points if 10 <= p <= 19)}人")
print(f"  20pt以上: {sum(1 for p in all_points if p >= 20)}人")
