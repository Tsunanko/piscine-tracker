#!/usr/bin/env python3
"""
GitHub Actions用データ取得スクリプト
42 APIからPiscine生のデータを取得してJSONファイルに保存する
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

# Load .env if it exists (for local testing)
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

# --- Configuration ---
INTRA_API_BASE = "https://api.intra.42.fr"
TOKEN_URL = f"{INTRA_API_BASE}/oauth/token"

JST = timezone(timedelta(hours=9))
PISCINE_START = datetime(2026, 2, 2, 0, 0, 0, tzinfo=JST)
PISCINE_END   = datetime(2026, 2, 28, 0, 0, 0, tzinfo=JST)
PISCINE_DAYS  = 26
TARGET_HOURS_PER_DAY = 8

CAMPUS_ID       = 26
PISCINE_CURSUS_ID = 9

OUTPUT_DIR = "public"


def get_token():
    client_id     = os.environ["CLIENT_ID"]
    client_secret = os.environ["CLIENT_SECRET"]
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(token, path, params=None):
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{INTRA_API_BASE}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages(token, path, params=None):
    """全ページを取得して結合する"""
    results = []
    page = 1
    base_params = dict(params or {})
    base_params["page[size]"] = 100
    while True:
        base_params["page[number]"] = page
        data = api_get(token, path, base_params)
        results.extend(data)
        print(f"  page {page}: {len(data)} items")
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.6)
    return results


def parse_host(host):
    if not host:
        return None, None
    name = host.split(".")[0]
    m = re.match(r"^(c\d+)(.*)", name)
    if m:
        return m.group(1), m.group(2)
    return None, None


def parse_duration(s):
    if not s:
        return 0.0
    parts = s.split(":")
    if len(parts) != 3:
        return 0.0
    return int(parts[0]) + int(parts[1]) / 60 + float(parts[2]) / 3600


def main():
    print("=== Piscine Data Fetcher ===")
    now = datetime.now(JST)
    print(f"Time: {now.isoformat()}")

    token = get_token()
    print("Token acquired")

    # 1. Piscine生一覧取得
    print("\n[1] Fetching piscine students...")
    cursus_users = fetch_all_pages(token, f"/v2/cursus/{PISCINE_CURSUS_ID}/cursus_users", {
        "filter[campus_id]": CAMPUS_ID,
        "range[begin_at]": f"{PISCINE_START.strftime('%Y-%m-%d')},{PISCINE_END.strftime('%Y-%m-%d')}",
        "sort": "user_id",
    })
    students = {}
    for item in cursus_users:
        user = item.get("user", {})
        login = user.get("login", "")
        if not login:
            continue
        image = (user.get("image") or {})
        image_small = image.get("versions", {}).get("small", "") if image else ""
        students[login] = {
            "login": login,
            "display_name": user.get("usual_full_name") or user.get("displayname", ""),
            "image_small": image_small or "",
            "total_hours": 0,
        }
    print(f"  Total piscine students: {len(students)}")

    # 2. アクティブロケーション取得
    print("\n[2] Fetching active locations...")
    locations_raw = fetch_all_pages(token, f"/v2/campus/{CAMPUS_ID}/locations", {
        "filter[active]": "true",
    })
    active_map = {}  # login -> location info
    for loc in locations_raw:
        user = loc.get("user", {})
        login = user.get("login", "")
        if not login:
            continue
        host = loc.get("host", "")
        cluster, seat = parse_host(host)
        active_map[login] = {
            "host": host,
            "cluster": cluster,
            "seat": seat,
            "begin_at": loc.get("begin_at", ""),
        }
    print(f"  Active locations: {len(active_map)}")

    # 3. 各Piscine生の合計時間を取得
    print("\n[3] Fetching location stats for each student...")
    params = {
        "begin_at": PISCINE_START.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end_at":   PISCINE_END.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    login_list = list(students.keys())
    for i, login in enumerate(login_list):
        try:
            data = api_get(token, f"/v2/users/{login}/locations_stats", params)
            total_h = sum(parse_duration(v) for v in data.values())
            students[login]["total_hours"] = round(total_h, 2)
        except Exception as e:
            print(f"  [WARN] {login}: {e}")
        if (i + 1) % 20 == 0 or (i + 1) == len(login_list):
            print(f"  {i + 1}/{len(login_list)} done")
        time.sleep(0.6)

    # 4. 各Piscine生の日別データも取得して個人JSONを生成
    print("\n[4] Fetching daily breakdown and writing per-user JSON...")
    os.makedirs(f"{OUTPUT_DIR}/data", exist_ok=True)

    for i, login in enumerate(login_list):
        try:
            stats = api_get(token, f"/v2/users/{login}/locations_stats", params)
            daily_hours = {}
            for date_str, dur in stats.items():
                h = parse_duration(dur)
                if h > 0:
                    daily_hours[date_str] = h

            total_hours = students[login]["total_hours"]

            # アクティブセッション考慮
            is_active = login in active_map
            active_extra_hours = 0.0
            if is_active:
                begin_str = active_map[login].get("begin_at", "")
                try:
                    begin = datetime.fromisoformat(begin_str.replace("Z", "+00:00"))
                    active_extra_hours = max(0, (now.astimezone(timezone.utc) - begin).total_seconds() / 3600)
                    total_hours = round(total_hours + active_extra_hours, 2)
                except Exception:
                    pass

            # メトリクス計算
            total_target = TARGET_HOURS_PER_DAY * PISCINE_DAYS
            if now < PISCINE_START:
                elapsed_days = 0
                elapsed_hours = 0
            elif now > PISCINE_END:
                elapsed_hours = (PISCINE_END - PISCINE_START).total_seconds() / 3600
                elapsed_days = PISCINE_DAYS
            else:
                elapsed_hours = (now - PISCINE_START).total_seconds() / 3600
                elapsed_days = elapsed_hours / 24

            avg_hours_per_day = total_hours / elapsed_days if elapsed_days > 0 else 0
            remaining_total = max(0, total_target - total_hours)
            if now >= PISCINE_END:
                remaining_days = 0
                required_avg = 0
            else:
                remaining_secs = (PISCINE_END - max(now, PISCINE_START)).total_seconds()
                remaining_days = remaining_secs / 86400
                required_avg = remaining_total / remaining_days if remaining_days > 0 else 0

            progress_pct = min(100, (total_hours / total_target) * 100) if total_target > 0 else 0
            expected_hours = (elapsed_hours / 24) * TARGET_HOURS_PER_DAY if elapsed_hours > 0 else 0
            diff = total_hours - expected_hours

            # 日別データ
            daily = []
            d = PISCINE_START
            while d < PISCINE_END:
                key = d.strftime("%Y-%m-%d")
                h = round(daily_hours.get(key, 0), 2)
                daily.append({
                    "date": key,
                    "weekday": d.strftime("%a"),
                    "hours": h,
                    "met_target": h >= TARGET_HOURS_PER_DAY,
                })
                d += timedelta(days=1)

            user_json = {
                "login": login,
                "display_name": students[login]["display_name"],
                "image_small": students[login]["image_small"],
                "piscine_start": PISCINE_START.strftime("%Y-%m-%d"),
                "piscine_end": (PISCINE_END - timedelta(days=1)).strftime("%Y-%m-%d"),
                "piscine_days": PISCINE_DAYS,
                "target_hours_per_day": TARGET_HOURS_PER_DAY,
                "total_target_hours": total_target,
                "total_logged_hours": round(total_hours, 2),
                "elapsed_days": round(elapsed_days, 2),
                "elapsed_hours": round(elapsed_hours, 2),
                "avg_hours_per_day": round(avg_hours_per_day, 2),
                "remaining_days": round(remaining_days, 2),
                "remaining_hours_needed": round(remaining_total, 2),
                "required_avg_remaining": round(required_avg, 2),
                "progress_pct": round(progress_pct, 1),
                "diff_from_target": round(diff, 2),
                "on_track": diff >= 0,
                "is_active": is_active,
                "active_extra_hours": round(active_extra_hours, 2),
                "daily": daily,
                "updated_at": now.isoformat(),
            }

            with open(f"{OUTPUT_DIR}/data/{login}.json", "w") as f:
                json.dump(user_json, f, ensure_ascii=False)

        except Exception as e:
            print(f"  [WARN] {login} daily failed: {e}")

        if (i + 1) % 20 == 0 or (i + 1) == len(login_list):
            print(f"  {i + 1}/{len(login_list)} done")
        time.sleep(0.6)

    # 5. ダッシュボード用 data.json 生成
    print("\n[5] Writing dashboard data.json...")
    all_students = list(students.values())
    online_logins = set(active_map.keys()) & set(students.keys())

    online = []
    offline = []
    for s in all_students:
        login = s["login"]
        if login in online_logins:
            loc = active_map[login]
            online.append({**s, **loc})
        else:
            offline.append(s)

    online.sort(key=lambda x: x.get("total_hours", 0), reverse=True)
    offline.sort(key=lambda x: x.get("total_hours", 0), reverse=True)

    dashboard = {
        "online": online,
        "offline": offline,
        "total_students": len(all_students),
        "total_online": len(online),
        "hours_loading": False,
        "cached_at": now.isoformat(),
    }

    with open(f"{OUTPUT_DIR}/data.json", "w") as f:
        json.dump(dashboard, f, ensure_ascii=False)

    print(f"  Wrote public/data.json ({len(online)} online, {len(offline)} offline)")
    print("\nDone!")


if __name__ == "__main__":
    main()
