#!/usr/bin/env python3
"""レビュー報酬の倍率パターンを分析するスクリプト
- Earning after defense の sum が +1 以外のケースを探す
- どの課題で倍ポイントがもらえるかを特定
"""
import os, sys, json, time, requests
from collections import defaultdict

INTRA_API_BASE = "https://api.intra.42.fr"
CAMPUS_ID = 26

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

# サンプルユーザー（ポイント多い上位20人）
print("[1] Fetching users...")
all_users = []
page = 1
while True:
    resp = requests.get(f"{INTRA_API_BASE}/v2/cursus/21/cursus_users", headers=headers, params={
        "filter[campus_id]": CAMPUS_ID, "page[size]": 100, "page[number]": page, "sort": "user_id",
    })
    if resp.status_code == 429:
        time.sleep(15); continue
    resp.raise_for_status()
    data = resp.json()
    if not data: break
    for item in data:
        u = item.get("user", {})
        if u.get("login"):
            all_users.append({"id": u["id"], "login": u["login"], "cp": u.get("correction_point", 0)})
    page += 1
    if len(data) < 100: break
    time.sleep(0.5)

all_users.sort(key=lambda u: u["cp"], reverse=True)
sample = all_users[:20]
print(f"  Total: {len(all_users)}, sampling top {len(sample)}")

# 各ユーザーの履歴を取得し、+1以外のEarningを探す
print("\n[2] Analyzing earning patterns...")
earning_sums = defaultdict(int)  # sum値 → 出現回数
big_earnings = []  # +2以上のearning一覧
defense_sums = defaultdict(int)  # Defense plannificationのsum値分布
sales_details = []  # Refund during salesの詳細

for i, u in enumerate(sample):
    uid, login = u["id"], u["login"]
    print(f"  [{i+1}/{len(sample)}] {login}...", end="", flush=True)

    history = []
    pg = 1
    while True:
        resp = requests.get(f"{INTRA_API_BASE}/v2/users/{uid}/correction_point_historics", headers=headers, params={
            "page[size]": 100, "page[number]": pg,
        })
        if resp.status_code == 429: time.sleep(15); continue
        if resp.status_code == 404: break
        resp.raise_for_status()
        data = resp.json()
        if not data: break
        history.extend(data)
        pg += 1
        if len(data) < 100: break
        time.sleep(0.3)

    for h in history:
        reason = h.get("reason", "")
        s = h.get("sum", 0)

        if "Earning after defense" in reason:
            earning_sums[s] += 1
            if s != 1:
                big_earnings.append({
                    "login": login,
                    "sum": s,
                    "date": h.get("created_at", "")[:19],
                    "total": h.get("total"),
                    "reason": reason,
                })

        if "Defense plannification" in reason:
            defense_sums[s] += 1

        if "sales" in reason.lower() or "refund during" in reason.lower():
            sales_details.append({
                "login": login,
                "sum": s,
                "date": h.get("created_at", "")[:19],
                "total": h.get("total"),
                "reason": reason,
            })

    print(f" {len(history)} entries")
    time.sleep(0.3)

# 結果出力
print(f"\n{'='*60}")
print(f"=== レビュー報酬の分析 ===")
print(f"{'='*60}")

print(f"\n[A] Earning after defense の sum値 分布")
for s, count in sorted(earning_sums.items()):
    print(f"  +{s}pt: {count}回")

print(f"\n[B] +1以外のEarning（倍ポイント？）: {len(big_earnings)}件")
for e in sorted(big_earnings, key=lambda x: x["sum"], reverse=True)[:30]:
    print(f"  {e['date']}  {e['login']:12s}  +{e['sum']}pt  total={e['total']}")

print(f"\n[C] Defense plannification の sum値 分布")
for s, count in sorted(defense_sums.items()):
    print(f"  {s:+d}pt: {count}回")

print(f"\n[D] Refund during sales の sum値 分布")
sale_sums = defaultdict(int)
for s in sales_details:
    sale_sums[s["sum"]] += 1
for s, count in sorted(sale_sums.items()):
    print(f"  +{s}pt: {count}回")

print(f"\n[E] Refund during sales の詳細（直近20件）")
sales_details.sort(key=lambda x: x["date"], reverse=True)
for s in sales_details[:20]:
    print(f"  {s['date']}  {s['login']:12s}  +{s['sum']}pt  total={s['total']}")
