#!/usr/bin/env python3
"""エバポセールの頻度分析と次回予測

分析内容:
1. Refund during sales または +4pt Earningの日付をセール日として特定
2. セール発動間隔の統計（平均・中央値・最大最小）
3. 曜日別の傾向
4. セールとセールの間に消費されたポイント量
5. 次回セール予測（複数モデル）
"""
import os, sys, time, requests
from datetime import datetime, timedelta
from collections import defaultdict
import statistics

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

# サンプル100人（上位）
print("[1] Fetching users...")
all_users = []
page = 1
while True:
    resp = requests.get(f"{INTRA_API_BASE}/v2/cursus/21/cursus_users", headers=headers, params={
        "filter[campus_id]": CAMPUS_ID, "page[size]": 100, "page[number]": page, "sort": "user_id",
    })
    if resp.status_code == 429: time.sleep(15); continue
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

# ポイント持ちTOP100
all_users.sort(key=lambda u: u["cp"], reverse=True)
sample = all_users[:100]
print(f"  Total: {len(all_users)}, sampling {len(sample)}")

# 全履歴取得
print(f"\n[2] Fetching histories for {len(sample)} users...")
sale_events = []  # {date, login, sum, type}
pool_contributions = []  # Provided points to the pool
earning_events = []  # Earning (for +4 sale detection)
defense_events = []  # Defense plannification (consumption)

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
        time.sleep(0.25)

    for h in history:
        reason = h.get("reason", "")
        s = h.get("sum", 0)
        dt = h.get("created_at", "")

        if "sales" in reason.lower() or "refund during" in reason.lower():
            sale_events.append({"date": dt[:10], "datetime": dt[:19], "login": login, "sum": s, "type": "refund"})
        elif "Earning after defense" in reason and s == 4:
            sale_events.append({"date": dt[:10], "datetime": dt[:19], "login": login, "sum": s, "type": "earn4x"})
        elif "Provided points to the pool" in reason:
            pool_contributions.append({"date": dt[:10], "login": login, "sum": s})
        elif "Defense plannification" in reason:
            defense_events.append({"date": dt[:10], "login": login, "sum": s})

    print(f" {len(history)} entries")
    time.sleep(0.2)

# ─── セール日の集約 ────────────────────────────────────
print(f"\n{'='*60}")
print("=== エバポセール分析（+4ptレビュー と Refund during sales を統合）===")
print(f"{'='*60}")

# セール日をセットで集約（同じ日は1セールとしてカウント）
sale_dates_by_date = defaultdict(lambda: {"users": set(), "refund_pts": 0, "earn4_count": 0})
for e in sale_events:
    d = e["date"]
    sale_dates_by_date[d]["users"].add(e["login"])
    if e["type"] == "refund":
        sale_dates_by_date[d]["refund_pts"] += e["sum"]
    else:
        sale_dates_by_date[d]["earn4_count"] += 1

# セール日リスト（人数閾値2人以上でノイズ除去）
sale_dates = sorted(d for d, info in sale_dates_by_date.items() if len(info["users"]) >= 2)
print(f"\n総セール日数（2人以上対象）: {len(sale_dates)}")

# 連続した日付をまとめる（セール期間として扱う）
sale_periods = []
cur_start = None
cur_end = None
for d in sale_dates:
    dt = datetime.strptime(d, "%Y-%m-%d")
    if cur_end is None:
        cur_start, cur_end = dt, dt
    elif (dt - cur_end).days <= 3:
        cur_end = dt
    else:
        sale_periods.append((cur_start, cur_end))
        cur_start, cur_end = dt, dt
if cur_end:
    sale_periods.append((cur_start, cur_end))

print(f"セール期間数（3日以内で集約）: {len(sale_periods)}")

# ─── 期間ごとの間隔分析 ──────────────────────────────
print(f"\n[A] セール期間と間隔")
intervals = []
for i, (s, e) in enumerate(sale_periods):
    start_str = s.strftime("%Y-%m-%d")
    end_str = e.strftime("%Y-%m-%d")
    # セール期間の合計人数とpt
    total_users = set()
    total_refund = 0
    for d in sale_dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        if s <= dt <= e:
            info = sale_dates_by_date[d]
            total_users.update(info["users"])
            total_refund += info["refund_pts"]
    if i > 0:
        prev_end = sale_periods[i-1][1]
        interval = (s - prev_end).days
        intervals.append(interval)
        print(f"  {start_str} 〜 {end_str}  対象{len(total_users)}人  +{total_refund}pt  前回から{interval}日")
    else:
        print(f"  {start_str} 〜 {end_str}  対象{len(total_users)}人  +{total_refund}pt")

# ─── 予測モデル ──────────────────────────────────────
if intervals:
    # 直近1年のみで予測（古いデータは構造変化の影響あり）
    one_year_ago = datetime.now() - timedelta(days=365)
    recent_intervals = []
    for i in range(1, len(sale_periods)):
        if sale_periods[i][0] >= one_year_ago:
            recent_intervals.append((sale_periods[i][0] - sale_periods[i-1][1]).days)

    print(f"\n[B] 予測モデル")
    print(f"  全期間の間隔: 平均{statistics.mean(intervals):.1f}日 / 中央値{statistics.median(intervals):.0f}日 / 最短{min(intervals)}日 / 最長{max(intervals)}日")
    if recent_intervals:
        print(f"  直近1年の間隔: 平均{statistics.mean(recent_intervals):.1f}日 / 中央値{statistics.median(recent_intervals):.0f}日 (n={len(recent_intervals)})")

    last_sale_end = sale_periods[-1][1]
    print(f"\n  最後のセール終了日: {last_sale_end.strftime('%Y-%m-%d')}")
    today = datetime.now()
    days_since = (today - last_sale_end).days
    print(f"  本日までの経過日数: {days_since}日")

    # 予測
    if recent_intervals:
        avg_recent = statistics.mean(recent_intervals)
        med_recent = statistics.median(recent_intervals)
        print(f"\n  予測（直近1年の平均）: {(last_sale_end + timedelta(days=int(avg_recent))).strftime('%Y-%m-%d')} (+{int(avg_recent)}日後)")
        print(f"  予測（直近1年の中央値）: {(last_sale_end + timedelta(days=int(med_recent))).strftime('%Y-%m-%d')} (+{int(med_recent)}日後)")

# ─── プール収支の推定 ──────────────────────────────
print(f"\n[C] プール収支の推定（サンプル{len(sample)}人のデータより）")
total_provided = sum(p["sum"] for p in pool_contributions)
total_refunded = sum(s["sum"] for s in sale_events if s["type"] == "refund")
print(f"  プールに入った量（Provided to pool）: {total_provided}pt  ({len(pool_contributions)}件)")
print(f"  プールから出た量（Refund during sales）: {total_refunded}pt  ({sum(1 for s in sale_events if s['type']=='refund')}件)")
print(f"  ネット（入-出）: {total_provided - total_refunded}pt")

# 月別プール推移
print(f"\n[D] 月別プール増減（サンプル{len(sample)}人）")
monthly = defaultdict(lambda: {"in": 0, "out": 0})
for p in pool_contributions:
    monthly[p["date"][:7]]["in"] += p["sum"]
for s in sale_events:
    if s["type"] == "refund":
        monthly[s["date"][:7]]["out"] += s["sum"]

cum = 0
recent_months = sorted(monthly.keys())[-24:]
print(f"  年月      | プール IN | プール OUT | 差分 | 累積")
for m in recent_months:
    info = monthly[m]
    net = info["in"] - info["out"]
    cum += net
    print(f"  {m}  | +{info['in']:4d}     | -{info['out']:4d}      | {net:+4d} | {cum:+5d}")
